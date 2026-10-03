"""Client side of room-o-matic/docs#20: conditional note writes, history and changes."""

import threading

import pytest

from roomomatic import NoteConflict

ROOM = "http://rooms-a.test/v1/rooms/room_1"


@pytest.fixture
def rooms(rom):
    client, room_id = rom.room(ROOM)
    return client, room_id


def test_conditional_write_and_conflict(rooms):
    c, rid = rooms
    assert c.put_note(rid, "plan", "a", if_revision=0)["revision"] == 1
    with pytest.raises(NoteConflict) as e:
        c.put_note(rid, "plan", "b", if_revision=0)  # create-only, but it exists
    assert e.value.status_code == 412
    assert c.put_note(rid, "plan", "b", if_revision=1)["revision"] == 2
    assert c.put_note(rid, "plan", "c")["revision"] == 3  # unconditional still works


def test_update_note_retries_on_conflict(rooms):
    c, rid = rooms
    c.put_note(rid, "count", 0)
    raced = threading.Event()

    def bump(value):
        if not raced.is_set():  # another writer sneaks in between our read and write
            raced.set()
            c.put_note(rid, "count", 100)
        return value + 1

    note = c.update_note(rid, "count", bump)
    assert note["value"] == 101 and note["revision"] == 3  # nothing lost
    assert c.update_note(rid, "fresh", lambda v: (v or 0) + 1)["value"] == 1


def test_update_note_gives_up(rooms):
    c, rid = rooms
    c.put_note(rid, "hot", 0)

    def always_raced(value):
        c.put_note(rid, "hot", value)
        return value

    with pytest.raises(NoteConflict):
        c.update_note(rid, "hot", always_raced, attempts=3)


def test_history_and_changes(rooms):
    c, rid = rooms
    for v in ("A", "B", "C"):
        c.put_note(rid, "summary", v)
    assert [n["value"] for n in c.note_history(rid, "summary")] == ["C", "B", "A"]
    page = c.note_changes(rid)
    assert [(ch["key"], ch["revision"]) for ch in page["changes"]] == [
        ("summary", 1),
        ("summary", 2),
        ("summary", 3),
    ]
    assert c.note_changes(rid, after=page["next_cursor"])["changes"] == []
