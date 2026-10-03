"""A durable, failure-isolated watcher over every roomsd server (room-o-matic/docs#14).

    watcher = Watcher(rom, checkpoint=Path("~/.rom/watch.json").expanduser())
    for d in watcher.run():
        handle(d.room_url, d.message)     # make this idempotent: delivery is at-least-once
        d.ack()                           # durable: survives restarts

Delivery contract:
- **At least once.** A message is redelivered until acked, including after a restart:
  only acked positions are persisted. Exactly-once external actions are the caller's
  job (idempotency keys, receipts).
- Per server, messages arrive in id order across all joined rooms, and acking message N
  acknowledges everything before it on that server, so ack in delivery order.
- A server seen for the first time starts at "now" (start="now") or replays history
  (start="history"). Known servers resume from their checkpoint, so an offline backlog
  is delivered, never skipped.
- A room seen for the first time is flagged `first_in_room`. Messages posted in it before
  you joined aren't part of the live stream; fetch them with RoomsClient.messages if needed.
- One failing server never blocks the others: it backs off (exponential, jittered,
  capped) while healthy servers keep delivering. `status()` shows which are degraded.
- The server set is refreshed from lobbyd every `refresh_seconds`. Servers that drop out
  of the directory (for example a lapsed lease during an outage) keep being polled with
  their checkpoints; a failed directory fetch keeps the current set.
"""

import json
import os
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from roomomatic.http import ApiError, service_url


@dataclass
class ServerState:
    cursor: int = 0  # last acked message id (persisted)
    delivered: int = 0  # last delivered id in this process (not persisted)
    seen_rooms: set[str] = field(default_factory=set)
    failures: int = 0
    next_try: float = 0.0
    last_error: str | None = None
    last_ok: float | None = None
    in_directory: bool = True


@dataclass(frozen=True)
class Delivery:
    server_url: str
    room_url: str
    message: dict
    first_in_room: bool
    _ack: Callable[[], None] = field(repr=False, compare=False)

    def ack(self) -> None:
        self._ack()


class Watcher:
    def __init__(
        self,
        client,
        *,
        checkpoint: Path | None = None,
        servers: list[str] | None = None,
        start: str = "now",
        refresh_seconds: float = 60,
        interval: float = 2.0,
        page_limit: int = 100,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        if start not in ("now", "history"):
            raise ValueError("start must be 'now' or 'history'")
        self.client = client
        self.checkpoint = checkpoint
        self.static_servers = [service_url(s) for s in servers] if servers else None
        self.start = start
        self.refresh_seconds = refresh_seconds
        self.interval = interval
        self.page_limit = page_limit
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        self.state: dict[str, ServerState] = {}
        self._last_refresh: float | None = None
        self.directory_error: str | None = None
        self._load()

    # ----- checkpoints ---------------------------------------------------------------

    def _load(self) -> None:
        if self.checkpoint is None or not self.checkpoint.exists():
            return
        data = json.loads(self.checkpoint.read_text())
        for url, s in data.get("servers", {}).items():
            self.state[url] = ServerState(
                cursor=s["cursor"], delivered=s["cursor"], seen_rooms=set(s.get("rooms", []))
            )

    def _save(self) -> None:
        if self.checkpoint is None:
            return
        data = {
            "servers": {
                url: {"cursor": st.cursor, "rooms": sorted(st.seen_rooms)}
                for url, st in self.state.items()
                if st.cursor >= 0  # unresolved start positions aren't persisted
            }
        }
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.checkpoint.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.checkpoint)  # atomic: a crash never leaves a torn checkpoint

    def ack(self, server_url: str, message_id: int) -> None:
        st = self.state[server_url]
        if message_id > st.cursor:
            st.cursor = message_id
            self._save()

    # ----- discovery -----------------------------------------------------------------

    def _refresh(self) -> None:
        now = self._clock()
        if self._last_refresh is not None and now - self._last_refresh < self.refresh_seconds:
            return
        self._last_refresh = now
        if self.static_servers is not None:
            listed = self.static_servers
        else:
            try:
                listed = [service_url(s["base_url"]) for s in self.client.lobby.roomsd_servers()]
                self.directory_error = None
            except (httpx.HTTPError, ApiError) as e:
                self.directory_error = str(e)  # keep polling what we already know
                return
        for st in self.state.values():
            st.in_directory = False
        added = False
        for url in listed:
            if url not in self.state:
                self.state[url] = self._new_server(url)
                added = True
            self.state[url].in_directory = True
        if added:
            # Persist the starting point now: a restart before the first ack must resume
            # here (and deliver the backlog), not start over at "now".
            self._save()

    def _new_server(self, url: str) -> ServerState:
        st = ServerState()
        if self.start == "now":
            try:
                st.cursor = st.delivered = self._end_cursor(url)
            except (httpx.HTTPError, ApiError) as e:
                # Can't learn "now" yet: back off and try again, rather than replay history.
                st.cursor = st.delivered = -1
                self._fail(st, e)
        return st

    def _end_cursor(self, url: str) -> int:
        cursor = 0
        while True:
            page = self.client.roomsd(url).updates(cursor=cursor, limit=500)
            if not page["messages"]:
                return cursor
            cursor = page["next_cursor"]

    # ----- polling -------------------------------------------------------------------

    def _fail(self, st: ServerState, error: Exception) -> None:
        st.failures += 1
        backoff = min(self.backoff_base * 2 ** (st.failures - 1), self.backoff_max)
        st.next_try = self._clock() + backoff * (0.5 + self._jitter() / 2)
        st.last_error = str(error)

    def poll_once(self) -> list[Delivery]:
        """One pass over every server that isn't backing off. Never raises for a single
        server's failure."""
        self._refresh()
        out: list[Delivery] = []
        for url, st in list(self.state.items()):
            if st.next_try > self._clock():
                continue
            try:
                if st.cursor < 0:  # first contact failed earlier (start="now")
                    st.cursor = st.delivered = self._end_cursor(url)
                    self._save()
                page = self.client.roomsd(url).updates(cursor=st.delivered, limit=self.page_limit)
            except (httpx.HTTPError, ApiError) as e:
                self._fail(st, e)
                continue
            st.failures, st.last_error, st.last_ok = 0, None, self._clock()
            for m in page["messages"]:
                first = m["room_id"] not in st.seen_rooms
                st.seen_rooms.add(m["room_id"])
                st.delivered = m["id"]
                out.append(
                    Delivery(
                        server_url=url,
                        room_url=page["room_urls"][m["room_id"]],
                        message=m,
                        first_in_room=first,
                        _ack=lambda url=url, mid=m["id"]: self.ack(url, mid),
                    )
                )
        return out

    def run(self, *, once: bool = False) -> Iterator[Delivery]:
        while True:
            batch = self.poll_once()
            yield from batch
            if not batch:
                if once:
                    return
                self._sleep(self.interval)

    def status(self) -> dict:
        """Per-server health, for exposing degraded/stale state."""
        now = self._clock()
        return {
            "directory_error": self.directory_error,
            "servers": {
                url: {
                    "healthy": st.failures == 0 and st.cursor >= 0,
                    "failures": st.failures,
                    "last_error": st.last_error,
                    "seconds_since_ok": None if st.last_ok is None else now - st.last_ok,
                    "retry_in": max(0.0, st.next_try - now),
                    "cursor": st.cursor,
                    "in_directory": st.in_directory,
                }
                for url, st in self.state.items()
            },
        }
