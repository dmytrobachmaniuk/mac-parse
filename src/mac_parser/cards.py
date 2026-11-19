"""Текст картки оголошення для Telegram (HTML, ліміт підпису до фото — 1024 символи)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from html import escape

from .pricing import Market, effective_specs

CAPTION_LIMIT = 1024


def fmt_uah(v: int | None) -> str:
    return f"{v:,}".replace(",", " ") + " грн" if v else "—"


def fmt_storage(gb: int | None) -> str:
    if not gb:
        return "?"
    return f"{gb // 1024}TB" if gb >= 1024 else str(gb)


def age(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "сьогодні"
    if days == 1:
        return "вчора"
    return f"{days} дн. тому"


def score_badge(score: int | None) -> str:
    if score is None:
        return "⏳ AI ще не дивився"
    icon = "🟢" if score >= 8 else "🟡" if score >= 5 else "🔴"
    return f"{icon} <b>{score}/10</b>"


def _join(items: list[str], limit: int) -> str:
    return "; ".join(escape(i) for i in items[:limit])


def caption(row: sqlite3.Row, market: Market, position: str = "") -> str:
    a = json.loads(row["analysis"]) if row["analysis"] else {}
    chip, ram, ssd, screen = effective_specs(row)
    screen_txt = {"15": '15"', "13": '13"'}.get(screen, "")
    if a.get("screen_inch"):
        screen_txt = f'{a["screen_inch"]:g}"'

    head = [score_badge(a.get("deal_score"))]
    if a.get("price_verdict"):
        head.append(escape(a["price_verdict"]))
    if position:
        head.append(position)

    price_line = f"💰 <b>{fmt_uah(row['price_uah'])}</b>"
    if row["currency"] != "UAH" and row["price"]:
        price_line += f" ({row['price']} {escape(row['currency'])})"
    delta = market.delta_pct(row["price_uah"])
    if delta is not None:
        sign = "−" if delta < 0 else "+"
        price_line += f"  {sign}{abs(delta):.0f}% від медіани {fmt_uah(market.median)} (n={market.count})"
    if row["negotiable"]:
        price_line += " · торг"

    history = json.loads(row["price_history"] or "[]")
    if len(history) > 1 and history[0][1] and history[0][1] != row["price_uah"]:
        price_line += f"\n📉 була {fmt_uah(history[0][1])}"

    seller = "🏪 магазин" if row["is_business"] or a.get("seller_type", "").startswith("магазин") else "👤 приватна"
    lines = [
        " · ".join(head),
        f"<b>MacBook Air {escape(chip or '?')} · {ram or '?'}/{fmt_storage(ssd)} · {screen_txt}</b>"
        + (f" · {escape(a['color'])}" if a.get("color") else ""),
        price_line,
        f"📍 {escape(row['city'] or '?')} · {seller} · {age(row['created_at'])}",
    ]

    bat_pct = a.get("battery_health_pct") or row["battery_pct"]
    cycles = a.get("battery_cycles") or row["battery_cycles"]
    if bat_pct or cycles:
        lines.append(f"🔋 {f'{bat_pct}%' if bat_pct else '?%'} · {cycles if cycles else '?'} циклів")
    if a:
        cond = f"🧽 Стан {a.get('condition_score', '?')}/10"
        if a.get("visual_defects"):
            cond += f": {_join(a['visual_defects'], 3)}"
        lines.append(cond)
        if a.get("kit"):
            lines.append(f"📦 {_join(a['kit'], 4)}")
        if a.get("pros"):
            lines.append(f"✅ {_join(a['pros'], 3)}")
        if a.get("red_flags"):
            lines.append(f"⚠️ {_join(a['red_flags'], 3)}")
        if a.get("summary"):
            lines.append(f"\n🤖 <i>{escape(a['summary'])}</i>")
        if a.get("questions_to_seller"):
            lines.append("❓ " + _join(a["questions_to_seller"], 2))
    else:
        lines.append(f"\n<i>{escape(row['title'])}</i>")

    text = "\n".join(lines)
    # Скорочуємо цілими рядками (щоб не розрізати HTML-теги): спершу другорядне, висновок AI — останнім.
    for prefix in ("❓", "✅", "📦"):
        if len(text) <= CAPTION_LIMIT:
            break
        lines = [ln for ln in lines if not ln.startswith(prefix)]
        text = "\n".join(lines)
    while len(text) > CAPTION_LIMIT and len(lines) > 4:
        lines.pop()
        text = "\n".join(lines)
    return text[:CAPTION_LIMIT]
