"""Ринкова ціна: медіана схожих активних оголошень (чіп + RAM + SSD + діагональ)."""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass

from .db import DB


@dataclass
class Market:
    median: int | None
    count: int
    low: int | None  # 25-й перцентиль
    high: int | None  # 75-й перцентиль

    def delta_pct(self, price: int | None) -> float | None:
        if not price or not self.median or self.count < 3:
            return None
        return (price - self.median) / self.median * 100


def effective_specs(row: sqlite3.Row) -> tuple[str | None, int | None, int | None, str]:
    """Характеристики: AI-версія, якщо є, інакше з регулярок."""
    a = json.loads(row["analysis"]) if row["analysis"] else {}
    return (
        a.get("chip") or row["chip"],
        a.get("ram_gb") or row["ram_gb"],
        a.get("ssd_gb") or row["ssd_gb"],
        row["screen"] or "13",
    )


def market_for(db: DB, chip: str | None, ram: int | None, ssd: int | None, screen: str) -> Market:
    if not chip:
        return Market(None, 0, None, None)
    prices = sorted(db.comparable_prices(chip, ram, ssd, screen))
    if not prices:
        return Market(None, 0, None, None)
    if len(prices) >= 8:  # відрізаємо крайні 10% з кожного боку від скаму та «магазинних» цін
        cut = len(prices) // 10
        prices = prices[cut: len(prices) - cut]
    q = statistics.quantiles(prices, n=4) if len(prices) >= 2 else [prices[0]] * 3
    return Market(round(statistics.median(prices)), len(prices), round(q[0]), round(q[2]))


def market_for_row(db: DB, row: sqlite3.Row) -> Market:
    return market_for(db, *effective_specs(row))
