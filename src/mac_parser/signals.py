"""Сигнали поверх оголошення: продавець, реальна ринкова ціна з проданих, перевиставлення."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .classify import age_days
from .db import DB
from .pricing import Market, effective_specs

SOLD_WINDOW_DAYS = 60    # беремо продажі за останні N днів
MIN_LIFETIME_HOURS = 12  # коротше — швидше видалене/помилкове, ніж продане
NEW_ACCOUNT_DAYS = 60
SCAM_ACCOUNT_DAYS = 30
SCAM_DISCOUNT_PCT = -20  # дешевше за ринок на 20%+ у свіжого акаунта = класична схема
RESELLER_MIN_ADS = 3


@dataclass
class Seller:
    account_days: int | None = None
    active_ads: int = 0          # скільки MacBook цей продавець зараз виставляє (у нашій базі)
    verified: bool = False
    last_seen_days: int | None = None
    flags: list[str] = field(default_factory=list)   # тривожні сигнали
    notes: list[str] = field(default_factory=list)   # нейтральні факти


@dataclass
class Sold:
    median: int | None = None
    count: int = 0
    days_to_sell: int | None = None  # медіана днів від створення до зникнення


@dataclass
class Repost:
    since: str | None = None        # найраніша дата, з якої цей ноутбук продається
    first_price: int | None = None
    by: str = ""                    # "серійник" | "продавець і заголовок"
    keys: list[str] = field(default_factory=list)


def _hours_between(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def seller_info(db: DB, row: sqlite3.Row, delta_pct: float | None) -> Seller:
    s = Seller(verified=bool(row["seller_verified"]))
    if row["seller_created"]:
        s.account_days = age_days(row["seller_created"])
    if row["seller_last_seen"]:
        s.last_seen_days = age_days(row["seller_last_seen"])
    if row["seller_id"]:
        s.active_ads = db.conn.execute(
            "SELECT COUNT(*) FROM listings WHERE seller_id=? AND active=1", (row["seller_id"],)
        ).fetchone()[0]

    if s.account_days is not None:
        years = s.account_days // 365
        s.notes.append(f"на майданчику {years} р." if years >= 1 else f"акаунт створено {s.account_days} дн. тому")
        if s.account_days < SCAM_ACCOUNT_DAYS and delta_pct is not None and delta_pct <= SCAM_DISCOUNT_PCT:
            s.flags.append(f"Новий акаунт ({s.account_days} дн.) і ціна на {abs(round(delta_pct))}% нижча за ринок — "
                           "типова схема шахрайства: тільки особиста зустріч, жодних передоплат")
        elif s.account_days < NEW_ACCOUNT_DAYS:
            s.flags.append(f"Акаунт продавця створено лише {s.account_days} дн. тому")
    if s.active_ads >= RESELLER_MIN_ADS and not row["is_business"]:
        s.flags.append(f"У продавця {s.active_ads} MacBook в оголошеннях — найімовірніше перекуп, хоч і не як магазин")
    elif s.active_ads > 1:
        s.notes.append(f"{s.active_ads} MacBook в оголошеннях")
    if s.verified:
        s.notes.append("верифікований")
    if s.last_seen_days is not None and s.last_seen_days >= 14:
        s.flags.append(f"Продавець не заходив на майданчик {s.last_seen_days} дн. — оголошення може бути неактуальним")
    return s


def sold_market(db: DB, chip: str | None, ram: int | None, ssd: int | None, screen: str) -> Sold:
    """Медіана цін оголошень такої ж конфігурації, що зникли за останні 60 днів (≈ продані)."""
    if not chip:
        return Sold()
    rows = db.conn.execute(
        """SELECT price_uah, first_seen, created_at, gone_at FROM listings
           WHERE active=0 AND gone_at IS NOT NULL AND reject_reason IS NULL AND price_uah IS NOT NULL
             AND julianday('now') - julianday(gone_at) <= ?
             AND COALESCE(json_extract(analysis,'$.chip'), chip)=?
             AND COALESCE(json_extract(analysis,'$.ram_gb'), ram_gb) IS ?
             AND COALESCE(json_extract(analysis,'$.ssd_gb'), ssd_gb) IS ?
             AND COALESCE(screen,'13')=?
             AND (analysis IS NULL OR json_extract(analysis,'$.is_relevant') != 0)""",
        (SOLD_WINDOW_DAYS, chip, ram, ssd, screen or "13"),
    ).fetchall()
    prices, lifetimes = [], []
    for r in rows:
        seen_for = _hours_between(r["first_seen"], r["gone_at"])
        if seen_for is not None and seen_for < MIN_LIFETIME_HOURS:
            continue
        prices.append(r["price_uah"])
        life = _hours_between(r["created_at"], r["gone_at"])
        if life is not None and life >= 0:
            lifetimes.append(life / 24)
    if not prices:
        return Sold()
    return Sold(
        median=round(statistics.median(prices)),
        count=len(prices),
        days_to_sell=round(statistics.median(lifetimes)) if lifetimes else None,
    )


def sold_for_row(db: DB, row: sqlite3.Row) -> Sold:
    return sold_market(db, *effective_specs(row))


_NORM_RE = re.compile(r"[^a-zа-яіїєґ0-9]+")


def _norm_title(t: str) -> str:
    return _NORM_RE.sub(" ", (t or "").lower()).strip()


def repost_info(db: DB, row: sqlite3.Row) -> Repost | None:
    """Той самий ноутбук уже продавався раніше: за серійником або (продавець + той самий заголовок)."""
    matches: list[sqlite3.Row] = []
    by = ""
    if row["serial"]:
        matches = db.conn.execute(
            "SELECT key, created_at, first_seen, price_history FROM listings WHERE serial=? AND key!=?",
            (row["serial"], row["key"]),
        ).fetchall()
        by = "серійник"
    if not matches and row["seller_id"]:
        # Лише з оголошеннями, що вже зникли: магазин може одночасно тримати кілька однакових «M1 8/256» —
        # це не перевиставлення. Перевиставлення — це «зняв і виставив знову».
        same_seller = db.conn.execute(
            "SELECT key, title, created_at, first_seen, price_history FROM listings "
            "WHERE seller_id=? AND key!=? AND active=0",
            (row["seller_id"], row["key"]),
        ).fetchall()
        mine = _norm_title(row["title"])
        matches = [m for m in same_seller if _norm_title(m["title"]) == mine]
        by = "продавець і заголовок"
    if not matches:
        return None
    def started(r) -> str:
        return min(x for x in (r["created_at"], r["first_seen"]) if x)

    earliest = min(matches, key=started)
    mine_start = min(x for x in (row["created_at"], row["first_seen"]) if x)
    if started(earliest) >= mine_start:
        return None  # це оголошення і так найстаріше
    history = json.loads(earliest["price_history"] or "[]")
    return Repost(
        since=started(earliest),
        first_price=history[0][1] if history else None,
        by=by,
        keys=[m["key"] for m in matches],
    )


def market_note(market: Market, sold: Sold) -> str | None:
    """Коротко для картки: просять vs реально продають."""
    if sold.count < 3 or not sold.median:
        return None
    text = f"продані схожі: ~{sold.median:,} грн ({sold.count})".replace(",", " ")
    if sold.days_to_sell is not None:
        text += f", зазвичай за {sold.days_to_sell} дн."
    return text



def ai_context(db: DB, row: sqlite3.Row, market: Market) -> str:
    """Додатковий контекст для AI: продавець, реальні продажі, перевиставлення."""
    lines = []
    seller = seller_info(db, row, market.delta_pct(row["price_uah"]))
    if seller.account_days is not None or seller.active_ads:
        lines.append(
            f"Продавець: акаунт створено {seller.account_days if seller.account_days is not None else '?'} дн. тому, "
            f"MacBook в оголошеннях: {seller.active_ads}, верифікований: {'так' if seller.verified else 'ні'}."
        )
    sold = sold_for_row(db, row)
    if sold.count >= 3:
        lines.append(f"Реально продані за 60 дн. (такі ж конфігурації): медіана {sold.median} грн, {sold.count} шт., "
                     f"продаються в середньому за {sold.days_to_sell if sold.days_to_sell is not None else '?'} дн.")
    rp = repost_info(db, row)
    if rp:
        lines.append(f"Цей ноутбук уже продавався раніше (збіг: {rp.by}) з {rp.since[:10]} за {rp.first_price} грн.")
    return "\n".join(lines)


def card_extra(db: DB, row: sqlite3.Row, market: Market) -> dict:
    """Те саме, що на сайті, — для тексту картки в Telegram."""
    seller = seller_info(db, row, market.delta_pct(row["price_uah"]))
    sold = sold_for_row(db, row)
    rp = repost_info(db, row)
    return {
        "seller_flags": seller.flags,
        "seller_notes": seller.notes,
        "sold": market_note(market, sold),
        "repost": (f"перевиставлено: продається з {rp.since[:10]}"
                   + (f" (тоді {rp.first_price:,} грн)".replace(",", " ") if rp.first_price else "")) if rp else None,
    }
