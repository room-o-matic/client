"""Unit tests for PeerAgent (room-o-matic/docs#15) against an in-memory stub of the
lobbyd offer API and roomsd, so the protocol logic is checked deterministically in CI.
The same scenarios run against real services in scripts/conformance.py."""

import httpx
import pytest

from roomomatic import ApiError, PeerAgent

ROOM = "http://rooms-a.test/v1/rooms/room_1"
ORDER = {"accepted": 0, "joined": 1, "working": 2, "handed_off": 3, "completed": 3}


class StubLobby:
    def __init__(self):
        self.offers: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_next: list[Exception] = []

    def _maybe_fail(self):
        if self.fail_next:
            raise self.fail_next.pop(0)

    def register_peer(self, instance_id, **kw):
        self._maybe_fail()
        self.calls.append(("register", instance_id))

    def inbox(self, instance_id):
        self._maybe_fail()
        return [
            dict(o)
            for o in self.offers.values()
            if o["state"] == "offered"
            or (
                o["state"] in ("accepted", "joined", "working")
                and o["assigned_instance"] == instance_id
            )
        ]

    def accept_offer(self, oid, instance_id):
        self.calls.append(("accept", oid))
        o = self.offers[oid]
        o.update(state="accepted", assigned_instance=instance_id)
        return {"offer": o, "changed": True}

    def decline_offer(self, oid, instance_id, reason=None):
        self.calls.append(("decline", oid, reason))
        self.offers[oid].update(state="declined", reason=reason)

    def progress_offer(self, oid, instance_id, state):
        o = self.offers[oid]
        if o["state"] == "cancelled":
            raise ApiError(409, "offer is cancelled", "stub")
        if o["state"] == state:
            return {"offer": o, "changed": False}
        assert ORDER[state] > ORDER[o["state"]], (o["state"], state)
        o["state"] = state
        self.calls.append(("progress", oid, state))
        return {"offer": o, "changed": True}


class StubRooms:
    def __init__(self, deny=False):
        self.deny = deny
        self.joined: list[str] = []
        self.posts: list[tuple] = []

    def join(self, room_id, role=None):
        if self.deny:
            raise ApiError(403, "this room is closed", "stub")
        self.joined.append(room_id)

    def post(self, room_id, body, type="message", **kw):
        self.posts.append((type, body))
        return {"id": len(self.posts)}

    def messages(self, room_id, after_id=0, limit=100):
        return {"messages": [{"id": 1, "body": "history"}]}


class StubClient:
    def __init__(self, lobby, rooms):
        self.lobby, self.rooms = lobby, rooms

    def room(self, room_url):
        return self.rooms, room_url.rsplit("/", 1)[1]


def offer(lobby, oid, role="reviewer"):
    lobby.offers[oid] = {
        "offer_id": oid,
        "room_url": ROOM,
        "role": role,
        "task": "review",
        "state": "offered",
        "assigned_instance": None,
    }


@pytest.fixture
def world():
    lobby, rooms = StubLobby(), StubRooms()
    return lobby, rooms, StubClient(lobby, rooms)


def agent(client, **kw):
    kw.setdefault("policy", lambda o: True)
    kw.setdefault("on_assignment", lambda a: None)
    return PeerAgent(client, "s1", sleep=lambda s: None, **kw)


def test_policy_declines_with_reason_and_is_asked_once(world):
    lobby, _, client = world
    offer(lobby, "o1", role="implementer")
    asked = []

    def policy(o):
        asked.append(o["offer_id"])
        return o["role"] == "reviewer" or "I only review"

    a = agent(client, policy=policy)
    a.step()
    a.step()
    assert asked == ["o1"]
    assert ("decline", "o1", "I only review") in lobby.calls


def test_accept_joins_and_runs_exactly_once(world):
    lobby, rooms, client = world
    offer(lobby, "o1")
    runs = []
    a = agent(client, on_assignment=lambda asg: runs.append(asg.offer_id))
    a.step()
    a.step()  # the working assignment is still in the inbox: no second run
    assert runs == ["o1"] and rooms.joined == ["room_1"]
    assert lobby.offers["o1"]["state"] == "working"


def test_reconnect_resumes_instead_of_rerunning(world):
    lobby, _, client = world
    offer(lobby, "o1")
    runs, resumed = [], []
    agent(client, on_assignment=lambda a: runs.append(1)).step()
    # the process restarts: a fresh PeerAgent for the same session instance
    agent(
        client,
        on_assignment=lambda a: runs.append(2),
        on_resume=lambda a: resumed.append(a.offer_id),
    ).step()
    assert runs == [1] and resumed == ["o1"]


def test_scope_denial_hands_off_without_running(world):
    lobby, rooms, client = world
    rooms.deny = True
    offer(lobby, "o1")
    runs = []
    a = agent(client, on_assignment=lambda asg: runs.append(1))
    a.step()
    assert runs == [] and lobby.offers["o1"]["state"] == "handed_off"
    assert a.errors and a.errors[0].status_code == 403


def test_cancellation_reaches_the_runtime(world):
    lobby, _, client = world
    offer(lobby, "o1")
    cancelled = []
    a = agent(client, on_cancel=lambda asg: cancelled.append(asg.offer_id))
    a.step()
    lobby.offers["o1"]["state"] = "cancelled"  # requester cancels mid-work
    a.step()
    assert cancelled == ["o1"] and "o1" not in a.active


def test_assignment_helpers(world):
    lobby, rooms, client = world
    offer(lobby, "o1")
    done = []

    def work(asg):
        assert asg.catch_up()[0]["body"] == "history"
        asg.send("looking", type="status")
        asg.handoff("reviewed: fine")
        done.append(asg.offer_id)

    agent(client, on_assignment=work).step()
    assert done == ["o1"]
    assert rooms.posts == [("status", "looking"), ("handoff", "reviewed: fine")]
    assert lobby.offers["o1"]["state"] == "handed_off"


def test_backpressure_respects_retry_after(world):
    lobby, _, client = world
    slept = []
    lobby.fail_next = [ApiError(429, "slow down", "stub", retry_after=7.0)]
    steps = iter(range(2))
    a = PeerAgent(
        client,
        "s1",
        policy=lambda o: True,
        on_assignment=lambda a: None,
        sleep=slept.append,
        interval=1,
    )
    a.run(until=lambda: next(steps, None) is None)
    assert slept[0] == 7.0  # waited as told before trying again


def test_transport_errors_back_off(world):
    lobby, _, client = world
    slept = []
    lobby.fail_next = [httpx.ConnectError("down"), httpx.ConnectError("down")]
    steps = iter(range(3))
    a = PeerAgent(
        client,
        "s1",
        policy=lambda o: True,
        on_assignment=lambda a: None,
        sleep=slept.append,
        interval=1,
    )
    a.run(until=lambda: next(steps, None) is None)
    assert slept[:2] == [2, 4]  # exponential
