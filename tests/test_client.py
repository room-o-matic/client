import re

import httpx
import pytest

from roomomatic import ApiError, NoServerAvailable, RoomomaticError, RoomRef, SessionRef
from roomomatic.agentd import parse_sse


def test_room_and_session_urls():
    ref = RoomRef.parse("http://rooms-a.test:8766/v1/rooms/room_01ABC/")
    assert (ref.base_url, ref.room_id) == ("http://rooms-a.test:8766", "room_01ABC")
    assert ref.url == "http://rooms-a.test:8766/v1/rooms/room_01ABC"
    s = SessionRef.parse("https://agentd.test/v1/sessions/agt_1")
    assert (s.base_url, s.session_id) == ("https://agentd.test", "agt_1")
    for bad in ["room_1", "http://x/rooms/room_1", "http://x/v1/rooms/../y"]:
        with pytest.raises(RoomomaticError):
            RoomRef.parse(bad)


def test_tokens_are_per_audience_and_cached(rom, world):
    rooms = rom.roomsd("http://rooms-a.test")
    rooms.create_room("a")
    rooms.create_room("b")
    rom.agentd("http://agentd-1.test").spawn("t", worker_type="fake")
    assert dict(world.issued) == {"http://rooms-a.test": 1, "http://agentd-1.test": 1}
    # The lobby itself is called with the API key, services with their own tokens.
    auths = {r[1].split("/v1")[0]: r[2] for r in world.requests}
    assert auths["http://lobby.test"] == "Bearer lbk_missy"
    assert auths["http://rooms-a.test"] == "Bearer tok:http://rooms-a.test:1"


def test_token_refreshed_near_expiry(rom, world):
    world.token_ttl = 30  # inside the refresh margin, so every use refetches
    rooms = rom.roomsd("http://rooms-a.test")
    rooms.create_room("a")
    rooms.create_room("b")
    assert world.issued["http://rooms-a.test"] == 2


def test_401_triggers_one_refresh_and_retry(rom, world):
    rooms = rom.roomsd("http://rooms-a.test")
    rooms.create_room("a")
    world.revoked_tokens.add("tok:http://rooms-a.test:1")
    assert rooms.create_room("b")["room_id"] == "room_1"
    assert world.issued["http://rooms-a.test"] == 2

    world.revoked_tokens.add("tok:http://rooms-a.test:2")
    world.revoked_tokens.add("tok:http://rooms-a.test:3")
    with pytest.raises(ApiError) as e:
        rooms.create_room("c")
    assert e.value.status_code == 401
    assert world.issued["http://rooms-a.test"] == 3  # only one retry, no loop


def test_create_room_picks_a_server(rom, world):
    room = rom.create_room("r", listed=True)
    assert room["room_url"] == "http://rooms-a.test/v1/rooms/room_1"
    world.servers = []
    with pytest.raises(NoServerAvailable):
        rom.create_room("r")


def test_summon_flow(rom, world):
    s = rom.summon("http://rooms-a.test/v1/rooms/room_1", "do it", worker_type="fake")
    assert s.session_url == "http://agentd-1.test/v1/sessions/agt_1"
    assert re.fullmatch(r"missy@test/agentd-host1\.fake-[0-9a-f]{6}", s.worker_identity)
    assert s.room_url == "http://rooms-a.test/v1/rooms/room_1"

    ((_, _, _, invite),) = world.calls("POST", "/invites")
    assert invite["name"] == s.worker_identity.split("/", 1)[1]
    assert (invite["role"], invite["ttl_seconds"]) == ("implementer", None)
    ((_, _, _, spawn),) = world.calls("POST", "agentd-1.test/v1/sessions")
    assert spawn["room"] == {
        "room_url": "http://rooms-a.test/v1/rooms/room_1",
        "token": "rmsd_secret",
    }
    assert (spawn["worker_type"], spawn["profile"], spawn["task"]) == (
        "fake",
        "workspace_coder",
        "do it",
    )
    # Registry lookup asked for capacity and the right worker type.
    ((_, url, _, _),) = world.calls("GET", "/v1/registry/agentd")
    assert "worker_type=fake" in url and "has_capacity=true" in url


def test_summon_revokes_invite_when_spawn_fails(rom, world):
    world.spawn_status = 429
    with pytest.raises(ApiError) as e:
        rom.summon("http://rooms-a.test/v1/rooms/room_1", "t", worker_type="fake")
    assert e.value.status_code == 429
    assert len(world.calls("DELETE", "/v1/rooms/room_1/invites/inv_1")) == 1


def test_summon_with_no_capacity(rom, world):
    world.instances[0]["active_sessions"] = 4
    with pytest.raises(NoServerAvailable, match="free capacity"):
        rom.summon("http://rooms-a.test/v1/rooms/room_1", "t", worker_type="fake")
    assert world.calls("POST", "/invites") == []  # nothing minted


def test_summon_pinned_instance_skips_registry(rom, world):
    s = rom.summon(
        "http://rooms-a.test/v1/rooms/room_1",
        "t",
        worker_type="fake",
        instance_url="http://agentd-1.test/",
    )
    assert s.worker_identity.startswith("missy@test/agentd-pinned.fake-")
    assert world.calls("GET", "/v1/registry/agentd") == []


def test_watch_spans_servers_and_skips_history(rom, world):
    world.servers.append({"server_id": "rooms-b", "base_url": "http://rooms-b.test", "tags": []})
    world.add_message("http://rooms-a.test", "room_1", "old")
    gen = rom.watch(interval=0, once=True)
    world.add_message("http://rooms-a.test", "room_1", "a-new")
    world.add_message("http://rooms-b.test", "room_9", "b-new")
    seen = [(url, m["body"]) for url, m in gen]
    assert seen == [
        ("http://rooms-a.test/v1/rooms/room_1", "a-new"),
        ("http://rooms-b.test/v1/rooms/room_9", "b-new"),
    ]


def test_watch_from_start_replays(rom, world):
    world.add_message("http://rooms-a.test", "room_1", "old")
    seen = [m["body"] for _, m in rom.watch(from_start=True, once=True, interval=0)]
    assert seen == ["old"]


def test_stream_events_parses_sse(rom):
    agentd = rom.agentd("http://agentd-1.test")
    events = list(agentd.stream_events("agt_1"))
    assert [e["type"] for e in events] == ["status", "final"]


def test_parse_sse_multiline_and_comments():
    lines = [": hi", "id: 1", 'data: {"a":', "data: 1}", "", "", 'data: {"b": 2}', ""]
    assert list(parse_sse(iter(lines))) == [{"a": 1}, {"b": 2}]


def test_api_error_carries_detail(rom, world):
    world.spawn_status = 429
    with pytest.raises(ApiError) as e:
        rom.agentd("http://agentd-1.test").spawn("t", worker_type="fake")
    assert e.value.detail == "at capacity"
    assert "agentd-1.test/v1/sessions" in str(e.value)


def test_from_env(monkeypatch):
    from roomomatic import Client

    monkeypatch.delenv("ROM_LOBBY_URL", raising=False)
    monkeypatch.delenv("LOBBYD_URL", raising=False)
    with pytest.raises(RoomomaticError):
        Client.from_env()
    monkeypatch.setenv("ROM_LOBBY_URL", "http://lobby.test")
    monkeypatch.setenv("ROM_API_KEY", "k")
    assert Client.from_env().lobby.base_url == "http://lobby.test"


def test_transport_errors_surface(world):
    from roomomatic import Client

    def boom(req):
        raise httpx.ConnectError("refused", request=req)

    c = Client("http://lobby.test", "k", transport=httpx.MockTransport(boom))
    with pytest.raises(httpx.ConnectError):
        c.lobby.whoami()


def test_summon_default_names_are_unique(rom, world):
    url = "http://rooms-a.test/v1/rooms/room_1"
    names = {rom.summon(url, "t", worker_type="fake").worker_identity for _ in range(5)}
    assert len(names) == 5


def test_summon_explicit_name_is_kept(rom, world):
    s = rom.summon("http://rooms-a.test/v1/rooms/room_1", "t", worker_type="fake", name="rev")
    assert s.worker_identity == "missy@test/rev"


def test_peer_and_offer_requests(world):
    import json as _json

    from roomomatic import Client

    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, _json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={})

    c = Client("http://lobby.test", "k", transport=httpx.MockTransport(handler))
    c.lobby.register_peer("odin-s1", capabilities=["review"], max_assignments=2)
    c.lobby.offer(
        "odin@test", "http://rooms-a.test/v1/rooms/room_1", "t", offer_id="o1", role="reviewer"
    )
    c.lobby.accept_offer("o1", "odin-s1")
    c.lobby.progress_offer("o1", "odin-s1", "working")
    assert seen[0][:2] == ("PUT", "/v1/peers/odin-s1")
    assert seen[0][2]["capabilities"] == ["review"] and seen[0][2]["max_assignments"] == 2
    assert seen[1] == (
        "POST",
        "/v1/offers",
        {
            "to": "odin@test",
            "room_url": "http://rooms-a.test/v1/rooms/room_1",
            "task": "t",
            "offer_id": "o1",
            "role": "reviewer",
        },
    )
    assert seen[2] == ("POST", "/v1/offers/o1/accept", {"instance_id": "odin-s1"})
    assert seen[3] == (
        "POST",
        "/v1/offers/o1/progress",
        {"instance_id": "odin-s1", "state": "working"},
    )
