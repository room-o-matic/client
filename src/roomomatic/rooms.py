import httpx

from roomomatic.http import Service, TokenSource, static_token


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
    ) -> dict:
        """Returns {room_id, room_url}. The creator joins automatically."""
        return self.request(
            "POST",
            "/v1/rooms",
            json={"name": name, "purpose": purpose, "listed": listed, "tags": tags or []},
        )

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

    def put_note(self, room_id: str, key: str, value) -> dict:
        return self.request("PUT", f"/v1/rooms/{room_id}/notes/{key}", json={"value": value})

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
