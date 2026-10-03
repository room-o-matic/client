"""Tests for room-o-matic/docs#17 (client side)."""

import pytest

from roomomatic import WorkerFailedToStart

ROOM = "http://rooms-a.test/v1/rooms/room_1"


def live_invites(world):
    return [i for i in world.invites.values() if not i.get("revoked_at")]


def test_invite_covers_runtime_and_is_passed_to_agentd(rom, world):
    s = rom.summon(ROOM, "t", worker_type="fake")
    ((_, _, _, invite),) = world.calls("POST", "/invites")
    assert invite["ttl_seconds"] == 7200 + 300  # profile runtime + slack
    room = world.sessions_by_id[s.session_id]["room"]
    assert room["invite_id"] == s.invite_id and room["expires_at"]


def test_explicit_ttl_wins(rom, world):
    rom.summon(ROOM, "t", worker_type="fake", invite_ttl_seconds=600)
    ((_, _, _, invite),) = world.calls("POST", "/invites")
    assert invite["ttl_seconds"] == 600


def test_failed_launch_revokes_the_invite(rom, world):
    world.spawn_fails_launch = True
    with pytest.raises(WorkerFailedToStart):
        rom.summon(ROOM, "t", worker_type="fake")
    assert live_invites(world) == []


def test_finalize_session_revokes_when_owner_required(rom, world):
    s = rom.summon(ROOM, "t", worker_type="fake")
    sess = world.sessions_by_id[s.session_id]
    sess.update(room_finalization="owner_required", room_invite_id=s.invite_id, room_url=ROOM)
    assert rom.finalize_session(s.session_url) == "revoked_by_owner"
    assert live_invites(world) == []
    sess["room_finalization"] = "done"
    assert rom.finalize_session(s.session_url) == "done"
