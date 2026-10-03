"""Tests for room-o-matic/docs#14: durable, at-least-once, failure-isolated watching."""

import pytest

from roomomatic import Watcher

A, B = "http://rooms-a.test", "http://rooms-b.test"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def two_servers(world):
    world.servers.append({"server_id": "rooms-b", "base_url": B, "tags": []})
    return world


def make(rom, clock=None, **kw):
    clock = clock or Clock()
    return Watcher(rom, clock=clock, sleep=lambda s: None, jitter=lambda: 1.0, **kw), clock


def bodies(deliveries):
    return [d.message["body"] for d in deliveries]


def test_unacked_messages_are_redelivered_after_restart(rom, world, tmp_path):
    ck = tmp_path / "watch.json"
    w, _ = make(rom, checkpoint=ck, start="history")
    world.add_message(A, "room_1", "m1")
    world.add_message(A, "room_1", "m2")
    first = w.poll_once()
    assert bodies(first) == ["m1", "m2"]
    first[0].ack()  # crash after consuming m2 but before acking it
    w2, _ = make(rom, checkpoint=ck)
    assert bodies(w2.poll_once()) == ["m2"]  # at-least-once: m2 comes back, m1 doesn't


def test_offline_backlog_is_delivered_not_skipped(rom, world, tmp_path):
    ck = tmp_path / "watch.json"
    w, _ = make(rom, checkpoint=ck)  # start="now"
    w.poll_once()
    # offline: messages arrive while no watcher runs
    world.add_message(A, "room_1", "while you were away")
    w2, _ = make(rom, checkpoint=ck)
    assert bodies(w2.poll_once()) == ["while you were away"]


def test_new_server_after_startup_is_picked_up(rom, world):
    w, clock = make(rom, refresh_seconds=60)
    w.poll_once()
    world.servers.append({"server_id": "rooms-b", "base_url": B, "tags": []})
    world.add_message(B, "room_9", "early")  # before the watcher knows B: start="now" skips
    clock.t += 61
    assert w.poll_once() == []
    world.add_message(B, "room_9", "from b")
    assert bodies(w.poll_once()) == ["from b"]


def test_directory_loss_keeps_known_servers(two_servers, rom):
    w, clock = make(rom, refresh_seconds=10)
    w.poll_once()
    two_servers.servers = [s for s in two_servers.servers if s["base_url"] != B]  # lease lapsed
    clock.t += 11
    w.poll_once()
    two_servers.add_message(B, "room_9", "still here")
    assert bodies(w.poll_once()) == ["still here"]
    assert w.status()["servers"][B]["in_directory"] is False
    two_servers.directory_down = True
    clock.t += 11
    two_servers.add_message(A, "room_1", "a keeps going")
    assert bodies(w.poll_once()) == ["a keeps going"]
    assert w.status()["directory_error"]


def test_failing_server_is_isolated_and_backs_off(two_servers, rom):
    w, clock = make(rom, backoff_base=1, backoff_max=8)
    w.poll_once()
    two_servers.down_hosts.add(A)
    two_servers.add_message(B, "room_9", "b1")
    assert bodies(w.poll_once()) == ["b1"]  # B is delivered although A is down
    st = w.status()["servers"]
    assert st[A]["healthy"] is False and st[A]["failures"] == 1 and st[B]["healthy"]

    calls = lambda: len([r for r in two_servers.requests if r[1].startswith(A)])  # noqa: E731
    before = calls()
    w.poll_once()
    assert calls() == before  # still backing off: A isn't hammered
    for expected_backoff in (2, 4, 8, 8):  # exponential, capped
        clock.t += expected_backoff
        w.poll_once()
        assert w.status()["servers"][A]["retry_in"] == pytest.approx(expected_backoff, abs=1e-9)

    two_servers.down_hosts.discard(A)
    two_servers.add_message(A, "room_1", "a back")
    clock.t += 8
    assert bodies(w.poll_once()) == ["a back"]
    assert w.status()["servers"][A]["healthy"]


def test_first_message_from_a_new_room_is_flagged(rom, world):
    w, _ = make(rom)
    w.poll_once()
    world.add_message(A, "room_1", "x")
    world.add_message(A, "room_2", "y")
    world.add_message(A, "room_1", "z")
    flags = [(d.message["body"], d.first_in_room) for d in w.poll_once()]
    assert flags == [("x", True), ("y", True), ("z", False)]


def test_pages_and_ordered_acks(rom, world, tmp_path):
    ck = tmp_path / "w.json"
    w, _ = make(rom, checkpoint=ck, start="history", page_limit=2)
    for i in range(5):
        world.add_message(A, "room_1", f"m{i}")
    got = []
    while batch := w.poll_once():
        got += batch
    assert bodies(got) == [f"m{i}" for i in range(5)]
    got[2].ack()  # acks m0..m2
    got[1].ack()  # an older ack never moves the cursor back
    w2, _ = make(rom, checkpoint=ck)
    assert bodies(w2.poll_once()) == ["m3", "m4"]


def test_checkpoint_write_is_atomic(rom, world, tmp_path):
    ck = tmp_path / "w.json"
    w, _ = make(rom, checkpoint=ck, start="history")
    world.add_message(A, "room_1", "m")
    w.poll_once()[0].ack()
    assert ck.exists() and not ck.with_suffix(".tmp").exists()


def test_watch_wrapper_survives_a_down_server(two_servers, rom):
    two_servers.down_hosts.add(A)
    gen = rom.watch(interval=0, once=True)
    two_servers.add_message(B, "room_9", "b")
    assert [m["body"] for _, m in gen] == ["b"]
