"""High-level entry point for agents and orchestrators.

rom = Client.from_env()                      # ROM_LOBBY_URL + ROM_API_KEY
room = rom.create_room("release-factory", listed=True)
summoned = rom.summon(room["room_url"], "audit the repo", worker_type="codex")
for room_url, msg in rom.watch():            # every joined room on every roomsd
    ...
"""

import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from roomomatic.agentd import AgentdClient
from roomomatic.http import ApiError, RoomomaticError, RoomRef, SessionRef, service_url
from roomomatic.lobby import Lobby
from roomomatic.rooms import RoomsClient
from roomomatic.watcher import Watcher


class NoServerAvailable(RoomomaticError):
    pass


class AmbiguousSummon(RoomomaticError):
    """A spawn may or may not have started and agentd couldn't be asked. The invite was
    left in place; retry summon with the same operation_id and instance_url."""

    def __init__(
        self, message: str, *, operation_id: str, instance_url: str, invite_id: str | None
    ):
        super().__init__(message)
        self.operation_id = operation_id
        self.instance_url = instance_url
        self.invite_id = invite_id


@dataclass(frozen=True)
class Summoned:
    session_url: str
    session_id: str
    instance_id: str
    room_url: str
    invite_id: str | None
    worker_identity: str | None
    operation_id: str | None = None


class Client:
    def __init__(
        self,
        lobby_url: str,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        cleanup_journal: Path | None = None,
    ):
        self._transport = transport
        self.lobby = Lobby(lobby_url, api_key, transport=transport)
        self._rooms: dict[str, RoomsClient] = {}
        self._agentds: dict[str, AgentdClient] = {}
        # docs#13: invite revocations that failed, retried on later calls. Persisted to
        # cleanup_journal (JSON) when given, so they survive a client restart.
        self.cleanup_journal = cleanup_journal
        self.pending_cleanups: list[dict] = (
            json.loads(cleanup_journal.read_text())
            if cleanup_journal is not None and cleanup_journal.exists()
            else []
        )
        self.reconcile_backoff = 1.0  # seconds multiplier; tests set 0

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
        operation_id: str | None = None,
        max_attempts: int = 3,
        **spawn_fields,
    ) -> Summoned:
        """Bring an agentd worker into a room: pick an instance, mint a room invite for it,
        and spawn a session that joins with that invite (room-o-matic/docs#13 semantics).

        Every summon has an operation_id (pass your own to retry safely across process
        restarts). agentd treats it as an idempotency key, so a retry never starts a second
        worker. Outcomes:
        - definite rejection (an HTTP response): the invite is revoked; 429 tries the next
          instance with free capacity, up to max_attempts;
        - unknown outcome (transport error, e.g. a timeout after agentd committed): the
          client reconciles by operation_id before revoking or retrying anything. If
          reconciliation can't reach agentd, AmbiguousSummon is raised and the invite is
          left in place: retry with the same operation_id and instance_url.
        Failed cleanups never replace the primary error; they're journaled and retried.
        """
        self.retry_cleanups()
        op = operation_id or f"sum-{uuid.uuid4().hex}"
        rooms, room_id = self.room(room_url)
        room_ref = RoomRef(rooms.base_url, room_id).url
        if instance_url:  # instance details are fetched only once it's proven safe to spawn
            candidates = [{"base_url": service_url(instance_url)}]
        else:
            candidates = self.lobby.agentd_instances(
                worker_type=worker_type, profile=profile, has_capacity=True
            )[:max_attempts]
            if not candidates:
                raise NoServerAvailable(
                    f"no agentd instance with free capacity for worker_type={worker_type!r}"
                )
        if operation_id:  # a retry: never start anything before proving it didn't start
            for inst in candidates:
                found = self._reconcile(inst["base_url"], op)
                if found:
                    return self._summoned(inst["base_url"], found, room_ref, rooms, room_id)

        last_error: Exception | None = None
        for inst in candidates:
            base = inst["base_url"]
            # Token before invite: lobbyd only mints tokens for approved endpoints (docs#5).
            self.lobby.token(base)
            instance_id = inst.get("instance_id") or self.agentd(base).instance()["instance_id"]
            worker_name = name or f"{instance_id}.{worker_type}-{op[-6:]}"
            invite = self._mint_invite(rooms, room_id, worker_name, role, invite_ttl_seconds)
            agentd = self.agentd(base)
            try:
                spawned = agentd.spawn(
                    task,
                    worker_type=worker_type,
                    profile=profile,
                    room={"room_url": room_ref, "token": invite["token"]},
                    operation_id=op,
                    **spawn_fields,
                )
            except ApiError as e:  # definite: agentd answered and didn't start it
                self._cleanup(e, rooms, room_id, invite["invite_id"])
                last_error = e
                if e.status_code == 429:
                    continue
                raise
            except httpx.TransportError as e:  # unknown: it may have started
                found = self._reconcile(base, op, cause=e, invite_id=invite["invite_id"])
                if found:
                    return self._summoned(base, found, room_ref, rooms, room_id, invite)
                self._cleanup(e, rooms, room_id, invite["invite_id"])
                raise
            return Summoned(
                session_url=agentd.session_url(spawned["session_id"]),
                session_id=spawned["session_id"],
                instance_id=spawned["instance_id"],
                room_url=room_ref,
                invite_id=invite["invite_id"],
                worker_identity=invite["agent"],
                operation_id=op,
            )
        raise last_error  # every candidate was at capacity

    # ----- summon internals (docs#13) ----------------------------------------------------

    def _reconcile(
        self,
        base_url: str,
        operation_id: str,
        *,
        cause: Exception | None = None,
        invite_id: str | None = None,
    ) -> dict | None:
        """The session an operation created on `base_url`, or None once it's proven not to
        have started there: two 404s at least a second apart, since a request may still be
        in flight. Raises AmbiguousSummon when that can't be established."""
        agentd = self.agentd(base_url)
        misses = 0
        for delay in (0.0, 1.0, 2.0):
            time.sleep(delay * self.reconcile_backoff)
            try:
                found = agentd.by_operation(operation_id)
            except (httpx.TransportError, ApiError):
                continue
            if found:
                return found
            misses += 1
            if misses >= 2:
                return None
        raise AmbiguousSummon(
            f"can't tell whether operation {operation_id} started on {base_url}; retry summon"
            " with the same operation_id and instance_url to reconcile",
            operation_id=operation_id,
            instance_url=base_url,
            invite_id=invite_id,
        ) from cause

    def _mint_invite(self, rooms, room_id, worker_name, role, ttl):
        try:
            return rooms.invite(room_id, worker_name, role=role, ttl_seconds=ttl)
        except ApiError as e:
            if e.status_code != 409:
                raise
        # A live invite for this name exists from an earlier attempt of this operation,
        # which reconciliation showed never started a session: replace it.
        for inv in rooms.invites(room_id):
            if inv["agent"].endswith("/" + worker_name) and not inv.get("revoked_at"):
                rooms.revoke_invite(room_id, inv["invite_id"])
        return rooms.invite(room_id, worker_name, role=role, ttl_seconds=ttl)

    def _summoned(self, base_url, session, room_ref, rooms, room_id, invite=None) -> Summoned:
        return Summoned(
            session_url=self.agentd(base_url).session_url(session["session_id"]),
            session_id=session["session_id"],
            instance_id=session["instance_id"],
            room_url=room_ref,
            invite_id=invite["invite_id"] if invite else None,
            worker_identity=invite["agent"] if invite else None,
            operation_id=session.get("operation_id"),
        )

    def _cleanup(self, primary: Exception, rooms, room_id: str, invite_id: str) -> None:
        """Revoke an invite whose spawn definitely didn't start. If that fails, journal it
        and attach the failure to the primary error instead of replacing it."""
        try:
            rooms.revoke_invite(room_id, invite_id)
        except (ApiError, httpx.HTTPError) as e:
            self._journal_add(RoomRef(rooms.base_url, room_id).url, invite_id)
            primary.cleanup_error = e
            primary.add_note(f"cleanup failed and was journaled for retry: {e}")

    def _journal_add(self, room_url: str, invite_id: str) -> None:
        self.pending_cleanups.append({"room_url": room_url, "invite_id": invite_id})
        self._journal_save()

    def _journal_save(self) -> None:
        if self.cleanup_journal is not None:
            self.cleanup_journal.write_text(json.dumps(self.pending_cleanups))

    def retry_cleanups(self) -> int:
        """Retry journaled invite revocations; returns how many are still pending."""
        still = []
        for item in self.pending_cleanups:
            rooms, room_id = self.room(item["room_url"])
            try:
                rooms.revoke_invite(room_id, item["invite_id"])
            except ApiError as e:
                if e.status_code != 404:
                    still.append(item)
            except httpx.HTTPError:
                still.append(item)
        self.pending_cleanups = still
        self._journal_save()
        return len(still)

    def watch(
        self,
        servers: list[str] | None = None,
        *,
        from_start: bool = False,
        interval: float = 2.0,
        once: bool = False,
    ) -> Iterator[tuple[str, dict]]:
        """Yield (room_url, message) for new messages in every joined room on every roomsd.
        A convenience over Watcher that acks each message as it's yielded and keeps no
        checkpoint; use Watcher directly for durable, at-least-once delivery (docs#14).
        Failing servers back off without stopping the others."""
        watcher = self.watcher(
            servers=servers, start="history" if from_start else "now", interval=interval
        )
        watcher._refresh()  # resolve "now" when watch() is called, not on first next()
        return self._auto_ack(watcher, once)

    @staticmethod
    def _auto_ack(watcher, once: bool) -> Iterator[tuple[str, dict]]:
        for d in watcher.run(once=once):
            yield d.room_url, d.message
            d.ack()

    def watcher(self, **kw) -> "Watcher":
        """A durable multi-server Watcher; see roomomatic.watcher for the contract."""
        return Watcher(self, **kw)
