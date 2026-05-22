from dataclasses import dataclass


@dataclass(frozen=True)
class Tariff:
    days: int
    title: str


TARIFFS: dict[int, Tariff] = {
    30: Tariff(days=30, title="🗓 1 месяц"),
    90: Tariff(days=90, title="🗓 3 месяца"),
    180: Tariff(days=180, title="🗓 6 месяцев"),
    365: Tariff(days=365, title="🗓 12 месяцев"),
}


def get_tariff(days: int) -> Tariff:
    if days not in TARIFFS:
        raise ValueError("unknown tariff")
    return TARIFFS[days]


def format_tariffs() -> str:
    return "\n".join(f"• {tariff.title}" for tariff in TARIFFS.values())
