"""Helpers for ha_mcp_bridge."""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from .const import DEFAULT_HEALTH_PATH, DEFAULT_STATUS_PATH


def build_url(host: str, port: int, path: str) -> str:
    """Build a full URL from host, port and path."""
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    parsed = urlparse(host)
    netloc = parsed.netloc
    if ":" not in netloc:
        netloc = f"{netloc}:{port}"
    return urlunparse(parsed._replace(netloc=netloc, path=path))


def build_health_url(host: str, port: int) -> str:
    return build_url(host, port, DEFAULT_HEALTH_PATH)


def build_status_url(host: str, port: int) -> str:
    return build_url(host, port, DEFAULT_STATUS_PATH)
