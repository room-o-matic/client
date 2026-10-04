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
