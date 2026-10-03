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

    # ----- peers and offers (docs#7) ------------------------------------------------------

    def register_peer(
        self,
        instance_id: str,
        *,
        owner: str | None = None,
        capabilities: list[str] | None = None,
        availability: str = "available",
        max_assignments: int = 1,
        ttl_seconds: int | None = None,
    ) -> dict:
        """Register or heartbeat this session as a peer. Use one instance_id per running
        session; keep calling it more often than ttl_seconds."""
        body = {
            "owner": owner,
            "capabilities": capabilities or [],
            "availability": availability,
            "max_assignments": max_assignments,
            "ttl_seconds": ttl_seconds,
        }
        return self.request("PUT", f"/v1/peers/{instance_id}", json=body)

    def deregister_peer(self, instance_id: str) -> None:
        self.request("DELETE", f"/v1/peers/{instance_id}")

    def peers(
        self,
        capability: str | None = None,
        principal: str | None = None,
        available: bool = False,
    ) -> list[dict]:
        return self.request(
            "GET",
            "/v1/peers",
            params=_params(capability=capability, principal=principal, available=available),
        )

    def offer(
        self, to: str, room_url: str, task: str, *, offer_id: str | None = None, **fields
    ) -> dict:
        """Offer room work to a named agent. fields: issue, role, scope, budget,
        deadline_seconds. Pass offer_id to make retries idempotent."""
        body = {"to": to, "room_url": room_url, "task": task, "offer_id": offer_id, **fields}
        return self.request("POST", "/v1/offers", json=body)

    def offers(self, state: str | None = None) -> list[dict]:
        return self.request("GET", "/v1/offers", params=_params(state=state))

    def get_offer(self, offer_id: str) -> dict:
        return self.request("GET", f"/v1/offers/{offer_id}")

    def inbox(self, instance_id: str) -> list[dict]:
        """Open offers to this agent plus this session's unfinished assignments."""
        return self.request("GET", f"/v1/peers/{instance_id}/inbox")

    def accept_offer(self, offer_id: str, instance_id: str) -> dict:
        return self.request(
            "POST", f"/v1/offers/{offer_id}/accept", json={"instance_id": instance_id}
        )

    def decline_offer(self, offer_id: str, instance_id: str, reason: str | None = None) -> dict:
        return self.request(
            "POST",
            f"/v1/offers/{offer_id}/decline",
            json={"instance_id": instance_id, "reason": reason},
        )

    def progress_offer(self, offer_id: str, instance_id: str, state: str) -> dict:
        """state: joined | working | handed_off | completed. Returns {offer, changed}; only
        start work when changed is true (a repeat after reconnect returns false)."""
        return self.request(
            "POST",
            f"/v1/offers/{offer_id}/progress",
            json={"instance_id": instance_id, "state": state},
        )

    def cancel_offer(self, offer_id: str) -> dict:
        return self.request("POST", f"/v1/offers/{offer_id}/cancel")


def _params(**kw) -> dict:
    return {k: v for k, v in kw.items() if v not in (None, False)}
