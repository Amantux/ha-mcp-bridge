"""Helpers for ha_mcp_bridge."""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from .const import DEFAULT_HEALTH_PATH


def build_health_url(host: str, port: int) -> str:
    host = host.strip()
    if not host:
        host = "http://127.0.0.1"
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    parsed = urlparse(host)
    netloc = parsed.netloc
    if ":" not in netloc:
        netloc = f"{netloc}:{port}"
    parsed = parsed._replace(netloc=netloc, path="")
    return urlunparse(parsed) + DEFAULT_HEALTH_PATH
