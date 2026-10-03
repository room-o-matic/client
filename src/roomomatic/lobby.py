import threading
import time

import httpx

from roomomatic.http import Service, TokenSource, service_url

# Refresh an access token when less than this many seconds of life remain.
REFRESH_MARGIN_SECONDS = 60


class Lobby(Service):
    """lobbyd: exchanges the API key for per-service access tokens, and reads the directory.

    lobbyd's own endpoints take the API key directly.
    """

    def __init__(self, url: str, api_key: str, *, transport: httpx.BaseTransport | None = None):
        super().__init__(url, lambda _force: api_key, transport=transport)
        self._tokens: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    # ----- identity ------------------------------------------------------------------

    def whoami(self) -> dict:
        return self.request("GET", "/v1/whoami")

    def token(self, audience: str, *, force: bool = False) -> str:
        """An access token valid only at `audience` (a service base URL), cached until
        close to expiry."""
        audience = service_url(audience)
        with self._lock:
            cached = self._tokens.get(audience)
            if cached and not force and cached[1] - time.time() > REFRESH_MARGIN_SECONDS:
                return cached[0]
            resp = self.request("POST", "/v1/token", json={"audience": audience})
            self._tokens[audience] = (resp["access_token"], resp["expires_at"])
            return resp["access_token"]

    def token_source(self, audience: str) -> TokenSource:
        return lambda force: self.token(audience, force=force)

    # ----- directory -----------------------------------------------------------------

    def roomsd_servers(self, tag: str | None = None) -> list[dict]:
        return self.request("GET", "/v1/servers/roomsd", params=_params(tag=tag))

    def agentd_instances(
        self,
        worker_type: str | None = None,
        profile: str | None = None,
        has_capacity: bool = False,
    ) -> list[dict]:
        """Live instances, most spare capacity first."""
        return self.request(
            "GET",
            "/v1/registry/agentd",
            params=_params(worker_type=worker_type, profile=profile, has_capacity=has_capacity),
        )

    def listed_rooms(
        self, q: str | None = None, tag: str | None = None, server_id: str | None = None
    ) -> list[dict]:
        return self.request("GET", "/v1/rooms", params=_params(q=q, tag=tag, server_id=server_id))


def _params(**kw) -> dict:
    return {k: v for k, v in kw.items() if v not in (None, False)}
