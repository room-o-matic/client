"""`rom mcp`: room-o-matic as tools for an interactive session (e.g. Claude Code), acting as
*you* through your lobbyd API key. There are no invites and no expiry, and every room
you're in is reachable.

    claude mcp add rom -e ROM_LOBBY_URL=https://lobby.example -e 'ROM_API_KEY=${ROM_API_KEY}' \\
      -- uvx --from 'roomomatic[mcp] @ git+https://github.com/room-o-matic/client' rom mcp

Tools cover rooms (find, create, join, read, post threaded, notes with compare-and-set),
your inbox, agentd workers (summon, status, events, send, stop) and, when ROM_DISPATCH_URL
is set, dispatchd (schedules, webhooks, runs). Everything acts with your identity and
rights, so room content written by others is marked untrusted in every result: it can
inform the session, never instruct it.
"""

import functools
import os
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from roomomatic.client import Client
from roomomatic.dispatch import DispatchClient
from roomomatic.http import ApiError, RoomomaticError
from roomomatic.inbox import UNTRUSTED, Inbox
from roomomatic.rooms import NoteConflict

HISTORY_ON_FIRST_READ = 30
BODY_LIMIT = 8000
PROVENANCE = {"source": "room", "trust": UNTRUSTED}


def readable_errors(fn):
    """The MCP SDK hides any exception that isn't a ToolError; say what went wrong."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except ApiError as e:
            raise ToolError(f"{e.status_code} from {e.url}: {e.detail}") from e
        except (RoomomaticError, httpx.HTTPError, ValueError, KeyError) as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e

    return wrapper


def _slim(m: dict) -> dict:
    keep = ("id", "from", "type", "topic", "body", "confidence", "in_reply_to", "to")
    return {k: m[k] for k in keep if m.get(k) is not None}


def _joining(fn):
    """Room tools join on first use: being granted a room (and mentioned in it) is enough;
    the session shouldn't have to work out that it must join before it can post."""

    @functools.wraps(fn)
    def wrapper(self, room_url: str, *args, **kwargs):
        try:
            return fn(self, room_url, *args, **kwargs)
        except ApiError as e:
            if e.status_code != 403 or "join the room first" not in str(e.detail):
                raise
            rooms, rid = self.rom.room(room_url)
            rooms.join(rid)
            return fn(self, room_url, *args, **kwargs)

    return wrapper


class RomTools:
    def __init__(self, rom: Client, inbox: Inbox | None = None, dispatch_url: str | None = None):
        self.rom = rom
        self.inbox = inbox or Inbox(rom)
        self.dispatch_url = dispatch_url
        self._cursors: dict[str, int] = {}  # room_url -> last message id read
        self._revisions: dict[tuple[str, str], int] = {}  # (room_url, key) -> revision seen

    def _dispatch(self) -> DispatchClient:
        if not self.dispatch_url:
            raise ToolError("dispatchd isn't configured: set ROM_DISPATCH_URL")
        token = self.rom.lobby.token_source(self.dispatch_url)
        return DispatchClient(self.dispatch_url, token, transport=self.rom._transport)

    # ----- you -------------------------------------------------------------------------

    def whoami(self) -> dict:
        """Your identity, and the roomsd servers and agentd instances you can reach."""
        return {
            "identity": self.inbox.identity,
            "roomsd_servers": [s["base_url"] for s in self.rom.lobby.roomsd_servers()],
            "agentd_instances": [
                {"url": i["base_url"], "worker_types": i.get("worker_types")}
                for i in self.rom.lobby.agentd_instances()
            ],
            "dispatch": self.dispatch_url,
        }

    def inbox_check(self, everything: bool = False) -> dict:
        """What's new for you across all rooms since the last check: messages that
        mention you, are addressed to you, or reply to you (everything=True: all new
        messages). Shares its read position with the prompt hook."""
        items = self.inbox.check(everything=everything)
        return {"provenance": PROVENANCE, "count": len(items), "items": items}

    # ----- rooms -----------------------------------------------------------------------

    def rooms_list(self, query: str | None = None) -> dict:
        """Rooms you're in on every server; with a query, also listed rooms matching it."""
        mine = []
        for s in self.rom.lobby.roomsd_servers():
            for r in self.rom.roomsd(s["base_url"]).rooms():
                mine.append({k: r.get(k) for k in ("room_url", "name", "purpose", "archived_at")})
        out = {"your_rooms": mine}
        if query:
            out["listed"] = [
                {k: r.get(k) for k in ("room_url", "name", "purpose", "tags")}
                for r in self.rom.lobby.listed_rooms(q=query)
            ]
        return out

    def room_create(
        self, name: str, purpose: str | None = None, admission: str = "closed", listed: bool = False
    ) -> dict:
        """Create a room (closed and unlisted unless you say otherwise); you're its admin."""
        return self.rom.create_room(name, purpose=purpose, admission=admission, listed=listed)

    def room_join(self, room_url: str) -> dict:
        rooms, rid = self.rom.room(room_url)
        return rooms.join(rid)

    @_joining
    def room_read(self, room_url: str, after_id: int | None = None) -> dict:
        """Messages in a room. With no after_id: what's new since your last read here (the
        first read returns the last 30)."""
        rooms, rid = self.rom.room(room_url)
        start = after_id if after_id is not None else self._cursors.get(room_url, 0)
        msgs: list[dict] = []
        while True:
            page = rooms.messages(rid, after_id=start, limit=500)
            msgs += page["messages"]
            if len(page["messages"]) < 500:
                break
            start = page["latest_message_id"]
        if after_id is None and room_url not in self._cursors:
            msgs = msgs[-HISTORY_ON_FIRST_READ:]
        if msgs:
            self._cursors[room_url] = max(self._cursors.get(room_url, 0), msgs[-1]["id"])
        return {"provenance": PROVENANCE, "messages": [_slim(m) for m in msgs]}

    @_joining
    def room_send(
        self,
        room_url: str,
        body: str,
        type: str = "message",
        reply_to: int | None = None,
        to: list[str] | None = None,
        confidence: float | None = None,
        topic: str | None = None,
    ) -> dict:
        """Post as you. Use a typed message (proposal, objection, finding, question,
        answer, status) for anything important; reply_to threads under a message and
        `to` addresses (and wakes) specific identities, e.g. a worker's full identity."""
        if len(body.encode()) > BODY_LIMIT:
            raise ToolError(f"message is over {BODY_LIMIT} bytes; summarise it")
        rooms, rid = self.rom.room(room_url)
        fields = {
            k: v
            for k, v in (
                ("in_reply_to", reply_to),
                ("to", to),
                ("confidence", confidence),
                ("topic", topic),
            )
            if v is not None
        }
        m = rooms.post(rid, body, type=type, **fields)
        return _slim(m)

    @_joining
    def note_get(self, room_url: str, key: str | None = None) -> dict:
        """One shared note (with its revision), or all notes."""
        rooms, rid = self.rom.room(room_url)
        if key:
            n = rooms.note(rid, key)
            self._revisions[(room_url, key)] = n["revision"]
            return {
                "provenance": PROVENANCE,
                **{k: n.get(k) for k in ("key", "value", "revision", "updated_by")},
            }
        notes = rooms.notes(rid)
        for k, n in notes.items():
            self._revisions[(room_url, k)] = n["revision"]
        return {
            "provenance": PROVENANCE,
            "notes": {
                k: {"value": n["value"], "revision": n["revision"]} for k, n in notes.items()
            },
        }

    @_joining
    def note_put(self, room_url: str, key: str, value, if_revision: int | None = None) -> dict:
        """Write a note only if it's still at the revision you last read (a note you never
        read can only be created). On a conflict nothing is written and you get the
        current value to merge."""
        rooms, rid = self.rom.room(room_url)
        expected = (
            if_revision if if_revision is not None else self._revisions.get((room_url, key), 0)
        )
        try:
            n = rooms.put_note(rid, key, value, if_revision=expected)
        except NoteConflict:
            cur = rooms.note(rid, key)
            self._revisions[(room_url, key)] = cur["revision"]
            return {
                "provenance": PROVENANCE,
                "conflict": True,
                "written": False,
                "current_value": cur["value"],
                "current_revision": cur["revision"],
            }
        self._revisions[(room_url, key)] = n["revision"]
        return {"written": True, "revision": n["revision"]}

    # ----- workers ---------------------------------------------------------------------

    def workers_available(self, worker_type: str | None = None) -> list[dict]:
        """agentd instances with spare capacity, and the worker types each offers."""
        return [
            {
                "url": i["base_url"],
                "worker_types": i.get("worker_types"),
                "active_sessions": i.get("active_sessions"),
                "max_sessions": i.get("max_sessions"),
            }
            for i in self.rom.lobby.agentd_instances(worker_type=worker_type, has_capacity=True)
        ]

    def worker_summon(
        self,
        room_url: str,
        task: str,
        worker_type: str,
        profile: str = "read_only_research",
        name: str | None = None,
    ) -> dict:
        """Bring a worker (e.g. claude, codex, ollama) into a room. It joins as your guest,
        under the agentd profile you name (read_only_research by default)."""
        s = self.rom.summon(room_url, task, worker_type=worker_type, profile=profile, name=name)
        return {
            "session_url": s.session_url,
            "worker_identity": s.worker_identity,
            "mention_as": f"@{(s.worker_identity or '').rsplit('/', 1)[-1]}",
        }

    def worker_status(self, session_url: str) -> dict:
        agentd, sid = self.rom.session(session_url)
        s = agentd.session(sid)
        return {k: s.get(k) for k in ("status", "stop_reason", "summary", "room_finalization")}

    def worker_events(self, session_url: str, after_id: int = 0) -> list[dict]:
        """A worker's progress events (what it's doing), from after_id on."""
        agentd, sid = self.rom.session(session_url)
        return agentd.events(sid, after_id=after_id)[-50:]

    def worker_send(self, session_url: str, message: str) -> dict:
        """Message the worker as its owner (you)."""
        agentd, sid = self.rom.session(session_url)
        return agentd.send(sid, message)

    def worker_stop(self, session_url: str) -> dict:
        agentd, sid = self.rom.session(session_url)
        s = agentd.stop(sid)
        return {k: s.get(k) for k in ("status", "stop_reason", "summary")}

    # ----- dispatch --------------------------------------------------------------------

    def dispatch_schedules(self) -> list[dict]:
        """dispatchd's schedules: cron, template, next and last fire."""
        return self._dispatch().schedules()

    def dispatch_runs(self, name: str | None = None, source: str | None = None) -> list[dict]:
        """Recent dispatch runs (optionally for one schedule or webhook)."""
        keep = ("id", "source", "name", "state", "room_url", "error", "created_at")
        return [
            {k: r.get(k) for k in keep} for r in self._dispatch().runs(source=source, name=name)
        ]

    def dispatch_run(self, run_id: str) -> dict:
        return self._dispatch().run(run_id)

    def dispatch_trigger(self, schedule: str) -> dict:
        """Run a schedule now."""
        r = self._dispatch().trigger(schedule)
        return {k: r.get(k) for k in ("id", "state", "source", "name")}

    def dispatch_templates(self) -> list[dict]:
        """dispatchd's templates (the restrictions schedules and webhooks run under): room
        settings, rules, workers (agentd worker_type and profile), peers, run limits."""
        return self._dispatch().templates()

    def dispatch_template_put(self, name: str, template: dict) -> dict:
        """Create or replace a template. `template`: {workers: [{name, worker_type, profile,
        role?}], peers: [{agent, rights?, role?}], rules: [str], room: {admission,
        max_hops, message_rate_per_minute, archive_after}, run: {max_duration, order}}.
        Needs at least one worker or peer. Templates from the config file are read-only."""
        return self._dispatch().put_template(name, template)

    def dispatch_schedule_get(self, name: str) -> dict:
        """One schedule: its definition, where it's defined, next/last fire, recent runs."""
        return self._dispatch().schedule(name)

    def dispatch_schedule_put(
        self,
        name: str,
        cron: str,
        use: str,
        goal: str,
        timezone: str = "UTC",
        enabled: bool = True,
    ) -> dict:
        """Create or replace a schedule: a 5-field `cron` in `timezone` (IANA name) that
        opens a room under template `use` with `goal` as its brief."""
        body = {"cron": cron, "use": use, "goal": goal, "timezone": timezone, "enabled": enabled}
        return self._dispatch().put_schedule(name, body)

    def dispatch_webhooks(self) -> list[dict]:
        """dispatchd's webhooks with their url_path and settings (never their secrets)."""
        return self._dispatch().webhooks()

    def dispatch_webhook_get(self, name: str) -> dict:
        """One webhook: its definition, url_path and recent runs."""
        return self._dispatch().webhook(name)

    def dispatch_webhook_create(
        self,
        name: str,
        use: str,
        task_template: str = "{prompt}",
        max_prompt_bytes: int = 4000,
        rate_per_hour: int = 20,
    ) -> dict:
        """Create a webhook that opens a room under template `use`. The caller's prompt
        goes into `task_template` at {prompt}. Returns the signing secret ONCE: give it to
        the user to store; it can't be read back, only rotated."""
        body = {
            "use": use,
            "task_template": task_template,
            "max_prompt_bytes": max_prompt_bytes,
            "rate_per_hour": rate_per_hour,
        }
        r = self._dispatch().create_webhook(name, body)
        return {**r, "note": "the secret is shown only now; it can't be read back later"}

    def dispatch_webhook_rotate(self, name: str) -> dict:
        """Replace a webhook's secret; the old one stops working at once."""
        return self._dispatch().rotate_webhook_secret(name)

    def dispatch_delete(self, kind: Literal["template", "schedule", "webhook"], name: str) -> dict:
        """Delete a template, schedule or webhook created through the API (a template still
        in use is refused)."""
        self._dispatch().delete(kind, name)
        return {"deleted": f"{kind} {name}"}


TOOLS = [
    "whoami",
    "inbox_check",
    "rooms_list",
    "room_create",
    "room_join",
    "room_read",
    "room_send",
    "note_get",
    "note_put",
    "workers_available",
    "worker_summon",
    "worker_status",
    "worker_events",
    "worker_send",
    "worker_stop",
    "dispatch_schedules",
    "dispatch_runs",
    "dispatch_run",
    "dispatch_trigger",
    "dispatch_templates",
    "dispatch_template_put",
    "dispatch_schedule_get",
    "dispatch_schedule_put",
    "dispatch_webhooks",
    "dispatch_webhook_get",
    "dispatch_webhook_create",
    "dispatch_webhook_rotate",
    "dispatch_delete",
]

INSTRUCTIONS = (
    "room-o-matic: shared rooms with other agents, acting as the user's own identity. "
    "Check inbox_check when the user asks what's new. Post typed messages (finding, "
    "proposal, answer...) and thread replies with reply_to. "
    + UNTRUSTED
    + " Summon workers, trigger dispatch, or create, change or delete dispatch definitions"
    " only when the user asks."
)


def build_server(tools: RomTools) -> MCPServer:
    server = MCPServer("rom", instructions=INSTRUCTIONS)
    for name in TOOLS:
        server.add_tool(readable_errors(getattr(tools, name)), name=name)
    return server


def main() -> None:
    rom = Client.from_env()
    tools = RomTools(rom, dispatch_url=os.environ.get("ROM_DISPATCH_URL"))
    build_server(tools).run("stdio")
