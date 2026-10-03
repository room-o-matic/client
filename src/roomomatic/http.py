"""Shared HTTP plumbing: errors, URL parsing, and a base class that attaches a bearer token
and retries once with a fresh token on 401."""

import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from roomomatic.urls import canonical_url

ROOM_URL_RE = re.compile(r"^((?i:https?)://\S+?)/v1/rooms/([A-Za-z0-9_]{1,64})$")
SESSION_URL_RE = re.compile(r"^((?i:https?)://\S+?)/v1/sessions/([A-Za-z0-9_]{1,64})$")

# token(force_refresh) -> bearer token
TokenSource = Callable[[bool], str]


class RoomomaticError(Exception):
    pass


def service_url(url: str) -> str:
    """Canonical form of a service URL; unsafe or malformed URLs are refused before any
    credential, task or invite is sent to them (docs#5)."""
    try:
        return canonical_url(url)
    except ValueError as e:
        raise RoomomaticError(f"refusing service URL {url!r}: {e}") from None


class ApiError(RoomomaticError):
    def __init__(self, status_code: int, detail, url: str, retry_after: float | None = None):
        super().__init__(f"{status_code} from {url}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.url = url
        self.retry_after = retry_after  # seconds, from a 429/503 Retry-After header

    @classmethod
    def from_response(cls, r: httpx.Response) -> "ApiError":
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        try:
            retry_after = float(r.headers["retry-after"]) if "retry-after" in r.headers else None
        except ValueError:
            retry_after = None
        return cls(r.status_code, detail, str(r.request.url), retry_after)


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
        return cls(service_url(m.group(1)), m.group(2))


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
        return cls(service_url(m.group(1)), m.group(2))


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
        self.base_url = service_url(base_url)
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
