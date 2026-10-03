"""Shared HTTP plumbing: errors, URL parsing, and a base class that attaches a bearer token
and retries once with a fresh token on 401."""

import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx

ROOM_URL_RE = re.compile(r"^(https?://\S+?)/v1/rooms/([A-Za-z0-9_]{1,64})$")
SESSION_URL_RE = re.compile(r"^(https?://\S+?)/v1/sessions/([A-Za-z0-9_]{1,64})$")

# token(force_refresh) -> bearer token
TokenSource = Callable[[bool], str]


class RoomomaticError(Exception):
    pass


class ApiError(RoomomaticError):
    def __init__(self, status_code: int, detail, url: str):
        super().__init__(f"{status_code} from {url}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.url = url

    @classmethod
    def from_response(cls, r: httpx.Response) -> "ApiError":
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        return cls(r.status_code, detail, str(r.request.url))


@dataclass(frozen=True)
class RoomRef:
    base_url: str
    room_id: str

    @property
    def url(self) -> str:
        return f"{self.base_url}/v1/rooms/{self.room_id}"

    @classmethod
    def parse(cls, room_url: str) -> "RoomRef":
        m = ROOM_URL_RE.match(room_url.rstrip("/"))
        if not m:
            raise RoomomaticError(f"not a room URL: {room_url!r} (want <roomsd>/v1/rooms/<id>)")
        return cls(m.group(1), m.group(2))


@dataclass(frozen=True)
class SessionRef:
    base_url: str
    session_id: str

    @property
    def url(self) -> str:
        return f"{self.base_url}/v1/sessions/{self.session_id}"

    @classmethod
    def parse(cls, session_url: str) -> "SessionRef":
        m = SESSION_URL_RE.match(session_url.rstrip("/"))
        if not m:
            raise RoomomaticError(
                f"not a session URL: {session_url!r} (want <agentd>/v1/sessions/<id>)"
            )
        return cls(m.group(1), m.group(2))


def static_token(token: str) -> TokenSource:
    """For a token that can't be refreshed, such as a roomsd invite."""
    return lambda _force: token


class Service:
    def __init__(
        self,
        base_url: str,
        token: TokenSource,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self, force: bool) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(force)}"}

    def request(self, method: str, path: str, **kw):
        r = self._http.request(method, path, headers=self._headers(False), **kw)
        if r.status_code == 401:
            # Access tokens are short-lived; one retry with a fresh token.
            r = self._http.request(method, path, headers=self._headers(True), **kw)
        if r.status_code >= 400:
            raise ApiError.from_response(r)
        return r.json() if r.content else None
