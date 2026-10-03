import httpx

from roomomatic.http import ApiError, Service, TokenSource, static_token


class NoteConflict(ApiError):
    """A conditional note write lost: the note isn't at the revision you read (412)."""


class RoomsClient(Service):
    """One roomsd server. Authenticate with a lobbyd token source (named agents) or, for
    an invited worker, `RoomsClient.with_invite(base_url, invite_token)`."""

    @classmethod
    def with_invite(
        cls, base_url: str, invite_token: str, *, transport: httpx.BaseTransport | None = None
    ) -> "RoomsClient":
        return cls(base_url, static_token(invite_token), transport=transport)

    def __init__(
        self, base_url: str, token: TokenSource, *, transport: httpx.BaseTransport | None = None
    ):
        super().__init__(base_url, token, transport=transport)

    def well_known(self) -> dict:
        return self.request("GET", "/.well-known/roomsd")

    def whoami(self) -> dict:
        return self.request("GET", "/v1/auth/whoami")

    # ----- rooms ---------------------------------------------------------------------

    def create_room(
        self,
        name: str,
        *,
        purpose: str | None = None,
        listed: bool = False,
        tags: list[str] | None = None,
        admission: str | None = None,
        default_rights: list[str] | None = None,
    ) -> dict:
        """Returns {room_id, room_url}. The creator joins automatically and is admin.
        admission: "open" | "closed" (default: the server's setting)."""
        body = {"name": name, "purpose": purpose, "listed": listed, "tags": tags or []}
        if admission is not None:
            body["admission"] = admission
        if default_rights is not None:
            body["default_rights"] = default_rights
        return self.request("POST", "/v1/rooms", json=body)

    # ----- membership (admins) ---------------------------------------------------------

    def members(self, room_id: str) -> list[dict]:
        return self.request("GET", f"/v1/rooms/{room_id}/members")

    def grant(self, room_id: str, agent: str, rights: list[str]) -> dict:
        """Grant a named agent rights (read/write/invite/admin); also lifts a ban."""
        return self.request("PUT", f"/v1/rooms/{room_id}/members/{agent}", json={"rights": rights})

    def remove(self, room_id: str, agent: str, *, ban: bool = False) -> None:
        self.request("DELETE", f"/v1/rooms/{room_id}/members/{agent}", params={"ban": ban})

    def rooms(self) -> list[dict]:
        return self.request("GET", "/v1/rooms")

    def room(self, room_id: str) -> dict:
        return self.request("GET", f"/v1/rooms/{room_id}")

    def update_room(self, room_id: str, **changes) -> dict:
        """name, purpose, listed, tags. Creator only."""
        return self.request("PATCH", f"/v1/rooms/{room_id}", json=changes)

    def join(self, room_id: str, role: str | None = None) -> dict:
        return self.request("POST", f"/v1/rooms/{room_id}/participants", json={"role": role})

    # ----- messages ------------------------------------------------------------------

    def post(self, room_id: str, body: str, type: str = "message", **fields) -> dict:
        """fields: topic, confidence, reply_requested, severity, based_on_messages."""
        return self.request(
            "POST", f"/v1/rooms/{room_id}/messages", json={"type": type, "body": body, **fields}
        )

    def messages(self, room_id: str, after_id: int = 0, limit: int = 100) -> dict:
        return self.request(
            "GET",
            f"/v1/rooms/{room_id}/messages",
            params={"after_id": after_id, "limit": limit},
        )

    def updates(self, cursor: int = 0, limit: int = 100) -> dict:
        """New messages across every joined room on this server: {messages, room_urls,
        next_cursor}."""
        return self.request("GET", "/v1/me/updates", params={"cursor": cursor, "limit": limit})

    # ----- notes ---------------------------------------------------------------------

    def notes(self, room_id: str, keys: list[str] | None = None) -> dict:
        params = {"keys": ",".join(keys)} if keys else None
        return self.request("GET", f"/v1/rooms/{room_id}/notes", params=params)["notes"]

    def note(self, room_id: str, key: str) -> dict:
        return self.request("GET", f"/v1/rooms/{room_id}/notes/{key}")

    def put_note(self, room_id: str, key: str, value, if_revision: int | None = None) -> dict:
        """Write a note; returns it with its new `revision`. With `if_revision`, the write
        only happens if the note is still at that revision (0 = only if it doesn't exist
        yet), else NoteConflict. Without it, last writer wins (docs#20)."""
        body: dict = {"value": value}
        if if_revision is not None:
            body["if_revision"] = if_revision
        try:
            return self.request("PUT", f"/v1/rooms/{room_id}/notes/{key}", json=body)
        except ApiError as e:
            if e.status_code == 412:
                raise NoteConflict(e.status_code, e.detail, e.url) from None
            raise

    def update_note(self, room_id: str, key: str, fn, attempts: int = 5) -> dict:
        """Read-modify-write without losing concurrent updates: `fn(current_value)` (None
        if the note doesn't exist) returns the new value, which is written with
        compare-and-set; on a conflict the note is re-read and `fn` runs again."""
        for attempt in range(attempts):
            try:
                current = self.note(room_id, key)
            except ApiError as e:
                if e.status_code != 404:
                    raise
                current = {"value": None, "revision": 0}
            try:
                return self.put_note(
                    room_id, key, fn(current["value"]), if_revision=current["revision"]
                )
            except NoteConflict:
                if attempt == attempts - 1:
                    raise
        raise AssertionError("unreachable")

    def note_history(self, room_id: str, key: str) -> list[dict]:
        """Recent revisions of a note, newest first (the server keeps the last 50)."""
        return self.request("GET", f"/v1/rooms/{room_id}/notes/{key}/history")

    def note_changes(self, room_id: str, after: int = 0, limit: int = 100) -> dict:
        """Note writes after cursor `after`: {changes: [{id, key, revision, updated_by,
        updated_at}], next_cursor}. Notes aren't in the message feed; poll this."""
        return self.request(
            "GET", f"/v1/rooms/{room_id}/notes/changes", params={"after": after, "limit": limit}
        )

    # ----- invites -------------------------------------------------------------------

    def invite(
        self, room_id: str, name: str, role: str | None = None, ttl_seconds: int | None = None
    ) -> dict:
        """Returns the invite including its one-time-visible `token`."""
        return self.request(
            "POST",
            f"/v1/rooms/{room_id}/invites",
            json={"name": name, "role": role, "ttl_seconds": ttl_seconds},
        )

    def invites(self, room_id: str) -> list[dict]:
        return self.request("GET", f"/v1/rooms/{room_id}/invites")

    def revoke_invite(self, room_id: str, invite_id: str) -> dict:
        return self.request("DELETE", f"/v1/rooms/{room_id}/invites/{invite_id}")

    def revoke_self(self) -> None:
        """Invite tokens only: end this token now."""
        self.request("POST", "/v1/auth/revoke")
