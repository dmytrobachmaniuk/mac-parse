"""Текст картки оголошення для Telegram (HTML).

Картка — це альбом з усіма фото і під ним текстове повідомлення з кнопками,
тому ліміт тексту — 4096 символів, а не 1024, як у підпису до фото.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from html import escape as _html_escape

from .classify import age_days


def escape(text: str) -> str:
    # Лапки й апострофи в тексті повідомлення Telegram екранувати не треба.
    return _html_escape(str(text), quote=False)
from .pricing import Market, effective_specs

SOURCE_NAMES = {"olx": "OLX", "prom": "Prom.ua"}

TEXT_LIMIT = 4096


def fmt_uah(v: int | None) -> str:
    return f"{v:,}".replace(",", " ") + " грн" if v else "—"


def fmt_storage(gb: int | None) -> str:
    if not gb:
        return "?"
    return f"{gb // 1024} ТБ" if gb >= 1024 else f"{gb} ГБ"


def fmt_date(iso: str) -> str:
    """«30.05 (173 дн. тому)»."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "?"
    days = age_days(iso)
    ago = "сьогодні" if days == 0 else "вчора" if days == 1 else f"{days} дн. тому"
    return f"{d:%d.%m.%Y} ({ago})"


def score_badge(score: int | None) -> str:
    if score is None:
        return "⚪ без оцінки AI"
    icon = "🟢" if score >= 8 else "🟡" if score >= 5 else "🔴"
    return f"{icon} <b>{score}/10</b>"


def _bullets(title: str, items: list[str], limit: int = 5) -> list[str]:
    if not items:
        return []
    return ["", f"<b>{title}</b>", *(f"• {escape(i)}" for i in items[:limit])]


def card_text(row: sqlite3.Row, market: Market, header: str = "", extra: dict | None = None) -> str:
    """extra: {"seller_flags": [...], "seller_notes": [...], "sold": str|None, "repost": str|None} з signals."""
    extra = extra or {}
    a = json.loads(row["analysis"]) if row["analysis"] else {}
    chip, ram, ssd, screen = effective_specs(row)
    screen_txt = f'{a["screen_inch"]:g}"' if a.get("screen_inch") else f'{screen}"'

    top = [score_badge(a.get("deal_score"))]
    if a.get("price_verdict"):
        top.append(escape(a["price_verdict"]))
    lines = []
    if header:
        lines.append(header)
    lines += [
        " · ".join(top),
        f"<b>MacBook Air {escape(chip or '?')} · {screen_txt}</b>",
        "",
    ]

    price = f"💰 <b>{fmt_uah(row['price_uah'])}</b>"
    if row["currency"] != "UAH" and row["price"]:
        price += f" ({row['price']} {escape(row['currency'])})"
    if row["negotiable"]:
        price += " · торг"
    lines.append(price)
    delta = market.delta_pct(row["price_uah"])
    if delta is not None:
        sign = "−" if delta < 0 else "+"
        lines.append(f"📊 {sign}{abs(delta):.0f}% від медіани {fmt_uah(market.median)} (схожих: {market.count})")
    history = json.loads(row["price_history"] or "[]")
    if len(history) > 1 and history[0][1] and history[0][1] != row["price_uah"]:
        lines.append(f"📉 початкова ціна {fmt_uah(history[0][1])}")
    if extra.get("sold"):
        lines.append(f"💸 {escape(extra['sold'])}")
    if extra.get("repost"):
        lines.append(f"🔁 {escape(extra['repost'])}")

    bat_pct = a.get("battery_health_pct") or row["battery_pct"]
    cycles = a.get("battery_cycles") or row["battery_cycles"]
    specs = [
        f"Чіп: Apple {escape(chip or '?')}",
        f"Оперативна пам'ять: {f'{ram} ГБ' if ram else '?'}",
        f"Накопичувач: {fmt_storage(ssd)}",
        f"Екран: {screen_txt}",
    ]
    if a.get("color"):
        specs.append(f"Колір: {escape(a['color'])}")
    battery = f"Батарея: {f'{bat_pct}%' if bat_pct else '?'} · циклів: {cycles if cycles else '?'}"
    source = a.get("battery_source")
    if (bat_pct or cycles) and source in ("фото", "опис"):
        battery += " (✅ видно на фото)" if source == "фото" else " (зі слів продавця)"
    specs.append(battery)
    if a:
        specs.append(f"Стан: {a.get('condition_score', '?')}/10")
    if a.get("kit"):
        specs.append(f"Комплект: {escape(', '.join(a['kit']))}")
    lines += ["", "<b>Характеристики</b>", *(f"• {s}" for s in specs)]

    seller = "магазин / перекуп" if row["is_business"] or a.get("seller_type", "").startswith("магазин") else "приватна особа"
    lines += [
        "",
        "<b>Оголошення</b>",
        f"• Продавець: {seller}" + (f" «{escape(row['seller_name'])}»" if row["seller_name"] else ""),
        *([f"• Про продавця: {escape(' · '.join(extra['seller_notes']))}"] if extra.get("seller_notes") else []),
        f"• Джерело: {SOURCE_NAMES.get(row['source'], row['source'])}" + (f", {escape(row['city'])}" if row["city"] else ""),
        f"• Створено: {fmt_date(row['created_at'])}",
        f"• Оновлено: {fmt_date(row['refreshed_at'])}",
    ]

    if a:
        lines += _bullets("📸 Видно на фото", a.get("photo_facts", []), 6)
        lines += _bullets("Дефекти на фото", a.get("visual_defects", []))
        lines += _bullets("✅ Плюси", a.get("pros", []))
        lines += _bullets("⚠️ Ризики", [*a.get("red_flags", []), *extra.get("seller_flags", [])])
        if a.get("summary"):
            lines += ["", "🤖 <b>Висновок AI</b>", f"<i>{escape(a['summary'])}</i>"]
        lines += _bullets("❓ Спитати продавця", a.get("questions_to_seller", []), 3)
    else:
        lines += _bullets("⚠️ Ризики", extra.get("seller_flags", []))
        lines += ["", f"<i>{escape(row['title'])}</i>"]

    text = "\n".join(lines)
    while len(text) > TEXT_LIMIT and len(lines) > 10:
        lines.pop()
        text = "\n".join(lines)
    return text
