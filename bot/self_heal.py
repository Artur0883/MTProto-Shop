"""Background self-heal loop.

Detects a blocked masking domain (via the TLS picker probes) and switches the
advertised primary to a healthy one, with guardrails and human-readable admin
reporting. Decision brain lives in self_heal_logic (pure, tested); this module
gathers the snapshot, executes the action, and messages the admin.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
import time

from aiogram import Bot

from config import get_settings
from logging_setup import get_logger
from self_heal_logic import DomainHealth, SelfHealState, decide
from tls_domains import get_picker


logger = get_logger(__name__)


@dataclass
class _Runtime:
    last_switch_at: float | None = None
    switches_today: int = 0
    switch_day: date = field(default_factory=date.today)
    last_escalation: str | None = None


def _build_state(rt: _Runtime) -> SelfHealState:
    settings = get_settings()
    picker = get_picker()
    domains = tuple(
        DomainHealth(
            domain=s.domain,
            probed=s.last_probed_at is not None,
            last_ok=s.last_ok,
            success_rate=s.success_rate,
        )
        for s in picker.ranked()
    )
    return SelfHealState(
        configured_primary=settings.tls_domain,
        active_primary=picker.active_primary,
        domains=domains,
        now=time.monotonic(),
        last_switch_at=rt.last_switch_at,
        switches_today=rt.switches_today,
        switch_cooldown=settings.self_heal_switch_cooldown,
        max_switches_per_day=settings.self_heal_max_switches_per_day,
    )


async def _notify(bot: Bot, text: str) -> None:
    settings = get_settings()
    if settings.admin_id is None:
        return
    try:
        await bot.send_message(settings.admin_id, text)
    except Exception:
        logger.exception("event=self_heal_notify_failed")


def _escalation_text(kind: str) -> str:
    if kind == "all_domains_down":
        return (
            "❌ Все маскировочные домены недоступны с сервера — сам исправить не могу.\n\n"
            "Что сделать:\n"
            "1) Откройте меню: <code>mtp</code>\n"
            "2) Пункт 25 — проверьте кандидатов TLS-доменов с этого сервера\n"
            "3) Добавьте 2–3 новых рабочих домена\n"
            "4) Пересоздайте прокси (пункт обновления/перезапуска)\n\n"
            "Если домены с сервера живы, но клиенты не подключаются — возможно, "
            "заблокирован IP сервера: смените IP у хостера или поднимите прокси "
            "на новом IP."
        )
    if kind == "switch_limit_reached":
        return (
            "⚠️ Домен снова деградировал, но дневной лимит авто-переключений "
            "исчерпан.\n\nНужно внимание: проверьте список доменов "
            "(<code>mtp</code> → 25) и при необходимости добавьте новые рабочие домена."
        )
    return f"⚠️ Self-heal: {kind}"


async def self_heal_loop(bot: Bot) -> None:
    settings = get_settings()
    rt = _Runtime()
    logger.info("event=self_heal_started", interval=settings.self_heal_check_interval)
    while True:
        try:
            today = date.today()
            if today != rt.switch_day:
                rt.switch_day = today
                rt.switches_today = 0

            state = _build_state(rt)
            decision = decide(state)
            picker = get_picker()
            advertised = state.active_primary or state.configured_primary

            if decision.action == "keep":
                rt.last_escalation = None

            elif decision.action == "switch":
                picker.set_active_primary(decision.target)
                rt.last_switch_at = state.now
                rt.switches_today += 1
                rt.last_escalation = None
                logger.warning(
                    "event=self_heal_switch", frm=advertised, to=decision.target
                )
                await _notify(
                    bot,
                    f"⚠️ Домен <code>{advertised}</code> перестал отвечать. "
                    f"Переключил выдачу ссылок на <code>{decision.target}</code> — "
                    f"новые ссылки уже рабочие. Слежу дальше.",
                )

            elif decision.action == "recover":
                picker.clear_active_primary()
                rt.last_escalation = None
                logger.info("event=self_heal_recover", to=decision.target)
                await _notify(
                    bot,
                    f"✅ Домен <code>{decision.target}</code> снова стабилен. "
                    f"Вернул его как основной.",
                )

            elif decision.action == "revert_escalate":
                picker.clear_active_primary()
                logger.error(
                    "event=self_heal_revert_escalate", kind=decision.escalation
                )
                if rt.last_escalation != decision.escalation:
                    rt.last_escalation = decision.escalation
                    await _notify(bot, _escalation_text(decision.escalation))

            elif decision.action == "escalate":
                logger.error("event=self_heal_escalate", kind=decision.escalation)
                if rt.last_escalation != decision.escalation:
                    rt.last_escalation = decision.escalation
                    await _notify(bot, _escalation_text(decision.escalation))

        except Exception:
            logger.exception("event=self_heal_loop_error")
        await asyncio.sleep(settings.self_heal_check_interval)
