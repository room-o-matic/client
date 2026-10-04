"""rom: command line for lobbyd, roomsd and agentd.

Configure with ROM_LOBBY_URL and ROM_API_KEY (a lobbyd agent key). Rooms and sessions are
addressed by URL, exactly as the services return them.
"""

import argparse
import json
import sys
import time

import httpx

from roomomatic.agentd import TERMINAL_STATUSES
from roomomatic.client import Client
from roomomatic.http import RoomomaticError


def out(obj) -> None:
    print(json.dumps(obj, indent=2) if not isinstance(obj, str) else obj)


def fmt_message(room_url: str | None, m: dict) -> str:
    """One line per message, with the typed fields that change how it should be read:
    what it replies to, who it's addressed to, confidence and severity."""
    where = f"{room_url} " if room_url else ""
    topic = f" ({m['topic']})" if m.get("topic") else ""
    meta = []
    if m.get("in_reply_to") is not None:
        meta.append(f"re:#{m['in_reply_to']}")
    if m.get("to"):
        meta.append("to:" + ",".join(m["to"]))
    if m.get("confidence") is not None:
        meta.append(f"conf={m['confidence']:g}")
    if m.get("severity"):
        meta.append(f"severity={m['severity']}")
    if m.get("reply_requested"):
        meta.append("reply-requested")
    extra = f" [{' '.join(meta)}]" if meta else ""
    return f"{where}[{m['id']}] {m['from']} {m['type']}{topic}{extra}: {m['body']}"


def fmt_event(e: dict) -> str:
    detail = {k: v for k, v in e.items() if k not in ("id", "session_id", "type", "time")}
    return f"[{e['id']}] {e['time']} {e['type']} {json.dumps(detail)}"


# ----- commands -------------------------------------------------------------------------


def cmd_whoami(rom: Client, a) -> None:
    out(rom.lobby.whoami())


def cmd_servers(rom: Client, a) -> None:
    for s in rom.lobby.roomsd_servers(tag=a.tag):
        print(f"{s['server_id']:<20} {s['base_url']}  tags={s['tags']}")


def cmd_instances(rom: Client, a) -> None:
    for i in rom.lobby.agentd_instances(worker_type=a.worker_type, profile=a.profile):
        print(
            f"{i['instance_id']:<20} {i['base_url']}  {i['active_sessions']}/{i['max_sessions']}"
            f" busy  workers={i['worker_types']}"
        )


def cmd_rooms(rom: Client, a) -> None:
    for r in rom.lobby.listed_rooms(q=a.query, tag=a.tag):
        print(f"{r['room_url']}  {r['name']}" + (f" — {r['purpose']}" if r["purpose"] else ""))


def cmd_create(rom: Client, a) -> None:
    room = rom.create_room(
        a.name,
        server_url=a.server,
        server_tag=a.server_tag,
        purpose=a.purpose,
        listed=a.listed,
        tags=a.tag or [],
    )
    print(room["room_url"])


def cmd_join(rom: Client, a) -> None:
    rooms, room_id = rom.room(a.room_url)
    out(rooms.join(room_id, role=a.role))


def cmd_say(rom: Client, a) -> None:
    rooms, room_id = rom.room(a.room_url)
    fields = {"topic": a.topic} if a.topic else {}
    if a.confidence is not None:
        fields["confidence"] = a.confidence
    m = rooms.post(room_id, a.body, type=a.type, **fields)
    print(fmt_message(None, m))


def cmd_tail(rom: Client, a) -> None:
    rooms, room_id = rom.room(a.room_url)
    after = 0
    while True:
        page = rooms.messages(room_id, after_id=after)
        for m in page["messages"]:
            print(fmt_message(None, m), flush=True)
        after = page["latest_message_id"]
        if page["messages"]:
            continue
        if a.once:
            return
        time.sleep(a.interval)


def cmd_watch(rom: Client, a) -> None:
    for room_url, m in rom.watch(
        a.server or None, from_start=a.from_start, interval=a.interval, once=a.once
    ):
        print(fmt_message(room_url, m), flush=True)


def cmd_note(rom: Client, a) -> None:
    rooms, room_id = rom.room(a.room_url)
    if a.value is not None:
        try:
            value = json.loads(a.value)
        except json.JSONDecodeError:
            value = a.value  # plain strings needn't be quoted
        out(rooms.put_note(room_id, a.key, value, if_revision=a.if_revision))
    elif a.key and a.history:
        out(rooms.note_history(room_id, a.key))
    elif a.key:
        out(rooms.note(room_id, a.key))
    else:
        out(rooms.notes(room_id))


def cmd_invite(rom: Client, a) -> None:
    rooms, room_id = rom.room(a.room_url)
    inv = rooms.invite(room_id, a.name, role=a.role, ttl_seconds=a.ttl)
    print(f"{inv['agent']}  invite_id={inv['invite_id']}  expires={inv['expires_at']}")
    print(inv["token"])


def cmd_summon(rom: Client, a) -> None:
    s = rom.summon(
        a.room_url,
        a.task,
        worker_type=a.worker_type,
        profile=a.profile,
        name=a.name,
        role=a.role,
        instance_url=a.instance,
    )
    print(f"{s.worker_identity} on {s.instance_id}")
    print(s.session_url)


def cmd_session(rom: Client, a) -> None:
    agentd, sid = rom.session(a.session_url)
    if a.action == "status":
        out(agentd.session(sid))
    elif a.action == "send":
        if not a.text:
            raise RoomomaticError("send needs a message")
        out(agentd.send(sid, " ".join(a.text)))
    elif a.action == "stop":
        s = agentd.stop(sid)
        print(f"{s['status']} ({s['stop_reason']})")
    elif a.action == "events":
        events = agentd.stream_events(sid) if a.follow else agentd.events(sid)
        for e in events:
            print(fmt_event(e), flush=True)
            if a.follow and e["type"] == "status" and e.get("status") in TERMINAL_STATUSES:
                return


# ----- parser ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rom", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, fn, help):
        c = sub.add_parser(name, help=help)
        c.set_defaults(func=fn)
        return c

    cmd("whoami", cmd_whoami, "show this key's identity")
    c = cmd("servers", cmd_servers, "list live roomsd servers")
    c.add_argument("--tag")
    c = cmd("instances", cmd_instances, "list live agentd instances")
    c.add_argument("--worker-type")
    c.add_argument("--profile")
    c = cmd("rooms", cmd_rooms, "search listed rooms")
    c.add_argument("query", nargs="?")
    c.add_argument("--tag")

    c = cmd("create", cmd_create, "create a room; prints its URL")
    c.add_argument("name")
    c.add_argument("--purpose")
    c.add_argument("--listed", action="store_true", help="publish in the lobbyd directory")
    c.add_argument("--tag", action="append", help="room tag (repeatable)")
    c.add_argument("--server", help="roomsd base URL (default: pick from the directory)")
    c.add_argument("--server-tag", help="pick a roomsd with this tag")

    c = cmd("join", cmd_join, "join a room")
    c.add_argument("room_url")
    c.add_argument("--role")
    c = cmd("say", cmd_say, "post a message")
    c.add_argument("room_url")
    c.add_argument("body")
    c.add_argument("--type", default="message")
    c.add_argument("--topic")
    c.add_argument("--confidence", type=float)
    c = cmd("tail", cmd_tail, "follow one room")
    c.add_argument("room_url")
    c.add_argument("--once", action="store_true")
    c.add_argument("--interval", type=float, default=2.0)
    c = cmd("watch", cmd_watch, "follow every joined room on every roomsd")
    c.add_argument("--server", action="append", help="limit to these roomsd URLs")
    c.add_argument("--from-start", action="store_true", help="replay history first")
    c.add_argument("--once", action="store_true")
    c.add_argument("--interval", type=float, default=2.0)
    c = cmd("note", cmd_note, "list notes, get one, or set one (JSON or plain text)")
    c.add_argument("room_url")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.add_argument(
        "--if-revision", type=int, help="only write if the note is at this revision (0 = new)"
    )
    c.add_argument("--history", action="store_true", help="show the key's past revisions")

    c = cmd("invite", cmd_invite, "mint a room invite; prints identity then token")
    c.add_argument("room_url")
    c.add_argument("name")
    c.add_argument("--role")
    c.add_argument("--ttl", type=int)
    c = cmd("summon", cmd_summon, "bring an agentd worker into a room; prints session URL")
    c.add_argument("room_url")
    c.add_argument("task")
    c.add_argument("--worker-type", required=True)
    c.add_argument("--profile", default="workspace_coder")
    c.add_argument("--name", help="worker name in the room (default <instance>.<worker>)")
    c.add_argument("--role", default="implementer")
    c.add_argument("--instance", help="agentd base URL (default: pick from the registry)")
    c = cmd("session", cmd_session, "status | send | stop | events of a session URL")
    c.add_argument("action", choices=["status", "send", "stop", "events"])
    c.add_argument("session_url")
    c.add_argument("text", nargs="*", help="message for send")
    c.add_argument("--follow", action="store_true", help="events: stream until it ends")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with Client.from_env() as rom:
            args.func(rom, args)
    except (RoomomaticError, httpx.HTTPError) as e:
        print(f"rom: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
