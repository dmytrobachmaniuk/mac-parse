"""Статичний сайт-каталог для GitHub Pages: index.html + data.json.

Сторінка сама малює картки з data.json (розділи «Найкраще / M1 / M2», сортування),
тож оновлення каталогу — це просто новий data.json після кожного скану.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .classify import age_days
from .config import settings
from .db import DB
from .pricing import effective_specs, market_for_row
from .signals import repost_info, seller_info, sold_for_row

TEMPLATE = Path(__file__).with_name("site_template.html")


def listing_json(db: DB, row) -> dict:
    a = json.loads(row["analysis"]) if row["analysis"] else {}
    chip, ram, ssd, screen = effective_specs(row)
    market = market_for_row(db, row)
    delta = market.delta_pct(row["price_uah"])
    history = json.loads(row["price_history"] or "[]")
    first_price = history[0][1] if history and history[0][1] != row["price_uah"] else None
    seller = seller_info(db, row, delta)
    sold = sold_for_row(db, row)
    repost = repost_info(db, row)
    return {
        "key": row["key"],
        "source": row["source"],
        "url": row["url"],
        "title": row["title"],
        "chip": chip,
        "ram": ram,
        "ssd": ssd,
        "screen": f'{a["screen_inch"]:g}"' if a.get("screen_inch") else f'{screen}"',
        "color": a.get("color"),
        "price": row["price_uah"],
        "orig_price": f"{row['price']} {row['currency']}" if row["currency"] != "UAH" else None,
        "first_price": first_price,
        "negotiable": bool(row["negotiable"]),
        "median": market.median if delta is not None else None,
        "similar": market.count,
        "delta": round(delta, 1) if delta is not None else None,
        "city": row["city"],
        "business": bool(row["is_business"]) or a.get("seller_type", "").startswith("магазин"),
        "seller": row["seller_name"],
        "created": row["created_at"],
        "created_days": age_days(row["created_at"]),
        "refreshed": row["refreshed_at"],
        "first_seen": row["first_seen"],
        "photos": json.loads(row["photos"])[:10],
        "seller_info": {
            "account_days": seller.account_days,
            "active_ads": seller.active_ads,
            "verified": seller.verified,
            "flags": seller.flags,
            "notes": seller.notes,
        },
        "sold": {"median": sold.median, "count": sold.count, "days": sold.days_to_sell} if sold.count >= 3 else None,
        "repost": {"since": repost.since, "first_price": repost.first_price, "by": repost.by} if repost else None,
        "battery_pct": a.get("battery_health_pct") or row["battery_pct"],
        "battery_cycles": a.get("battery_cycles") or row["battery_cycles"],
        "battery_source": a.get("battery_source"),
        "ai": {
            k: a.get(k)
            for k in (
                "deal_score", "price_verdict", "condition_score", "summary", "photo_facts", "visual_defects",
                "kit", "pros", "red_flags", "questions_to_seller",
            )
        } if a else None,
    }


def build(db: DB, out: Path, ai_note: str | None = None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    rows = db.visible(None, "new")
    # Лічильники — по тих самих картках, що на сайті (без відсіяних AI як «не той ноутбук»).
    stats = {"passed": len(rows), "analyzed_passed": sum(1 for r in rows if r["analysis"])}
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "best_min_score": settings.best_min_score,
        "max_age_days": settings.max_age_days,
        "scan_interval_min": settings.scan_interval_min,
        "analyzed": stats["analyzed_passed"],
        "total": stats["passed"],
        "ai_note": ai_note,
        "listings": [listing_json(db, r) for r in rows],
    }
    (out / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (out / "index.html").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return len(rows)
