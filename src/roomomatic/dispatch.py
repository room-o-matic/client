"""Client for dispatchd's operator API (scheduled and webhook-triggered rooms).

Operators use their usual lobbyd identity: the token's audience is dispatchd's base URL,
which lobbyd only mints for once that URL is approved with a `service` key.
"""

from roomomatic.http import Service


class DispatchClient(Service):
    def runs(self, source: str | None = None, name: str | None = None, limit: int = 20):
        params = {k: v for k, v in (("source", source), ("name", name)) if v}
        return self.request("GET", "/v1/runs", params={**params, "limit": limit})

    def run(self, run_id: str) -> dict:
        return self.request("GET", f"/v1/runs/{run_id}")

    def schedules(self) -> list[dict]:
        return self.request("GET", "/v1/schedules")

    def trigger(self, schedule: str) -> dict:
        return self.request("POST", f"/v1/schedules/{schedule}/run")
