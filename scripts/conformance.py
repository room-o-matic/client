"""Run the peer-adapter conformance suite (roomomatic.conformance) against real lobbyd and
roomsd from the sibling checkouts, for PeerAgent. Exits non-zero on any failure.

    uv run python scripts/conformance.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e import DOMAIN, services  # noqa: E402

from roomomatic import Client, PeerAgent  # noqa: E402
from roomomatic.conformance import run_all  # noqa: E402


class RealHarness:
    def __init__(self, urls, keys):
        self.urls, self.keys = urls, keys
        self.requester = Client(urls["lobby"], keys["missy"])

    def peer_client(self) -> Client:
        return Client(self.urls["lobby"], self.keys["boostie"])

    def peer_identity(self) -> str:
        return f"boostie@{DOMAIN}"

    def new_room(self, admission: str = "open") -> str:
        return self.requester.create_room("conformance", admission=admission)["room_url"]

    def make_adapter(self, client, instance_id, **callbacks):
        return PeerAgent(client, instance_id, sleep=lambda s: None, **callbacks)


def main() -> int:
    with services() as (urls, keys, _tmp):
        results = run_all(RealHarness(urls, keys))
    for name, outcome in results:
        print(f"{'ok  ' if outcome == 'pass' else 'FAIL'} {name}: {outcome}")
    return 0 if all(o == "pass" for _, o in results) else 1


if __name__ == "__main__":
    sys.exit(main())
