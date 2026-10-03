"""High-level entry point for agents and orchestrators.

rom = Client.from_env()                      # ROM_LOBBY_URL + ROM_API_KEY
room = rom.create_room("release-factory", listed=True)
summoned = rom.summon(room["room_url"], "audit the repo", worker_type="codex")
for room_url, msg in rom.watch():            # every joined room on every roomsd
    ...
"""

import os
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from roomomatic.agentd import AgentdClient
from roomomatic.http import ApiError, RoomomaticError, RoomRef, SessionRef, service_url
from roomomatic.lobby import Lobby
from roomomatic.rooms import RoomsClient


class NoServerAvailable(RoomomaticError):
    pass


@dataclass(frozen=True)
class Summoned:
    session_url: str
    session_id: str
    instance_id: str
    room_url: str
    invite_id: str
    worker_identity: str


class Client:
    def __init__(
        self, lobby_url: str, api_key: str, *, transport: httpx.BaseTransport | None = None
    ):
        self._transport = transport
        self.lobby = Lobby(lobby_url, api_key, transport=transport)
        self._rooms: dict[str, RoomsClient] = {}
        self._agentds: dict[str, AgentdClient] = {}

    @classmethod
    def from_env(cls) -> "Client":
        url = os.environ.get("ROM_LOBBY_URL") or os.environ.get("LOBBYD_URL")
        key = os.environ.get("ROM_API_KEY") or os.environ.get("LOBBYD_API_KEY")
        if not (url and key):
            raise RoomomaticError("set ROM_LOBBY_URL and ROM_API_KEY (a lobbyd agent key)")
        return cls(url, key)

    def close(self) -> None:
        for svc in [self.lobby, *self._rooms.values(), *self._agentds.values()]:
            svc.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- per-service clients (tokens fetched and refreshed per audience) ---------------

    def roomsd(self, base_url: str) -> RoomsClient:
        base_url = service_url(base_url)
        if base_url not in self._rooms:
            self._rooms[base_url] = RoomsClient(
                base_url, self.lobby.token_source(base_url), transport=self._transport
            )
        return self._rooms[base_url]

    def agentd(self, base_url: str) -> AgentdClient:
        base_url = service_url(base_url)
        if base_url not in self._agentds:
            self._agentds[base_url] = AgentdClient(
                base_url, self.lobby.token_source(base_url), transport=self._transport
            )
        return self._agentds[base_url]

    def room(self, room_url: str) -> tuple[RoomsClient, str]:
        """(client for the room's server, room_id) from a room URL."""
        ref = RoomRef.parse(room_url)
        return self.roomsd(ref.base_url), ref.room_id

    def session(self, session_url: str) -> tuple[AgentdClient, str]:
        ref = SessionRef.parse(session_url)
        return self.agentd(ref.base_url), ref.session_id

    # ----- discovery -------------------------------------------------------------------

    def pick_roomsd(self, tag: str | None = None) -> str:
        servers = self.lobby.roomsd_servers(tag=tag)
        if not servers:
            raise NoServerAvailable(f"no live roomsd servers{f' tagged {tag!r}' if tag else ''}")
        return servers[0]["base_url"]

    def pick_agentd(self, worker_type: str, profile: str | None = None) -> dict:
        instances = self.lobby.agentd_instances(
            worker_type=worker_type, profile=profile, has_capacity=True
        )
        if not instances:
            raise NoServerAvailable(
                f"no agentd instance with free capacity for worker_type={worker_type!r}"
                + (f", profile={profile!r}" if profile else "")
            )
        return instances[0]

    # ----- workflows -------------------------------------------------------------------

    def create_room(
        self,
        name: str,
        *,
        server_url: str | None = None,
        server_tag: str | None = None,
        **kw,
    ) -> dict:
        """Create a room on `server_url`, or on a live roomsd picked from the directory."""
        return self.roomsd(server_url or self.pick_roomsd(server_tag)).create_room(name, **kw)

    def summon(
        self,
        room_url: str,
        task: str,
        *,
        worker_type: str,
        profile: str = "workspace_coder",
        name: str | None = None,
        role: str | None = "implementer",
        instance_url: str | None = None,
        invite_ttl_seconds: int | None = None,
        **spawn_fields,
    ) -> Summoned:
        """Bring an agentd worker into a room: pick an instance, mint a room invite for it,
        and spawn a session that joins with that invite. The invite is revoked if the spawn
        fails, so a failed summon leaves no live credential behind."""
        rooms, room_id = self.room(room_url)
        if instance_url:
            instance = self.agentd(instance_url).instance()
            instance["base_url"] = service_url(instance_url)
        else:
            instance = self.pick_agentd(worker_type, profile)
        # Get the access token for the chosen instance before minting the invite: lobbyd
        # only issues tokens for operator-approved endpoints, so an unapproved destination
        # fails here and never receives a task or room capability (docs#5).
        self.lobby.token(instance["base_url"])
        # roomsd allows one live invite per guest identity, so the default name is unique
        # per summon. Pass `name` for a stable identity (e.g. to rotate a credential).
        worker_name = name or f"{instance['instance_id']}.{worker_type}-{secrets.token_hex(3)}"
        invite = rooms.invite(room_id, worker_name, role=role, ttl_seconds=invite_ttl_seconds)
        agentd = self.agentd(instance["base_url"])
        try:
            spawned = agentd.spawn(
                task,
                worker_type=worker_type,
                profile=profile,
                room={"room_url": RoomRef(rooms.base_url, room_id).url, "token": invite["token"]},
                **spawn_fields,
            )
        except (ApiError, httpx.HTTPError):
            rooms.revoke_invite(room_id, invite["invite_id"])
            raise
        return Summoned(
            session_url=agentd.session_url(spawned["session_id"]),
            session_id=spawned["session_id"],
            instance_id=spawned["instance_id"],
            room_url=RoomRef(rooms.base_url, room_id).url,
            invite_id=invite["invite_id"],
            worker_identity=invite["agent"],
        )

    def watch(
        self,
        servers: list[str] | None = None,
        *,
        from_start: bool = False,
        interval: float = 2.0,
        once: bool = False,
    ) -> Iterator[tuple[str, dict]]:
        """Yield (room_url, message) for new messages in every joined room, across every
        roomsd (from the directory unless `servers` is given). One request per server per
        poll, via /v1/me/updates."""
        urls = servers or [s["base_url"] for s in self.lobby.roomsd_servers()]
        # Resolved now, not on first iteration, so "new" means new since watch() was called.
        cursors = {url: 0 if from_start else self._drain(url) for url in urls}
        return self._poll(cursors, interval=interval, once=once)

    def _poll(
        self, cursors: dict[str, int], *, interval: float, once: bool
    ) -> Iterator[tuple[str, dict]]:
        while True:
            got = False
            for url in cursors:
                page = self.roomsd(url).updates(cursor=cursors[url])
                for m in page["messages"]:
                    got = True
                    yield page["room_urls"][m["room_id"]], m
                cursors[url] = page["next_cursor"]
            if not got:
                if once:
                    return
                time.sleep(interval)

    def _drain(self, server_url: str) -> int:
        cursor = 0
        while True:
            page = self.roomsd(server_url).updates(cursor=cursor, limit=500)
            if not page["messages"]:
                return cursor
            cursor = page["next_cursor"]
