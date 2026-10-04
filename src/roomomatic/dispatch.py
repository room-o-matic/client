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

    # ----- definitions (templates, schedules, webhooks) -----------------------------------
    # Ones from dispatchd's config file are read-only here (409); the API adds its own.

    def templates(self) -> list[dict]:
        return self.request("GET", "/v1/templates")

    def template(self, name: str) -> dict:
        return self.request("GET", f"/v1/templates/{name}")

    def put_template(self, name: str, body: dict) -> dict:
        return self.request("PUT", f"/v1/templates/{name}", json=body)

    def schedule(self, name: str) -> dict:
        return self.request("GET", f"/v1/schedules/{name}")

    def put_schedule(self, name: str, body: dict) -> dict:
        return self.request("PUT", f"/v1/schedules/{name}", json=body)

    def webhooks(self) -> list[dict]:
        return self.request("GET", "/v1/webhooks")

    def webhook(self, name: str) -> dict:
        return self.request("GET", f"/v1/webhooks/{name}")

    def create_webhook(self, name: str, body: dict) -> dict:
        """Returns the webhook with its generated `secret`, which is never shown again."""
        return self.request("POST", f"/v1/webhooks/{name}", json=body)

    def rotate_webhook_secret(self, name: str) -> dict:
        return self.request("POST", f"/v1/webhooks/{name}/rotate-secret")

    def delete(self, kind: str, name: str) -> None:
        """kind: template, schedule or webhook."""
        self.request("DELETE", f"/v1/{kind}s/{name}")
