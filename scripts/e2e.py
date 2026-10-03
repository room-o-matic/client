"""End-to-end check of lobbyd + roomsd + agentd through the roomomatic client.

Expects the workspace layout from the docs repo: ../lobby, ../rooms and ../agents next to
this repo, each with `uv sync` done. Starts all three on spare ports with throwaway data
dirs, runs the full flow, and exits non-zero on the first failure.

    uv run python scripts/e2e.py
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from roomomatic import TERMINAL_STATUSES, ApiError, Client, RoomsClient

WORKSPACE = Path(__file__).resolve().parents[2]
DOMAIN = "e2e"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bin_(repo: str, name: str) -> str:
    path = WORKSPACE / repo / ".venv" / "bin" / name
    if not path.exists():
        sys.exit(f"missing {path}; run `uv sync` in {WORKSPACE / repo}")
    return str(path)


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        raise SystemExit(1)


@contextmanager
def services():
    tmp = Path(tempfile.mkdtemp(prefix="rom-e2e-"))
    ports = {name: free_port() for name in ("lobby", "rooms", "agentd")}
    urls = {name: f"http://127.0.0.1:{p}" for name, p in ports.items()}
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("ROM_", "LOBBYD_"))}

    lobby_env = {
        **base_env,
        "LOBBYD_DATA_DIR": str(tmp / "lobby"),
        "LOBBYD_ISSUER": urls["lobby"],
        "LOBBYD_DOMAIN": DOMAIN,
    }

    def key(name: str, scope: str) -> str:
        cmd = [bin_("lobby", "lobbyd"), "key", "create", name, "--scope", scope]
        return subprocess.check_output(cmd, env=lobby_env, text=True).strip()

    keys = {
        "missy": key("missy", "agent"),
        "boostie": key("boostie", "agent"),
        "rooms": key("rooms-a", "roomsd"),
        "agentd": key("agentd-e2e", "agentd"),
    }
    procs = [
        subprocess.Popen(
            [bin_("lobby", "lobbyd"), "serve", "--port", str(ports["lobby"])],
            env=lobby_env,
            stdout=open(tmp / "lobby.log", "w"),
            stderr=subprocess.STDOUT,
        ),
        subprocess.Popen(
            [bin_("rooms", "roomsd"), "serve", "--port", str(ports["rooms"])],
            env={
                **base_env,
                "ROOMSD_DATA_DIR": str(tmp / "rooms"),
                "ROOMSD_SERVER_ID": "rooms-a",
                "ROOMSD_BASE_URL": urls["rooms"],
                "LOBBYD_URL": urls["lobby"],
                "LOBBYD_DOMAIN": DOMAIN,
                "ROOMSD_LOBBYD_API_KEY": keys["rooms"],
            },
            stdout=open(tmp / "rooms.log", "w"),
            stderr=subprocess.STDOUT,
        ),
        subprocess.Popen(
            [bin_("agents", "agentd"), "serve", "--port", str(ports["agentd"])],
            env={
                **base_env,
                "AGENTD_DATA_DIR": str(tmp / "agentd"),
                "AGENTD_INSTANCE_ID": "agentd-e2e",
                "AGENTD_BASE_URL": urls["agentd"],
                "AGENTD_LOBBYD_URL": urls["lobby"],
                "AGENTD_LOBBYD_DOMAIN": DOMAIN,
                "AGENTD_LOBBYD_API_KEY": keys["agentd"],
            },
            stdout=open(tmp / "agentd.log", "w"),
            stderr=subprocess.STDOUT,
        ),
    ]
    try:
        for url in urls.values():
            for _ in range(100):
                try:
                    if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise SystemExit(f"{url} did not come up; logs in {tmp}")
        yield urls, keys, tmp
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            p.wait(timeout=20)
        if os.environ.get("ROM_E2E_KEEP"):
            print(f"logs and data kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def wait_for(fn, what: str, timeout: float = 10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := fn():
            return result
        time.sleep(0.1)
    check(False, what)


def main() -> None:
    with services() as (urls, keys, tmp):
        missy = Client(urls["lobby"], keys["missy"])
        boostie = Client(urls["lobby"], keys["boostie"])

        check(missy.lobby.whoami()["identity"] == f"missy@{DOMAIN}", "missy's identity")
        wait_for(lambda: missy.lobby.roomsd_servers(), "roomsd registered in lobbyd")
        wait_for(lambda: missy.lobby.agentd_instances(worker_type="fake"), "agentd registered")

        room = missy.create_room("e2e-room", purpose="end to end", listed=True, tags=["e2e"])
        check(room["room_url"].startswith(urls["rooms"] + "/v1/rooms/"), "room created by URL")
        listed = wait_for(lambda: boostie.lobby.listed_rooms(tag="e2e"), "room listed in lobbyd")
        check(listed[0]["room_url"] == room["room_url"], "boostie discovers the listed room")

        rooms_b, room_id = boostie.room(listed[0]["room_url"])
        rooms_b.join(room_id, role="reviewer")
        rooms_b.post(room_id, "Use SQLite for v1.", type="proposal", confidence=0.8)

        summoned = missy.summon(room["room_url"], "interactive", worker_type="fake")
        check(
            summoned.worker_identity.startswith(f"missy@{DOMAIN}/agentd-e2e.fake-"),
            "worker identity",
        )
        agentd, sid = missy.session(summoned.session_url)
        wait_for(lambda: agentd.session(sid)["status"] == "running", "session running")
        wait_for(
            lambda: missy.lobby.agentd_instances()[0]["active_sessions"] == 1,
            "registry shows the active session",
        )

        agentd.send(sid, "done")
        events = list(agentd.stream_events(sid))
        final = events[-1]
        check(
            final["type"] == "status" and final["status"] == "completed",
            "SSE stream ends completed",
        )
        check(any(e["type"] == "artifact" for e in events), "worker published an artifact")
        check(agentd.session(sid)["status"] in TERMINAL_STATUSES, "session terminal")

        def watched_with_handoff():
            msgs = list(missy.watch(from_start=True, once=True, interval=0))
            return msgs if any(m["type"] == "handoff" for _, m in msgs) else None

        seen = wait_for(watched_with_handoff, "handoff arrived")
        senders = [m["from"] for _, m in seen]
        check(f"boostie@{DOMAIN}" in senders, "watch sees boostie's proposal")
        check(senders.count(summoned.worker_identity) == 2, "watch sees worker start + handoff")

        invite = RoomsClient.with_invite(urls["rooms"], "rmsd_not_real")
        try:
            invite.whoami()
            check(False, "bogus invite rejected")
        except ApiError as e:
            check(e.status_code == 401, "bogus invite rejected")

        try:
            missy.agentd(urls["agentd"]).session("agt_nope")
        except ApiError as e:
            check(e.status_code == 404, "unknown session is 404")

        # The CLI, against the same services.
        cli_env = {**os.environ, "ROM_LOBBY_URL": urls["lobby"], "ROM_API_KEY": keys["boostie"]}
        rom = [sys.executable, "-m", "roomomatic.cli"]
        out = subprocess.check_output([*rom, "rooms", "e2e"], env=cli_env, text=True)
        check(room["room_url"] in out, "rom rooms finds the room")
        subprocess.check_call(
            [*rom, "say", room["room_url"], "from the CLI"], env=cli_env, stdout=subprocess.DEVNULL
        )
        out = subprocess.check_output([*rom, "tail", room["room_url"], "--once"], env=cli_env)
        check("from the CLI" in out.decode(), "rom tail shows the CLI message")

        missy.close()
        boostie.close()
    print("e2e passed")


if __name__ == "__main__":
    main()
