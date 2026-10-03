"""Peer-adapter conformance suite (room-o-matic/docs#15).

Every peer adapter (PeerAgent today; an Odin/Boostie/Missy bridge or an attached
interactive session later) must pass the same scenarios. A Harness supplies the services:

    class Harness(Protocol):
        requester: Client                    # offers work (e.g. Missy)
        def peer_client(self) -> Client: ... # credentials for the peer under test
        def peer_identity(self) -> str: ...  # e.g. "boostie@local"
        def new_room(self, admission: str = "open") -> str: ...   # room URL
        def make_adapter(self, client, instance_id, **callbacks): ...  # adapter under test

    results = run_all(harness)   # [(scenario, "pass" | "FAIL: ...")]

scripts/conformance.py runs this against real lobbyd and roomsd.
"""

import uuid
from collections.abc import Callable


def _drive(adapter, steps: int = 3) -> None:
    for _ in range(steps):
        adapter.step()


def declines_by_policy(h) -> None:
    room = h.new_room()
    peer = h.make_adapter(
        h.peer_client(),
        f"conf-{uuid.uuid4().hex[:8]}",
        policy=lambda o: "not my area",
        on_assignment=lambda a: None,
    )
    o = h.requester.lobby.offer(h.peer_identity(), room, "x", offer_id=f"decl-{uuid.uuid4().hex}")
    _drive(peer)
    got = h.requester.lobby.get_offer(o["offer_id"])
    assert got["state"] == "declined" and got["decline_reason"] == "not my area", got


def repeated_offer_runs_once(h) -> None:
    room, runs = h.new_room(), []
    peer = h.make_adapter(
        h.peer_client(),
        f"conf-{uuid.uuid4().hex[:8]}",
        policy=lambda o: True,
        on_assignment=lambda a: runs.append(a.offer_id),
    )
    oid = f"rep-{uuid.uuid4().hex}"
    for _ in range(3):  # the requester retries its offer
        h.requester.lobby.offer(h.peer_identity(), room, "x", offer_id=oid)
    _drive(peer)
    assert runs == [oid], runs


def reconnect_never_reruns(h) -> None:
    room, runs, resumed = h.new_room(), [], []
    instance = f"conf-{uuid.uuid4().hex[:8]}"
    first = h.make_adapter(
        h.peer_client(), instance, policy=lambda o: True, on_assignment=lambda a: runs.append(1)
    )
    h.requester.lobby.offer(h.peer_identity(), room, "x", offer_id=f"rec-{uuid.uuid4().hex}")
    _drive(first)
    again = h.make_adapter(
        h.peer_client(),
        instance,
        policy=lambda o: True,  # new process
        on_assignment=lambda a: runs.append(2),
        on_resume=lambda a: resumed.append(1),
    )
    _drive(again)
    assert runs == [1] and resumed == [1], (runs, resumed)


def scope_denial_runs_nothing(h) -> None:
    room, runs = h.new_room(admission="closed"), []
    peer = h.make_adapter(
        h.peer_client(),
        f"conf-{uuid.uuid4().hex[:8]}",
        policy=lambda o: True,
        on_assignment=lambda a: runs.append(1),
    )
    o = h.requester.lobby.offer(h.peer_identity(), room, "x", offer_id=f"deny-{uuid.uuid4().hex}")
    _drive(peer)
    assert runs == [], runs
    assert h.requester.lobby.get_offer(o["offer_id"])["state"] == "handed_off"


def cancellation_reaches_adapter(h) -> None:
    room, cancelled = h.new_room(), []
    peer = h.make_adapter(
        h.peer_client(),
        f"conf-{uuid.uuid4().hex[:8]}",
        policy=lambda o: True,
        on_assignment=lambda a: None,
        on_cancel=lambda a: cancelled.append(a.offer_id),
    )
    o = h.requester.lobby.offer(h.peer_identity(), room, "x", offer_id=f"can-{uuid.uuid4().hex}")
    _drive(peer, 1)
    h.requester.lobby.cancel_offer(o["offer_id"])
    _drive(peer, 1)
    assert cancelled == [o["offer_id"]], cancelled


def auth_expiry_is_survived(h) -> None:
    room, runs = h.new_room(), []
    client = h.peer_client()
    peer = h.make_adapter(
        client,
        f"conf-{uuid.uuid4().hex[:8]}",
        policy=lambda o: True,
        on_assignment=lambda a: (a.send("still authorized"), runs.append(1)),
    )
    rooms_base = room.split("/v1/rooms/")[0]
    client.lobby.token(rooms_base)
    client.lobby._tokens[rooms_base] = ("expired-or-revoked", 2**31)  # a dead cached token
    h.requester.lobby.offer(h.peer_identity(), room, "x", offer_id=f"auth-{uuid.uuid4().hex}")
    _drive(peer)
    assert runs == [1], runs  # 401 -> fresh token -> join and post succeeded


SCENARIOS: list[Callable] = [
    declines_by_policy,
    repeated_offer_runs_once,
    reconnect_never_reruns,
    scope_denial_runs_nothing,
    cancellation_reaches_adapter,
    auth_expiry_is_survived,
]


def run_all(harness) -> list[tuple[str, str]]:
    results = []
    for scenario in SCENARIOS:
        try:
            scenario(harness)
            results.append((scenario.__name__, "pass"))
        except Exception as e:  # noqa: BLE001 - report every scenario
            results.append((scenario.__name__, f"FAIL: {type(e).__name__}: {e}"))
    return results
