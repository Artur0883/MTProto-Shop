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
