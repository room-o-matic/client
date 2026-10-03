import json
import time
from collections import defaultdict

import httpx
import pytest

from roomomatic import Client

LOBBY = "http://lobby.test"


class FakeWorld:
    """Minimal fakes of lobbyd, roomsd and agentd behind one httpx.MockTransport.

    Records every request as (method, url, auth header, json body) for assertions. Each
    service only accepts tokens minted for its own base URL, like the real ones.
    """

    def __init__(self):
        self.requests: list[tuple[str, str, str | None, dict | None]] = []
        self.issued = defaultdict(int)  # audience -> tokens issued
        self.token_ttl = 900
        self.revoked_tokens: set[str] = set()
        self.servers = [{"server_id": "rooms-a", "base_url": "http://rooms-a.test", "tags": []}]
        self.instances = [
            {
                "instance_id": "agentd-host1",
                "base_url": "http://agentd-1.test",
                "worker_types": ["fake"],
                "max_sessions": 4,
                "active_sessions": 0,
            }
        ]
        self.messages: dict[str, list[dict]] = defaultdict(list)  # roomsd url -> messages
        self.spawn_status = 201
        self.invites: dict[str, dict] = {}
        # docs#13 fault injection
        self.sessions_by_op: dict[str, dict] = {}  # agentd's durable idempotency records
        self.spawn_calls = 0
        self.drop_spawn_response = False  # commit the spawn, then lose the response
        self.agentd_unreachable = False  # every agentd request fails at transport level
        self.roomsd_delete_down = False  # invite revocation fails at transport level
        self.status_by_host: dict[str, int] = {}  # per-agentd forced spawn status
        self.instance_names = {
            "http://agentd-1.test": "agentd-host1",
            "http://agentd-2.test": "agentd-host2",
        }
        self.allowed_pairs: dict[str, list] = {}  # per-agentd override of allowed_for_you
        self.down_hosts: set[str] = set()
        self.spawn_fails_launch = False  # docs#17: 201 with status "failed"
        self.sessions_by_id: dict[str, dict] = {}  # docs#14: any request to these fails (transport)
        self.directory_down = False
        # docs#20: roomsd notes with revisions: (origin, room, key) -> list of revisions
        self.notes: dict[tuple, list[dict]] = defaultdict(list)
        self.note_changes: list[dict] = []
        # Audiences lobbyd will mint tokens for (operator-approved endpoints, docs#5).
        self.approved = {"http://rooms-a.test", "http://rooms-b.test", "http://agentd-1.test"}

    # ----- plumbing -----

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        auth = req.headers.get("authorization")
        self.requests.append((req.method, str(req.url), auth, body))
        origin = f"{req.url.scheme}://{req.url.host}"
        if origin in self.down_hosts:
            raise httpx.ConnectError(f"{origin} unreachable", request=req)
        if origin == LOBBY:
            if self.directory_down and req.url.path == "/v1/servers/roomsd":
                raise httpx.ConnectError("lobbyd unreachable", request=req)
            return self.lobby(req, body)
        if "agentd" in origin and self.agentd_unreachable:
            raise httpx.ConnectError("agentd unreachable", request=req)
        if "rooms" in origin and req.method == "DELETE" and self.roomsd_delete_down:
            raise httpx.ConnectError("roomsd unreachable", request=req)
        token = (auth or "").removeprefix("Bearer ")
        if token in self.revoked_tokens or not token.startswith(f"tok:{origin}:"):
            return httpx.Response(401, json={"detail": "bad token"})
        if "rooms" in origin:
            return self.roomsd(origin, req, body)
        return self.agentd(origin, req, body)

    def calls(self, method: str, url_part: str) -> list[tuple]:
        return [r for r in self.requests if r[0] == method and url_part in r[1]]

    # ----- services -----

    def lobby(self, req, body):
        p = req.url.path
        if p == "/v1/token":
            aud = body["audience"]
            if aud not in self.approved:
                return httpx.Response(403, json={"detail": f"{aud} is not approved"})
            self.issued[aud] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok:{aud}:{self.issued[aud]}",
                    "expires_at": int(time.time()) + self.token_ttl,
                },
            )
        if p == "/v1/servers/roomsd":
            return httpx.Response(200, json=self.servers)
        if p == "/v1/registry/agentd":
            wt = req.url.params.get("worker_type")
            found = [i for i in self.instances if not wt or wt in i["worker_types"]]
            if req.url.params.get("has_capacity"):
                found = [i for i in found if i["active_sessions"] < i["max_sessions"]]
            return httpx.Response(200, json=found)
        if p == "/v1/whoami":
            return httpx.Response(200, json={"identity": "missy@test"})
        return httpx.Response(404)

    def roomsd(self, origin, req, body):
        p = req.url.path
        if req.method == "POST" and p == "/v1/rooms":
            return httpx.Response(
                201, json={"room_id": "room_1", "room_url": f"{origin}/v1/rooms/room_1"}
            )
        if req.method == "GET" and p.endswith("/invites"):
            return httpx.Response(200, json=list(self.invites.values()))
        if req.method == "POST" and p.endswith("/invites"):
            agent = f"missy@test/{body['name']}"
            if any(i["agent"] == agent and not i.get("revoked_at") for i in self.invites.values()):
                return httpx.Response(409, json={"detail": "already has a live invite"})
            inv = {
                "invite_id": f"inv_{len(self.invites) + 1}",
                "agent": f"missy@test/{body['name']}",
                "token": "rmsd_secret",
                "expires_at": "2099-01-01T00:00:00.000Z",
                "role": body["role"],
            }
            self.invites[inv["invite_id"]] = inv
            return httpx.Response(201, json=inv)
        if req.method == "DELETE" and "/invites/" in p:
            inv_id = p.rsplit("/", 1)[1]
            if inv_id in self.invites:
                self.invites[inv_id]["revoked_at"] = "now"
            return httpx.Response(200, json={"revoked_at": "now"})
        if "/notes" in p:
            return self.roomsd_notes(origin, req, body)
        if p == "/v1/me/updates":
            cursor = int(req.url.params.get("cursor", 0))
            limit = int(req.url.params.get("limit", 100))
            msgs = [m for m in self.messages[origin] if m["id"] > cursor][:limit]
            return httpx.Response(
                200,
                json={
                    "messages": msgs,
                    "room_urls": {m["room_id"]: f"{origin}/v1/rooms/{m['room_id']}" for m in msgs},
                    "next_cursor": msgs[-1]["id"] if msgs else cursor,
                },
            )
        return httpx.Response(404)

    def roomsd_notes(self, origin, req, body):
        parts = req.url.path.split("/")  # '', v1, rooms, <room>, notes, [key], [history]
        room = parts[3]
        if parts[5:] == ["changes"]:
            after = int(req.url.params.get("after", 0))
            changes = [c for c in self.note_changes if c["room"] == room and c["id"] > after]
            out = [{k: v for k, v in c.items() if k != "room"} for c in changes]
            return httpx.Response(
                200, json={"changes": out, "next_cursor": out[-1]["id"] if out else after}
            )
        revs = self.notes[(origin, room, parts[5])]
        if len(parts) == 7 and parts[6] == "history":
            return httpx.Response(200, json=list(reversed(revs)))
        if req.method == "GET":
            return httpx.Response(200, json=revs[-1]) if revs else httpx.Response(404)
        current = revs[-1]["revision"] if revs else 0
        want = body.get("if_revision")
        if want is not None and want != current:
            return httpx.Response(412, json={"detail": f"note is at revision {current}"})
        note = {"key": parts[5], "value": body["value"], "revision": current + 1}
        revs.append(note)
        self.note_changes.append(
            {
                "id": len(self.note_changes) + 1,
                "room": room,
                "key": parts[5],
                "revision": current + 1,
            }
        )
        return httpx.Response(200, json=note)

    def agentd(self, origin, req, body):
        p = req.url.path
        if req.method == "POST" and p == "/v1/sessions":
            forced = self.status_by_host.get(origin, self.spawn_status)
            if forced != 201:
                return httpx.Response(forced, json={"detail": "at capacity"})
            op = body.get("operation_id")
            if op and op in self.sessions_by_op:
                return httpx.Response(200, json={**self.sessions_by_op[op], "replayed": True})
            self.spawn_calls += 1
            session = {
                "session_id": f"agt_{self.spawn_calls}",
                "instance_id": "agentd-host1" if "agentd-1" in origin else "agentd-host2",
                "status": "starting",
                "operation_id": op,
            }
            if op:
                self.sessions_by_op[op] = session
            session["room"] = {k: v for k, v in (body.get("room") or {}).items() if k != "token"}
            if self.spawn_fails_launch:
                session["status"] = "failed"
            self.sessions_by_id[session["session_id"]] = session
            if self.drop_spawn_response:
                raise httpx.ReadTimeout("response lost after commit", request=req)
            return httpx.Response(201, json=session)
        if req.method == "GET" and p.startswith("/v1/sessions/agt_") and p.count("/") == 3:
            sess = self.sessions_by_id.get(p.rsplit("/", 1)[1])
            return httpx.Response(200, json=sess) if sess else httpx.Response(404)
        if p.startswith("/v1/sessions/by-operation/"):
            op = p.rsplit("/", 1)[1]
            if op in self.sessions_by_op:
                return httpx.Response(200, json=self.sessions_by_op[op])
            return httpx.Response(404, json={"detail": "no session for that operation_id"})
        if p == "/v1/instance":
            pairs = [{"profile": "workspace_coder", "worker_type": "fake"}]
            runtimes = {"workspace_coder": 7200}
            name = self.instance_names.get(origin, "agentd-pinned")
            caps = {
                "protocol": "room-o-matic.agentd/1",
                "kind": "gateway",
                "pairs": pairs,
                "allowed_for_you": self.allowed_pairs.get(origin, pairs),
                "profile_runtime_seconds": runtimes,
            }
            return httpx.Response(
                200, json={"instance_id": name, "base_url": origin, "capabilities": caps}
            )
        if p.endswith("/events"):
            frames = (
                "id: 1\nevent: status\ndata: "
                + json.dumps({"id": 1, "type": "status", "status": "running"})
                + "\n\n: keepalive\n\nid: 2\nevent: final\ndata: "
                + json.dumps({"id": 2, "type": "final", "summary": "ok"})
                + "\n\n"
            )
            return httpx.Response(200, text=frames, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    def add_message(self, roomsd: str, room_id: str, body: str) -> None:
        all_ids = [m["id"] for msgs in self.messages.values() for m in msgs]
        self.messages[roomsd].append(
            {"id": max(all_ids, default=0) + 1, "room_id": room_id, "body": body}
        )


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
def rom(world) -> Client:
    with Client(LOBBY, "lbk_missy", transport=world.transport()) as c:
        c.reconcile_backoff = 0  # no real sleeping in tests
        yield c
