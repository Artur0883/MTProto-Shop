from dataclasses import dataclass


@dataclass(frozen=True)
class Tariff:
    days: int
    title: str
    price: int | None = None
    enabled: bool = True


TARIFFS: dict[int, Tariff] = {
    1: Tariff(
        days=1,
        title="🎁 Пробный — 1 день",
        price=0,
        enabled=True,
    ),
    30: Tariff(
        days=30,
        title="🗓 1 месяц — 50 ₽",
        price=50,
        enabled=True,
    ),
    90: Tariff(
        days=90,
        title="🗓 3 месяца — 130 ₽",
        price=130,
        enabled=True,
    ),
    180: Tariff(
        days=180,
        title="🗓 6 месяцев — 240 ₽",
        price=240,
        enabled=True,
    ),
    365: Tariff(
        days=365,
        title="🗓 12 месяцев — 450 ₽",
        price=450,
        enabled=True,
    ),
}


def get_tariff(days: int) -> Tariff:
    tariff = TARIFFS.get(days)
    if tariff is None or not tariff.enabled:
        raise ValueError("unknown tariff")
    return tariff


def format_tariffs() -> str:
    return "\n".join(f"• {tariff.title}" for tariff in TARIFFS.values())
