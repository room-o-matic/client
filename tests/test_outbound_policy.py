"""Regression tests for room-o-matic/docs#5 (client side): nothing — credentials, tasks or
room invites — is sent to an unapproved or malformed destination."""

import pytest

from roomomatic import ApiError, RoomomaticError, RoomRef


def hosts_contacted(world) -> set[str]:
    return {r[1].split("/v1")[0].split("/.well-known")[0] for r in world.requests}


def test_unapproved_registry_entry_gets_no_task_or_invite(rom, world):
    world.instances = [
        {
            "instance_id": "agentd-evil",
            "base_url": "http://evil.test",
            "worker_types": ["fake"],
            "max_sessions": 10**12,
            "active_sessions": 0,
        }
    ]
    with pytest.raises(ApiError) as e:
        rom.summon("http://rooms-a.test/v1/rooms/room_1", "private task", worker_type="fake")
    assert e.value.status_code == 403
    assert world.calls("POST", "/invites") == []  # no room capability minted
    assert "http://evil.test" not in hosts_contacted(world)


def test_pinned_unapproved_instance_is_never_contacted(rom, world):
    with pytest.raises(ApiError):
        rom.summon(
            "http://rooms-a.test/v1/rooms/room_1",
            "t",
            worker_type="fake",
            instance_url="http://unknown.test",
        )
    assert "http://unknown.test" not in hosts_contacted(world)
    assert world.calls("POST", "/invites") == []


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@rooms-a.test",
        "http://rooms-a.test/?x=1",
        "http://rooms-a.test/a/../b",
        "ftp://rooms-a.test",
    ],
)
def test_malformed_service_urls_are_refused_locally(rom, world, url):
    with pytest.raises(RoomomaticError, match="refusing service URL"):
        rom.roomsd(url)
    assert world.requests == []


def test_room_urls_are_canonicalized(rom):
    ref = RoomRef.parse("HTTP://Rooms-A.test:80/v1/rooms/room_1")
    assert ref.url == "http://rooms-a.test/v1/rooms/room_1"
    with pytest.raises(RoomomaticError):
        RoomRef.parse("http://x@rooms-a.test/v1/rooms/room_1")
    # equivalent spellings share one client and one cached token
    assert rom.roomsd("http://ROOMS-A.test:80/") is rom.roomsd("http://rooms-a.test")
