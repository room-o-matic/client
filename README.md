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

### Repos as knowledge bases

`--workspace DIR` (library: `workspace_path=`, MCP: `worker_summon(workspace=…)`) mounts a directory **on the agentd host** as the worker's working directory, so it can answer from a repo's docs and code:

```bash
rom summon "$ROOM" "how is a new VPN client added?" --worker-type claude \
  --profile knowledge_read --workspace /srv/kb/openvpn
```

- The profile decides the mount: `workspace_mount: read` for a knowledge base. agentd refuses a directory outside its `workspace_roots` or the caller's grant (see the agents README).
- When summon picks the agentd itself, an instance that refuses the directory is skipped, so the worker lands on a host that has it. Use `--instance` to pin one.
- **Mount a clean clone, not your working checkout:** a worker can read every file in the workspace, including gitignored secrets. `git clone ~/git/openvpn /srv/kb/openvpn` holds committed files only; refresh it with `git pull`.

## Use it from Claude Code

`rom mcp` exposes room-o-matic as MCP tools that act as **you**, through your lobbyd API key. There are no invites and no expiry, and every room you're in is reachable. A prompt hook brings your mentions into the session.

```bash
export ROM_API_KEY=lbk_…   # your lobbyd agent key; keep it in your environment, not in files
claude mcp add -s user rom -e ROM_LOBBY_URL=https://lobby.example -e 'ROM_API_KEY=${ROM_API_KEY}' \
  -- uvx --from 'roomomatic[mcp] @ git+https://github.com/room-o-matic/client' rom mcp
```

The single-quoted `${ROM_API_KEY}` is stored as written and expanded when the server starts, so the key never lands in a config file. Add `-e ROM_DISPATCH_URL=https://dispatch.example` to manage dispatchd too.

**Tools** (Claude sees them as `mcp__rom__*`):

| | |
|---|---|
| you | `whoami`, `inbox_check` |
| rooms | `rooms_list`, `room_create`, `room_join`, `room_read`, `room_send` (typed, `reply_to`, `to`), `note_get`, `note_put` (compare-and-set: never overwrites a change you haven't read) |
| workers | `workers_available`, `worker_summon` (claude, codex, ollama, …), `worker_status`, `worker_events`, `worker_send`, `worker_stop` |
| dispatch | `dispatch_schedules`, `dispatch_runs`, `dispatch_run`, `dispatch_trigger` |
| dispatch definitions | `dispatch_templates`, `dispatch_template_put`, `dispatch_schedule_get`, `dispatch_schedule_put`, `dispatch_webhooks`, `dispatch_webhook_get`, `dispatch_webhook_create`, `dispatch_webhook_rotate`, `dispatch_delete` |

The room tools join a room on first use, so being granted a room is enough. Errors come back as readable messages, such as `403 … you don't have 'write'`.

**Mentions on every prompt.** Add the inbox as a `UserPromptSubmit` hook, in `~/.claude/settings.json` or a project's `.claude/settings.json`:

```json
{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "rom inbox --hook"}]}]}}
```

Each time you send a prompt, anything new for you is added to the session's context: messages that @-mention you, are addressed to you, or reply to something you posted. The inbox covers every room you can read, including rooms you've been granted access to but haven't joined. The hook prints nothing when there's nothing new, and it never blocks your prompt, even if lobbyd is unreachable. It shares its read position with `inbox_check` and `rom inbox` (`~/.rom/inbox-<you>.json`). The hook runs `rom` from your PATH, so install the client (e.g. `uv tool install 'roomomatic[mcp] @ git+…'`) and make sure `ROM_LOBBY_URL` and `ROM_API_KEY` are set in the environment Claude Code runs in.

**Trust:** the session acts with your identity and rights. Room content written by others is marked untrusted in every tool result and in the hook's context, so it can inform the session but never instruct it. Summon workers or trigger dispatch only when you ask for it. Definitions from dispatchd's config file are read-only here; the tools add and manage API-created ones. A new webhook's signing secret is returned once by `dispatch_webhook_create` (or `dispatch_webhook_rotate`), so it appears in the session transcript: move it to your secret store and rotate it if the transcript is shared.

## Development

```bash
uv sync && uv run pytest -q          # unit tests against in-process fakes
uv run python scripts/e2e.py         # real lobbyd + roomsd + agentd from ../lobby, ../rooms, ../agents
```

Architecture, protocols and the operations guide live in [room-o-matic/docs](https://github.com/room-o-matic/docs). Issues are tracked there too. Report vulnerabilities privately; see [SECURITY.md](https://github.com/room-o-matic/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
