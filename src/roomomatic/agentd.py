import json
from collections.abc import Iterator

import httpx

from roomomatic.http import ApiError, Service, TokenSource

TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "expired"})


class AgentdClient(Service):
    """One agentd instance."""

    def __init__(
        self, base_url: str, token: TokenSource, *, transport: httpx.BaseTransport | None = None
    ):
        super().__init__(base_url, token, transport=transport)

    def session_url(self, session_id: str) -> str:
        return f"{self.base_url}/v1/sessions/{session_id}"

    def instance(self) -> dict:
        return self.request("GET", "/v1/instance")

    def spawn(
        self,
        task: str,
        *,
        worker_type: str,
        profile: str = "workspace_coder",
        room: dict | None = None,
        workspace_path: str | None = None,
        **fields,
    ) -> dict:
        """room: {"room_url", "token"}. fields: timeout_seconds, idle_timeout_seconds,
        requester, parent_session_id, metadata."""
        body = {"task": task, "worker_type": worker_type, "profile": profile, **fields}
        if room is not None:
            body["room"] = room
        if workspace_path is not None:
            body["workspace"] = {"mode": "mount", "path": workspace_path}
        return self.request("POST", "/v1/sessions", json=body)

    def session(self, session_id: str) -> dict:
        return self.request("GET", f"/v1/sessions/{session_id}")

    def sessions(self, active: bool = False) -> list[dict]:
        return self.request("GET", "/v1/sessions", params={"active": active} if active else None)

    def send(self, session_id: str, message: str) -> dict:
        return self.request(
            "POST", f"/v1/sessions/{session_id}/messages", json={"message": message}
        )

    def stop(self, session_id: str, reason: str = "caller_cancelled") -> dict:
        return self.request("POST", f"/v1/sessions/{session_id}/stop", json={"reason": reason})

    def events(self, session_id: str, after_id: int = 0) -> list[dict]:
        """Backlog as a list (polling form)."""
        return self.request(
            "GET",
            f"/v1/sessions/{session_id}/events",
            params={"stream": False, "after_id": after_id},
        )

    def stream_events(self, session_id: str, after_id: int = 0) -> Iterator[dict]:
        """Follow the SSE stream; ends when the session reaches a terminal status."""
        path = f"/v1/sessions/{session_id}/events"
        for force in (False, True):
            with self._http.stream(
                "GET",
                path,
                params={"after_id": after_id},
                headers=self._headers(force),
                timeout=httpx.Timeout(10, read=None),
            ) as r:
                if r.status_code == 401 and not force:
                    continue
                if r.status_code >= 400:
                    r.read()
                    raise ApiError.from_response(r)
                yield from parse_sse(r.iter_lines())
                return


def parse_sse(lines: Iterator[str]) -> Iterator[dict]:
    data: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            yield json.loads("\n".join(data))
            data = []
