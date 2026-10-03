# Copied from room-o-matic/lobby src/lobbyd/urls.py. Keep the copies in sync.
"""Canonical service URLs (room-o-matic/docs#5).

A service endpoint is compared and stored only in canonical form, so equivalent spellings
of one URL can't be registered as different endpoints, and unsafe forms are rejected
before any credential is minted for them. Copied into the client
(roomomatic/urls.py); keep the copies in sync.

Rules: http or https only; lowercase host; default port dropped; no userinfo, query,
fragment, percent-encoding, backslashes, empty segments or dot segments; no trailing
slash. Private and loopback hosts stay allowed for single-operator development.
"""

from urllib.parse import urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_url(url: str) -> str:
    """The canonical form of a service base URL. Raises ValueError if it's not acceptable."""
    if not isinstance(url, str) or not url or any(c in url for c in "\\%\t\r\n "):
        raise ValueError("URL must be plain text without spaces, backslashes or % escapes")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise ValueError(f"unparseable URL: {e}") from None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValueError("URL scheme must be http or https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("URL must not contain userinfo")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("URL must not contain a query or fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("URL must have a host")
    path = parts.path.rstrip("/")
    if path:
        segments = path.split("/")[1:]
        if any(seg in ("", ".", "..") for seg in segments):
            raise ValueError("URL path must not contain empty or dot segments")
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return f"{scheme}://{netloc}{path}"
