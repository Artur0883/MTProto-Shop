"""Pure node-reconciliation diff. No I/O.

Given the users that SHOULD be on a node and the users currently present, decide
what to add and what to remove. Protected usernames (e.g. the system bootstrap
user) are never removed.
"""
from __future__ import annotations


def diff_users(
    desired: set[str], present: set[str], protected: set[str]
) -> tuple[set[str], set[str]]:
    to_add = desired - present
    to_remove = (present - desired) - protected
    return to_add, to_remove


def present_usernames(resp: object) -> set[str]:
    """Set of usernames present on a node, from a TeleMT ``GET /v1/users`` body.

    TeleMT may answer as a bare list, ``{"users": [...]}``, ``{"data": [...]}``,
    or a dict keyed by username. Accept every shape ``proxy_manager.list_clients``
    accepts, so reconciliation agrees with the rest of the bot and never deletes a
    live user just because the response was wrapped differently.
    """
    if resp is None:
        return set()
    container: object = resp
    if isinstance(resp, dict):
        for key in ("users", "data"):
            value = resp.get(key)
            if value is not None:
                container = value
                break
        else:
            container = resp
    names: set[str] = set()
    if isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                name = item.get("username") or item.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
            elif isinstance(item, str) and item:
                names.add(item)
    elif isinstance(container, dict):
        for key in container:
            if isinstance(key, str) and key:
                names.add(key)
    return names
