"""`rom tail` / `rom watch` show the typed fields that change how a message reads."""

from roomomatic.cli import fmt_message


def test_plain_message_is_unchanged():
    m = {"id": 1, "from": "a@x", "type": "message", "body": "hi"}
    assert fmt_message(None, m) == "[1] a@x message: hi"


def test_typed_fields_are_shown():
    m = {
        "id": 5,
        "from": "me@local/reviewer",
        "type": "answer",
        "topic": "polling",
        "body": "5-10s with backoff",
        "in_reply_to": 4,
        "to": ["boostie@local"],
        "confidence": 0.7,
        "severity": "high",
        "reply_requested": True,
    }
    assert fmt_message("http://r/v1/rooms/x", m) == (
        "http://r/v1/rooms/x [5] me@local/reviewer answer (polling) "
        "[re:#4 to:boostie@local conf=0.7 severity=high reply-requested]: 5-10s with backoff"
    )


def test_say_threads_and_addresses(monkeypatch, capsys):
    from roomomatic import cli

    posted = {}

    class Rooms:
        def post(self, room_id, body, type="message", **fields):
            posted.update(room_id=room_id, body=body, type=type, **fields)
            return {"id": 9, "from": "me@x", "type": type, "body": body, **fields}

    class Rom:
        def room(self, url):
            return Rooms(), "room_1"

    args = cli.build_parser().parse_args(
        [
            "say",
            "http://r/v1/rooms/room_1",
            "agreed",
            "--type",
            "answer",
            "--reply-to",
            "4",
            "--to",
            "a@x",
            "--to",
            "b@x",
            "--reply-requested",
        ]
    )
    cli.cmd_say(Rom(), args)
    assert posted == {
        "room_id": "room_1",
        "body": "agreed",
        "type": "answer",
        "in_reply_to": 4,
        "to": ["a@x", "b@x"],
        "reply_requested": True,
    }
    assert "[re:#4 to:a@x,b@x reply-requested]" in capsys.readouterr().out
