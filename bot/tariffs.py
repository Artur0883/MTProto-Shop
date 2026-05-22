from dataclasses import dataclass


@dataclass(frozen=True)
class Tariff:
    days: int
    title: str


TARIFFS: dict[int, Tariff] = {
    7: Tariff(days=7, title="7 дней"),
    30: Tariff(days=30, title="30 дней"),
    90: Tariff(days=90, title="90 дней"),
}


def get_tariff(days: int) -> Tariff:
    if days not in TARIFFS:
        raise ValueError("unknown tariff")
    return TARIFFS[days]


def format_tariffs() -> str:
    return "\n".join(f"• {tariff.title}" for tariff in TARIFFS.values())
