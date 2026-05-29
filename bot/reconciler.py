"""Multi-node reconciler.

Periodically makes each proxy node's user set match the active subscriptions in
SQLite — adds users missing on a node (e.g. a node that was unreachable when the
client was issued access) and removes stale ones. Decision logic is the pure
`diff_users` (tested); this module does the I/O. No-op effect for a single node.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot

from config import get_settings
from database import get_all_active_subscriptions
from logging_setup import get_logger
from nodes import get_nodes
from reconcile_logic import diff_users
from telemt_client import api_call


logger = get_logger(__name__)


def _client_id(telegram_id: int) -> str:
    return f"tg_{telegram_id}"


def _present_usernames(api_users: object) -> set[str]:
    """Extract usernames from a TeleMT `GET /v1/users` response."""
    names: set[str] = set()
    data = api_users.get("data") if isinstance(api_users, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("username"), str):
                names.add(item["username"])
    return names


async def reconcile_once() -> None:
    settings = get_settings()
    subs = await get_all_active_subscriptions(settings.database_path)
    desired_secret = {
        _client_id(int(s["telegram_id"])): (s.get("secret") or "")
        for s in subs
        if s.get("secret")
    }
    desired = set(desired_secret)
    protected = {settings.telemt_system_user}

    for node in get_nodes():
        try:
            resp = await api_call("GET", "/v1/users", base_url=node.api_url)
        except Exception:
            logger.warning("event=reconcile_node_unreachable", node=node.name)
            continue
        present = _present_usernames(resp)
        to_add, to_remove = diff_users(desired, present, protected)
        for client_id in to_add:
            secret = desired_secret.get(client_id)
            if not secret:
                continue
            try:
                await api_call(
                    "POST",
                    "/v1/users",
                    {"username": client_id, "secret": secret},
                    base_url=node.api_url,
                )
                logger.info("event=reconcile_added", node=node.name, client_id=client_id)
            except Exception:
                logger.warning("event=reconcile_add_failed", node=node.name, client_id=client_id)
        for client_id in to_remove:
            try:
                await api_call("DELETE", f"/v1/users/{client_id}", base_url=node.api_url)
                logger.info("event=reconcile_removed", node=node.name, client_id=client_id)
            except Exception:
                logger.warning("event=reconcile_remove_failed", node=node.name, client_id=client_id)


async def reconciler_loop(bot: Bot) -> None:
    settings = get_settings()
    logger.info("event=reconciler_started", interval=settings.reconcile_interval)
    while True:
        try:
            await reconcile_once()
        except Exception:
            logger.exception("event=reconciler_loop_error")
        await asyncio.sleep(settings.reconcile_interval)
