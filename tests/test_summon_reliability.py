"""Fault-injection tests for room-o-matic/docs#13: one logical assignment per summon, and
no silently abandoned credential or process."""

import httpx
import pytest

from roomomatic import AmbiguousSummon, ApiError, Client

ROOM = "http://rooms-a.test/v1/rooms/room_1"


def live_invites(world):
    return [i for i in world.invites.values() if not i.get("revoked_at")]


def test_response_lost_after_commit_is_reconciled(rom, world):
    world.drop_spawn_response = True
    s = rom.summon(ROOM, "t", worker_type="fake")
    assert s.session_id == "agt_1"
    assert world.spawn_calls == 1  # one worker
    assert len(live_invites(world)) == 1  # its credential was not revoked
    assert world.calls("DELETE", "/invites/") == []


def test_retry_with_same_operation_id_never_spawns_twice(rom, world):
    first = rom.summon(ROOM, "t", worker_type="fake", operation_id="op-retry-1")
    again = rom.summon(ROOM, "t", worker_type="fake", operation_id="op-retry-1")
    assert again.session_id == first.session_id
    assert world.spawn_calls == 1
    assert len(world.calls("POST", "/invites")) == 1  # no second invite either


def test_retry_after_client_restart(world):
    a = Client("http://lobby.test", "lbk_missy", transport=world.transport())
    a.reconcile_backoff = 0
    world.drop_spawn_response = True
    s = a.summon(ROOM, "t", worker_type="fake", operation_id="op-restart-1")
    a.close()
    world.drop_spawn_response = False
    b = Client("http://lobby.test", "lbk_missy", transport=world.transport())  # new process
    b.reconcile_backoff = 0
    again = b.summon(ROOM, "t", worker_type="fake", operation_id="op-restart-1")
    assert again.session_id == s.session_id and world.spawn_calls == 1


def test_unreachable_agentd_is_ambiguous_and_keeps_the_invite(rom, world):
    world.drop_spawn_response = True

    def lost_then_unreachable(handler):
        def h(req):
            if "by-operation" in req.url.path:
                raise httpx.ConnectError("agentd unreachable", request=req)
            return handler(req)

        return h

    rom._agentds.clear()
    flaky = Client(
        "http://lobby.test",
        "lbk_missy",
        transport=httpx.MockTransport(lost_then_unreachable(world.handle)),
    )
    flaky.reconcile_backoff = 0
    with pytest.raises(AmbiguousSummon) as e:
        flaky.summon(ROOM, "t", worker_type="fake")
    assert e.value.operation_id and e.value.instance_url == "http://agentd-1.test"
    assert len(live_invites(world)) == 1  # possibly-running worker keeps its credential
    assert world.calls("DELETE", "/invites/") == []


def test_retry_of_ambiguous_summon_proves_before_spawning(rom, world):
    world.agentd_unreachable = True
    with pytest.raises(AmbiguousSummon):
        rom.summon(
            ROOM,
            "t",
            worker_type="fake",
            operation_id="op-amb-1",
            instance_url="http://agentd-1.test",
        )
    assert world.spawn_calls == 0  # couldn't prove anything, so nothing was started


def test_definite_rejection_revokes_the_invite(rom, world):
    world.spawn_status = 403
    with pytest.raises(ApiError) as e:
        rom.summon(ROOM, "t", worker_type="fake")
    assert e.value.status_code == 403
    assert live_invites(world) == []


def test_capacity_rejection_tries_the_next_instance(rom, world):
    world.instances.append(
        {
            "instance_id": "agentd-host2",
            "base_url": "http://agentd-2.test",
            "worker_types": ["fake"],
            "max_sessions": 4,
            "active_sessions": 0,
        }
    )
    world.approved.add("http://agentd-2.test")
    world.status_by_host["http://agentd-1.test"] = 429
    s = rom.summon(ROOM, "t", worker_type="fake")
    assert s.instance_id == "agentd-host2"
    assert len(live_invites(world)) == 1  # the first instance's invite was revoked


def test_capacity_retries_are_bounded(rom, world):
    for i in range(2, 6):
        url = f"http://agentd-{i}.test"
        world.instances.append(
            {
                "instance_id": f"h{i}",
                "base_url": url,
                "worker_types": ["fake"],
                "max_sessions": 4,
                "active_sessions": 0,
            }
        )
        world.approved.add(url)
    world.spawn_status = 429
    with pytest.raises(ApiError) as e:
        rom.summon(ROOM, "t", worker_type="fake", max_attempts=3)
    assert e.value.status_code == 429
    assert len(world.calls("POST", "/v1/sessions")) == 3
    assert live_invites(world) == []


def test_cleanup_outage_keeps_primary_error_and_journals(world, tmp_path):
    journal = tmp_path / "cleanup.json"
    c = Client(
        "http://lobby.test", "lbk_missy", transport=world.transport(), cleanup_journal=journal
    )
    world.spawn_status = 403
    world.roomsd_delete_down = True
    with pytest.raises(ApiError) as e:
        c.summon(ROOM, "t", worker_type="fake")
    assert e.value.status_code == 403  # the primary error, not the cleanup failure
    assert isinstance(e.value.cleanup_error, httpx.ConnectError)
    assert len(c.pending_cleanups) == 1 and journal.exists()
    assert len(live_invites(world)) == 1

    # A later process picks the obligation up from the journal and completes it.
    world.roomsd_delete_down = False
    later = Client(
        "http://lobby.test", "lbk_missy", transport=world.transport(), cleanup_journal=journal
    )
    assert later.retry_cleanups() == 0
    assert live_invites(world) == []


def test_not_started_after_two_misses_revokes(rom, world):
    def lose_before_commit(req):
        if req.method == "POST" and req.url.path == "/v1/sessions":
            raise httpx.ConnectError("connection reset before agentd saw it", request=req)
        return world.handle(req)

    c = Client("http://lobby.test", "lbk_missy", transport=httpx.MockTransport(lose_before_commit))
    c.reconcile_backoff = 0
    with pytest.raises(httpx.ConnectError):
        c.summon(ROOM, "t", worker_type="fake")
    assert world.spawn_calls == 0
    assert len(world.calls("GET", "/by-operation/")) == 2  # two misses before concluding
    assert live_invites(world) == []  # proven not started, so the invite is revoked
