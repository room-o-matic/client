"""A runtime-neutral peer (room-o-matic/docs#15): the room-o-matic side of an existing
named agent (Odin, Boostie, Missy, an interactive Claude/Codex session, …).

    agent = PeerAgent(
        rom, "missy-laptop-1",
        capabilities=["review"],
        policy=lambda offer: offer["role"] == "reviewer" or "only doing reviews",
        on_assignment=do_the_work,          # your runtime runs model turns here
    )
    agent.run()

PeerAgent does protocol and room I/O only; it never runs a model, so it plugs into any
runtime. Contract (see design/peer-protocol.md in the docs repo):

- register/heartbeat a session instance; a heartbeat is never consent;
- poll the offer inbox; `policy(offer)` returns True to accept, or False/a string reason to
  decline. Offers the policy has already answered aren't asked again;
- on accept: join the room with the agent's own identity, report joined, then working,
  and call `on_assignment(Assignment)` exactly once per assignment. If joining is refused
  (closed room, scope denial) the assignment is handed off with the reason, no work runs;
- after a reconnect, assignments this session already started call `on_resume` (they're
  never re-run), and cancelled ones call `on_cancel`;
- 429s from lobbyd are respected (Retry-After), and other errors back off.

Credentials stay local: the agent holds its own lobbyd key; lobbyd never gets a model
credential or any way to run code on the agent's host.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from roomomatic.http import ApiError

Decision = bool | str


@dataclass
class Assignment:
    offer: dict
    agent: "PeerAgent" = field(repr=False)

    @property
    def offer_id(self) -> str:
        return self.offer["offer_id"]

    @property
    def room_url(self) -> str:
        return self.offer["room_url"]

    def _room(self):
        return self.agent.client.room(self.room_url)

    def catch_up(self, after_id: int = 0) -> list[dict]:
        """Room history (the live stream doesn't include messages from before joining)."""
        rooms, room_id = self._room()
        return rooms.messages(room_id, after_id=after_id, limit=500)["messages"]

    def send(self, body: str, type: str = "message", **fields) -> dict:
        rooms, room_id = self._room()
        return rooms.post(room_id, body, type=type, **fields)

    def _progress(self, state: str) -> dict:
        return self.agent.client.lobby.progress_offer(self.offer_id, self.agent.instance_id, state)

    def handoff(self, summary: str) -> None:
        """Post a handoff message to the room and mark the assignment handed off."""
        self.send(summary, type="handoff")
        self._progress("handed_off")

    def complete(self, summary: str | None = None) -> None:
        if summary:
            self.send(summary, type="status")
        self._progress("completed")

    def leave(self) -> None:
        """Stop following the room. Membership stays (admins remove members); this only
        ends this session's involvement."""
        self.agent.left.add(self.offer_id)


class PeerAgent:
    def __init__(
        self,
        client,
        instance_id: str,
        *,
        policy: Callable[[dict], Decision],
        on_assignment: Callable[[Assignment], None],
        on_resume: Callable[[Assignment], None] | None = None,
        on_cancel: Callable[[Assignment], None] | None = None,
        capabilities: list[str] | None = None,
        owner: str | None = None,
        max_assignments: int = 1,
        ttl_seconds: int = 60,
        interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.instance_id = instance_id
        self.policy = policy
        self.on_assignment = on_assignment
        self.on_resume = on_resume or (lambda a: None)
        self.on_cancel = on_cancel or (lambda a: None)
        self.capabilities = capabilities or []
        self.owner = owner
        self.max_assignments = max_assignments
        self.ttl_seconds = ttl_seconds
        self.interval = interval
        self._clock, self._sleep = clock, sleep
        self._last_beat: float | None = None
        self.active: dict[str, Assignment] = {}
        self.answered: set[str] = set()
        self.left: set[str] = set()
        self.errors: list[Exception] = []

    def heartbeat(self, force: bool = False) -> None:
        now = self._clock()
        if (
            not force
            and self._last_beat is not None
            and now - self._last_beat < self.ttl_seconds / 3
        ):
            return
        self.client.lobby.register_peer(
            self.instance_id,
            owner=self.owner,
            capabilities=self.capabilities,
            max_assignments=self.max_assignments,
            ttl_seconds=self.ttl_seconds,
        )
        self._last_beat = now

    def step(self) -> None:
        """One heartbeat-and-inbox pass. Raises only for programming errors; service
        errors are recorded and backed off by run()."""
        self.heartbeat()
        lobby = self.client.lobby
        inbox = lobby.inbox(self.instance_id)
        seen = {o["offer_id"] for o in inbox}
        for offer_id in list(self.active):
            if offer_id not in seen:  # cancelled (or finished elsewhere): stop working
                self.on_cancel(self.active.pop(offer_id))
        for offer in inbox:
            oid = offer["offer_id"]
            if offer["state"] == "offered" and oid not in self.answered:
                decision = self.policy(offer)
                self.answered.add(oid)
                if decision is True:
                    lobby.accept_offer(oid, self.instance_id)
                    self._start(offer)
                else:
                    reason = decision if isinstance(decision, str) else None
                    lobby.decline_offer(oid, self.instance_id, reason=reason)
            elif offer["state"] in ("accepted", "joined", "working") and oid not in self.active:
                self._start(offer)  # after a reconnect

    def _start(self, offer: dict) -> None:
        a = Assignment(offer, self)
        lobby = self.client.lobby
        try:
            rooms, room_id = a._room()
            rooms.join(room_id, role=offer.get("role"))
        except ApiError as e:
            if e.status_code in (403, 404):  # closed room / scope denial: no work runs
                lobby.progress_offer(a.offer_id, self.instance_id, "handed_off")
                self.errors.append(e)
                return
            raise
        if offer["state"] in ("offered", "accepted"):
            lobby.progress_offer(a.offer_id, self.instance_id, "joined")
        # Progress is forward-only and idempotent: re-reporting "working" after a
        # reconnect returns changed=false, which is what stops a second run.
        started = lobby.progress_offer(a.offer_id, self.instance_id, "working")
        self.active[a.offer_id] = a
        if started["changed"]:
            self.on_assignment(a)
        else:
            self.on_resume(a)  # this session already started it: never run it twice
        if a.offer_id in self.left:
            self.active.pop(a.offer_id, None)

    def run(self, *, until: Callable[[], bool] | None = None) -> None:
        failures = 0
        while not (until and until()):
            try:
                self.step()
                failures = 0
                self._sleep(self.interval)
            except ApiError as e:
                self.errors.append(e)
                failures += 1
                if e.retry_after is not None:
                    self._sleep(e.retry_after)
                else:
                    self._sleep(min(self.interval * 2**failures, 60))
            except httpx.HTTPError as e:
                self.errors.append(e)
                failures += 1
                self._sleep(min(self.interval * 2**failures, 60))
