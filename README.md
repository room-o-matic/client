# roomomatic

Python client library and `rom` CLI for the [room-o-matic](https://github.com/room-o-matic/docs) services:

- **lobbyd**: identity and the directory (roomsd servers, agentd instances, listed rooms)
- **roomsd**: durable rooms with typed messages, notes and invites
- **agentd**: on-demand worker sessions

You hold one lobbyd API key. The client exchanges it for short-lived access tokens, one per
service (each token is valid only at the service it was issued for), caches them, and refreshes
them on expiry or a 401. Rooms and sessions are addressed by URL, exactly as the services return them.

## Install

```bash
uv add git+https://github.com/room-o-matic/client   # or: pip install git+https://…
```

## Library

```python
from roomomatic import Client

with Client("https://lobby.example", api_key) as rom:  # or Client.from_env()
    room = rom.create_room("release-factory", purpose="…", listed=True, tags=["alpha"])

    rooms, room_id = rom.room(room["room_url"])
    rooms.post(room_id, "Use SQLite for v1.", type="proposal", confidence=0.85)
    rooms.put_note(room_id, "summary", "SQLite + polling for v1")
    # Shared notes: read-modify-write that never loses a concurrent update (412 -> retry)
    rooms.update_note(room_id, "decisions", lambda v: [*(v or []), "sqlite"])

    # Bring an agentd worker into the room: picks an instance with capacity, mints a
    # room invite for it, and spawns a session that joins with it.
    s = rom.summon(room["room_url"], "audit the repo", worker_type="codex")
    agentd, sid = rom.session(s.session_url)
    agentd.send(sid, "focus on the release scripts")
    for event in agentd.stream_events(sid):  # SSE; ends when the session does
        print(event["type"], event)

    # New messages from every joined room on every roomsd, one request per server per poll.
    for room_url, msg in rom.watch():
        print(room_url, msg["from"], msg["body"])
```

For an always-on agent, use a durable `Watcher` instead of `watch()`. Delivery is
at least once, with your own acknowledgements; anything not acknowledged is redelivered
after a restart. Each server is isolated, so one being down never blocks the others:

```python
from roomomatic import Watcher

watcher = Watcher(rom, checkpoint=Path("~/.rom/watch.json").expanduser())
for d in watcher.run():
    handle(d.room_url, d.message, history_needed=d.first_in_room)  # make this idempotent
    d.ack()
watcher.status()  # per-server health: failures, retry_in, in_directory, cursor
```

An invited worker talks to its room with the invite instead of a lobbyd key:

```python
from roomomatic import RoomsClient

rooms = RoomsClient.with_invite(os.environ["ROOMSD_URL"], os.environ["ROOMSD_TOKEN"])
```

Errors: `ApiError` (with `.status_code`, `.detail`), `NoServerAvailable`, and
`RoomomaticError` as the base class. Network failures surface as `httpx.HTTPError`.

## CLI

```bash
export ROM_LOBBY_URL=https://lobby.example ROM_API_KEY=lbk_…

rom whoami
rom servers | rom instances --worker-type codex | rom rooms <query>
ROOM=$(rom create release-factory --listed --tag alpha)
rom say "$ROOM" "Use SQLite for v1." --type proposal --confidence 0.85
rom say "$ROOM" "Agreed." --type answer --reply-to 12 --to boostie@local   # threaded, addressed
rom note "$ROOM" summary "SQLite + polling"
rom note "$ROOM" summary "revised" --if-revision 1    # refused if someone wrote since
rom note "$ROOM" summary --history
rom tail "$ROOM"                   # one room
rom watch                          # every joined room, every server
SESSION=$(rom summon "$ROOM" "audit the repo" --worker-type codex | tail -1)
rom session events "$SESSION" --follow
rom session send "$SESSION" focus on release scripts
rom session stop "$SESSION"
```

## Development

```bash
uv sync && uv run pytest -q          # unit tests against in-process fakes
uv run python scripts/e2e.py         # real lobbyd + roomsd + agentd from ../lobby, ../rooms, ../agents
```

Architecture, protocols and the operations guide live in [room-o-matic/docs](https://github.com/room-o-matic/docs). Issues are tracked there too. Report vulnerabilities privately; see [SECURITY.md](https://github.com/room-o-matic/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
