"""Configured proxy nodes. One node = today's single-server behavior.

`get_nodes()` returns the list of proxy nodes the bot manages. When `PROXY_NODES`
is unset it falls back to a single node derived from `SERVER_HOST` + `TELEMT_API_URL`,
so existing single-server deployments behave exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import get_settings


@dataclass(frozen=True)
class ProxyNode:
    name: str
    public_host: str
    api_url: str
    primary: bool


def get_nodes() -> list[ProxyNode]:
    settings = get_settings()
    raw = settings.proxy_nodes or []
    if raw:
        nodes = [
            ProxyNode(
                name=str(n.get("name") or "node"),
                public_host=str(n["public_host"]),
                api_url=str(n["api_url"]).rstrip("/"),
                primary=bool(n.get("primary", False)),
            )
            for n in raw
        ]
    else:
        nodes = [
            ProxyNode(
                name="default",
                public_host=settings.server_host,
                api_url=settings.telemt_api_url,
                primary=True,
            )
        ]
    if nodes and not any(node.primary for node in nodes):
        first = nodes[0]
        nodes[0] = ProxyNode(first.name, first.public_host, first.api_url, True)
    return nodes


def primary_node(nodes: list[ProxyNode]) -> ProxyNode:
    for node in nodes:
        if node.primary:
            return node
    return nodes[0]
