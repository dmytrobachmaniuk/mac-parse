"""Один прохід: OLX → фільтр → БД → Gemini."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from .analyzer import PROMPT_VERSION, Analyzer, QuotaExceeded, is_fresh
from .classify import classify, to_uah
from .config import settings
from .db import DB
from .pricing import market_for_row
from .sources import olx

log = logging.getLogger(__name__)

# Кілька запитів, бо частина продавців не пише чіп у заголовку («MacBook Air 2020 8/256»).
QUERIES = ["macbook air m1", "macbook air m2", "macbook air 2020", "macbook air 2022", "macbook air 15 2023"]


@dataclass
class ScanReport:
    fetched: int = 0
    new: int = 0
    price_changed: int = 0
    passed_filter: int = 0
    deactivated: int = 0
    analyzed: int = 0
    errors: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [
            f"Знайдено на OLX: {self.fetched}",
            f"Нових: {self.new}, змінили ціну: {self.price_changed}",
            f"Пройшли фільтр (Air M1/M2): {self.passed_filter}",
            f"Зникли з OLX: {self.deactivated}",
            f"Проаналізовано AI: {self.analyzed}",
        ]
        if self.errors:
            lines.append("Помилки: " + "; ".join(self.errors[:3]))
        return "\n".join(lines)


class Scanner:
    def __init__(self, db: DB):
        self.db = db
        self.http = olx.make_client()
        self.analyzer: Analyzer | None = None
        if settings.gemini_api_key:
            self.analyzer = Analyzer(self.http)
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def scrape(self, report: ScanReport) -> None:
        listings: dict[str, olx.Listing] = {}
        complete = True
        for q in QUERIES:
            try:
                for item in await olx.fetch_query(self.http, q):
                    listings.setdefault(item.key, item)
            except (olx.OlxError, httpx.HTTPError) as e:
                complete = False
                report.errors.append(str(e) or e.__class__.__name__)
                log.exception("OLX «%s» не вдалося", q)

        report.fetched = len(listings)
        for item in listings.values():
            uah = to_uah(item.price, item.currency, settings.usd_rate)
            specs = classify(item, settings.min_price_uah, settings.max_price_uah, uah, settings.max_age_days)
            status = self.db.upsert(item, specs, uah)
            report.new += status == "new"
            report.price_changed += status == "price_changed"
            report.passed_filter += specs.reject_reason is None
        if complete and listings:
            report.deactivated = self.db.deactivate_missing("olx", set(listings))

    async def analyze_pending(self, report: ScanReport, limit: int | None = None) -> None:
        if not self.analyzer:
            return
        # Одна відповідь Gemini ~20 с, тому кілька запитів паралельно; RPM тримає лімітер в Analyzer.
        sem = asyncio.Semaphore(3)
        quota_hit = asyncio.Event()

        async def one(row) -> None:
            async with sem:
                if quota_hit.is_set():
                    return
                # Оголошення могли проаналізувати, поки чекали в черзі (наприклад, відкрили в боті).
                fresh = self.db.get(row["key"])
                if fresh is not None and is_fresh(fresh):
                    return
                try:
                    result = await self.analyzer.analyze(row, market_for_row(self.db, row))
                except QuotaExceeded as e:
                    if not quota_hit.is_set():
                        report.errors.append("ліміт Gemini вичерпано, решту доаналізую наступного разу")
                        log.warning("Gemini quota: %s", e)
                    quota_hit.set()
                    return
                except Exception as e:  # noqa: BLE001 — одне криве оголошення не має зупиняти прохід
                    report.errors.append(f"{row['ext_id']}: {e.__class__.__name__}")
                    log.exception("Аналіз %s не вдався", row["key"])
                    return
                self.db.save_analysis(row["key"], result, row["price_uah"])
                report.analyzed += 1

        rows = self.prioritize(self.db.needs_analysis(10_000, PROMPT_VERSION))
        rows = rows[: limit or settings.max_analyze_per_scan]
        await asyncio.gather(*(one(r) for r in rows))

    def prioritize(self, rows: list) -> list:
        """Квота Gemini обмежена, тож спершу — ще не проаналізовані й найдешевші відносно ринку."""

        def key(row) -> tuple:
            delta = market_for_row(self.db, row).delta_pct(row["price_uah"])
            return (row["analysis"] is not None, delta if delta is not None else 0.0)

        return sorted(rows, key=key)

    async def run(self) -> ScanReport:
        async with self._lock:
            report = ScanReport()
            await self.scrape(report)
            await self.analyze_pending(report)
            if self.db.meta_get("initialized") is None:
                # Перший прохід наповнює базу — не засипаємо сповіщеннями про все підряд.
                self.db.mark_all_notified()
                self.db.meta_set("initialized", "1")
            log.info("Скан завершено:\n%s", report.text())
            return report

    async def close(self) -> None:
        await self.http.aclose()
