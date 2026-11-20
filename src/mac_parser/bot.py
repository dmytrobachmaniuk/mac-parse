"""Telegram-бот: розділи «Найкраще ціна/якість», M1, M2.

Картка = альбом з усіма фото оголошення + під ним текст (характеристики списком,
ціна відносно медіани, висновок AI) з кнопками гортання й посиланням на OLX.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    TelegramObject,
)

from .analyzer import SOURCE_NAMES, QuotaExceeded, is_fresh
from .cards import card_text, fmt_storage, fmt_uah
from .config import settings
from .db import DB, now
from .pricing import effective_specs, market_for_row
from .signals import ai_context, card_extra
from .scanner import Scanner, ScanReport

log = logging.getLogger(__name__)
router = Router()

SECTIONS = {"M1": "💻 MacBook Air M1", "M2": "💻 MacBook Air M2", "TOP": "🏆 Найкраще ціна/якість"}
SORTS = {"score": "🏆 ціна/якість", "price": "💰 дешевші", "new": "🆕 нові"}
SORT_CYCLE = {"score": "price", "price": "new", "new": "score"}
MAX_ALBUM = 10  # ліміт Telegram на медіагрупу

# Заповнюються в run().
db: DB
scanner: Scanner
# (chat_id, id текстового повідомлення картки) → id повідомлень альбому над ним.
albums: dict[tuple[int, int], list[int]] = {}
# Ключі оголошень, які зараз аналізуються у фоні (щоб не запускати двічі).
_analyzing: dict[str, asyncio.Task] = {}


async def ack(cb: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Відповідь на натискання кнопки. Натискання, зроблені поки бот був вимкнений,
    Telegram уже не приймає («query is too old») — це не привід зупиняти обробку."""
    try:
        await cb.answer(text, show_alert=show_alert)
    except TelegramBadRequest:
        pass


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        restricted = settings.allowed_user_ids or settings.allowed_usernames
        allowed = user is not None and (
            user.id in settings.allowed_user_ids
            or (user.username or "").lower() in settings.allowed_usernames
        )
        if restricted and not allowed:
            if isinstance(event, Message):
                await event.answer(f"Бот приватний. Ваш id: <code>{user.id if user else '?'}</code>")
            elif isinstance(event, CallbackQuery):
                await ack(event, "Бот приватний", show_alert=True)
            return None
        return await handler(event, data)


# ---------------------------------------------------------------- клавіатури
def menu_kb(chat_id: int) -> InlineKeyboardMarkup:
    m1, m2 = len(db.visible("M1", "new")), len(db.visible("M2", "new"))
    best = len(rows_for("TOP", "score"))
    notify = "🔔 Сповіщення: увімк." if db.get_notify(chat_id) else "🔕 Сповіщення: вимк."
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text=f"🏆 Найкраще ціна/якість · {best}", callback_data="v:TOP:score:0")],
        [b(text=f"💻 Усі M1 · {m1}", callback_data="v:M1:score:0"), b(text=f"💻 Усі M2 · {m2}", callback_data="v:M2:score:0")],
        [b(text="📊 Ринок", callback_data="stats"), b(text="🔄 Оновити зараз", callback_data="scan")],
        [b(text=notify, callback_data="notif")],
    ])


def card_kb(row: sqlite3.Row, section: str, sort: str, idx: int, total: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    prev_i, next_i = (idx - 1) % total, (idx + 1) % total
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            b(text="◀", callback_data=f"v:{section}:{sort}:{prev_i}"),
            b(text=f"{idx + 1} / {total}", callback_data="noop"),
            b(text="▶", callback_data=f"v:{section}:{sort}:{next_i}"),
        ],
        [b(text=f"🔗 Відкрити на {SOURCE_NAMES.get(row['source'], row['source'])}", url=row["url"])],
        [b(text=f"Сорт: {SORTS[sort]}", callback_data=f"v:{section}:{SORT_CYCLE[sort]}:0")],
        [
            b(text="🔁 Переаналізувати", callback_data=f"re:{row['ext_id']}:{section}:{sort}:{idx}"),
            b(text="⬅ Меню", callback_data="menu"),
        ],
    ])


def notify_kb(row: sqlite3.Row) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    rows = [[b(text=f"🔗 Відкрити на {SOURCE_NAMES.get(row['source'], row['source'])}", url=row["url"])]]
    if settings.site_url:
        # Режим GitHub Actions: постійного бота нема, тож «меню» — це сайт-каталог.
        rows.append([b(text="📋 Ця картка в каталозі", url=f"{settings.site_url}/#{row['key']}")])
    else:
        rows.append([b(text="⬅ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


MENU_TEXT = (
    "<b>Мисливець на MacBook Air M1/M2</b>\n"
    "Парсю OLX кожні {interval} хв, показую лише оголошення, створені за останні {age} дн.\n"
    "Gemini дивиться всі фото й опис і ставить оцінку угоді.\n\n"
    "Оберіть розділ:"
)


def menu_text() -> str:
    text = MENU_TEXT.format(interval=settings.scan_interval_min, age=settings.max_age_days)
    s = db.stats()
    status = ai_status()
    line = f"🤖 Перевірено AI: {s['analyzed_passed']} із {s['passed']}"
    if status:
        line += f"\n⏸ {status}. Бот працює й далі, просто без нових оцінок."
    return text.replace("\n\nОберіть розділ:", f"\n\n{line}\n\nОберіть розділ:")


# ---------------------------------------------------------------- AI
def ai_status() -> str | None:
    """None, якщо Gemini зараз доступний; інакше — пояснення для картки."""
    if scanner.analyzer is None:
        return "AI вимкнено: не задано GEMINI_API_KEY (секрет GitHub або .env)"
    if not scanner.analyzer.available_models():
        until = min(scanner.analyzer.exhausted.values())
        return f"Ліміт AI на сьогодні вичерпано, оцінка з'явиться після ~{datetime.fromtimestamp(until):%H:%M}"
    return None


def start_analysis(row: sqlite3.Row, force: bool = False) -> asyncio.Task | None:
    """Запускає AI-аналіз у фоні (або повертає вже запущений). Нічого не чекає."""
    if ai_status() or (is_fresh(row) and not force):
        return None
    task = _analyzing.get(row["key"])
    if task is None:
        task = asyncio.create_task(_analyze(row))
        _analyzing[row["key"]] = task
    return task


async def _analyze(row: sqlite3.Row) -> bool:
    try:
        market = market_for_row(db, row)
        result = await scanner.analyzer.analyze(row, market, ai_context(db, row, market))
        db.save_analysis(row["key"], result, row["price_uah"])
        return True
    except QuotaExceeded:
        return False
    except Exception:  # noqa: BLE001 — картка живе й без AI
        log.exception("AI для %s не вдався", row["key"])
        return False
    finally:
        _analyzing.pop(row["key"], None)


def prefetch(rows: list[sqlite3.Row], idx: int) -> None:
    """Аналізуємо наступну картку заздалегідь, щоб при гортанні оцінка вже була."""
    if len(rows) > 1:
        start_analysis(rows[(idx + 1) % len(rows)])


# ---------------------------------------------------------------- відправка картки
async def _download(url: str, name: str) -> BufferedInputFile | None:
    if not url.startswith("http"):  # фото з Telegram-барахолки лежать локально (tgphotos/…)
        path = settings.tg_photo_dir.parent / url
        return BufferedInputFile(path.read_bytes(), filename=name) if path.exists() else None
    try:
        r = await scanner.http.get(url)
        if r.status_code == 200 and r.content:
            return BufferedInputFile(r.content, filename=name)
    except Exception:  # noqa: BLE001
        log.warning("Фото не завантажилось: %s", url)
    return None


async def _album_media(row: sqlite3.Row, use_cache: bool = True) -> list[InputMediaPhoto]:
    cached = json.loads(row["photo_file_ids"] or "null") if use_cache else None
    if cached:
        return [InputMediaPhoto(media=fid) for fid in cached[:MAX_ALBUM]]
    urls = json.loads(row["photos"])[:MAX_ALBUM]
    files = await asyncio.gather(*(_download(u, f"{row['ext_id']}_{i}.jpg") for i, u in enumerate(urls)))
    return [InputMediaPhoto(media=f) for f in files if f is not None]


async def _send_album(bot: Bot, chat_id: int, row: sqlite3.Row) -> list[Message]:
    for use_cache in (True, False):
        media = await _album_media(row, use_cache)
        if not media:
            return []
        try:
            if len(media) == 1:
                msgs = [await bot.send_photo(chat_id, media[0].media)]
            else:
                msgs = await bot.send_media_group(chat_id, media)
        except TelegramBadRequest as e:
            if use_cache:  # file_id протух — завантажимо фото заново
                log.warning("Кеш фото %s не спрацював: %s", row["key"], e)
                continue
            raise
        if not use_cache or not row["photo_file_ids"]:
            db.set_photo_file_ids(row["key"], [m.photo[-1].file_id for m in msgs if m.photo])
        return msgs
    return []


async def send_listing(bot: Bot, chat_id: int, row: sqlite3.Row, text: str, kb: InlineKeyboardMarkup) -> Message:
    album = await _send_album(bot, chat_id, row)
    msg = await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    if album:
        albums[(chat_id, msg.message_id)] = [m.message_id for m in album]
    return msg


async def clear_card(bot: Bot, message: Message) -> None:
    ids = albums.pop((message.chat.id, message.message_id), []) + [message.message_id]
    try:
        await bot.delete_messages(message.chat.id, ids)
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------- хендлери
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message) -> None:
    db.add_subscriber(message.chat.id)
    await message.answer(menu_text(), reply_markup=menu_kb(message.chat.id))


@router.message(Command("m1", "m2", "top"))
async def cmd_section(message: Message) -> None:
    section = message.text.lstrip("/").split()[0].split("@")[0].upper()
    await show_card(message.bot, message.chat.id, None, section, "score", 0)


@router.message(Command("scan"))
async def cmd_scan(message: Message) -> None:
    await start_scan(message.bot, message.chat.id)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    await message.answer(stats_text())


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await ack(cb)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await ack(cb)
    if (cb.message.chat.id, cb.message.message_id) in albums or cb.message.photo:
        await clear_card(cb.bot, cb.message)
        await cb.message.answer(menu_text(), reply_markup=menu_kb(cb.message.chat.id))
    else:
        await cb.message.edit_text(menu_text(), reply_markup=menu_kb(cb.message.chat.id))


@router.callback_query(F.data == "notif")
async def cb_notif(cb: CallbackQuery) -> None:
    chat_id = cb.message.chat.id
    db.set_notify(chat_id, not db.get_notify(chat_id))
    await ack(cb, "Збережено")
    await cb.message.edit_reply_markup(reply_markup=menu_kb(chat_id))


@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery) -> None:
    await ack(cb)
    await cb.message.answer(stats_text())


@router.callback_query(F.data == "scan")
async def cb_scan(cb: CallbackQuery) -> None:
    await ack(cb)
    await start_scan(cb.bot, cb.message.chat.id)


@router.callback_query(F.data.startswith("v:"))
async def cb_view(cb: CallbackQuery) -> None:
    _, section, sort, idx = cb.data.split(":")
    await ack(cb)
    await show_card(cb.bot, cb.message.chat.id, cb.message, section, sort, int(idx))


@router.callback_query(F.data.startswith("re:"))
async def cb_reanalyze(cb: CallbackQuery) -> None:
    _, ext_id, section, sort, idx = cb.data.split(":")
    row = db.get(f"olx:{ext_id}")
    if row is None:
        await ack(cb, "Оголошення вже зникло з OLX", show_alert=True)
        return
    status = ai_status()
    if status:
        await ack(cb, status, show_alert=True)
        return
    await ack(cb, "Gemini аналізує заново, картка оновиться сама")
    task = start_analysis(row, force=True)
    await _refresh_card(cb.bot, cb.message, row["key"], section, sort, int(idx), task)


# ---------------------------------------------------------------- логіка
def _value_key(row: sqlite3.Row) -> tuple:
    """Ціна/якість: вища оцінка Gemini → більша знижка від медіани → дешевше. Без оцінки — в кінець."""
    delta = market_for_row(db, row).delta_pct(row["price_uah"])
    return (-(row["ai_score"] or 0), delta if delta is not None else 999, row["price_uah"] or 0)


def rows_for(section: str, sort: str) -> list[sqlite3.Row]:
    rows = db.visible(None if section == "TOP" else section, sort)
    if section == "TOP":
        # Лише перевірені Gemini і з високою оцінкою. Аналіз старою версією промпту теж рахуємо —
        # він оновиться у фоні, а розділ не має порожніти після кожної зміни промпту.
        rows = [r for r in rows if (r["ai_score"] or 0) >= settings.best_min_score]
    if sort == "score":
        rows.sort(key=_value_key)
    return rows


# chat_id → id текстового повідомлення картки, яка зараз на екрані.
_current: dict[int, int] = {}


def _card(row: sqlite3.Row, section: str, sort: str, idx: int, total: int) -> tuple[str, InlineKeyboardMarkup]:
    header = None
    if not is_fresh(row):
        status = ai_status()
        if status:
            header = f"⏸ <i>{status}. Показую дані з оголошення.</i>"
        elif row["key"] in _analyzing:
            header = "⏳ <i>Gemini аналізує фото й опис, картка оновиться сама (~20–60 с)</i>"
    market = market_for_row(db, row)
    return card_text(row, market, header=header or "", extra=card_extra(db, row, market)), card_kb(row, section, sort, idx, total)


async def _refresh_card(bot: Bot, message: Message, key: str, section: str, sort: str, idx: int,
                        task: asyncio.Task | None) -> None:
    """Показує статус на картці, чекає на AI у фоні й перемальовує текст, якщо картка ще на екрані."""
    total = max(len(rows_for(section, sort)), idx + 1)
    row = db.get(key)
    if row is None:
        return
    text, kb = _card(row, section, sort, idx, total)
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass
    if task is None:
        return

    async def wait_and_redraw() -> None:
        await task
        if _current.get(message.chat.id) != message.message_id:
            return  # користувач уже перегорнув далі
        fresh = db.get(key)
        if fresh is None:
            return
        new_text, new_kb = _card(fresh, section, sort, idx, total)
        if not is_fresh(fresh) and not ai_status():
            new_text = new_text.replace("⏳", "⚠️", 1).replace(
                "Gemini аналізує фото й опис, картка оновиться сама (~20–60 с)",
                "AI не вдалося проаналізувати, натисніть «🔁 Переаналізувати»")
        try:
            await bot.edit_message_text(new_text, chat_id=message.chat.id, message_id=message.message_id,
                                        reply_markup=new_kb, disable_web_page_preview=True)
        except TelegramBadRequest:
            pass

    asyncio.create_task(wait_and_redraw())


async def show_card(bot: Bot, chat_id: int, message: Message | None, section: str, sort: str, idx: int) -> None:
    rows = rows_for(section, sort)
    if not rows:
        if section == "TOP":
            s = db.stats()
            status = ai_status()
            text = (
                f"{SECTIONS[section]}\n\nПоки жодне оголошення не отримало від Gemini {settings.best_min_score}+ із 10.\n"
                f"Перевірено {s['analyzed_passed']} із {s['passed']}."
                + (f"\n{status}." if status else " Решта аналізується у фоні — загляньте за кілька хвилин.")
                + "\n\nУсі оголошення доступні в розділах M1 і M2."
            )
        else:
            text = f"{SECTIONS[section]}\n\nПоки порожньо. Натисніть «🔄 Оновити зараз»."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Меню", callback_data="menu")]])
        if message:
            await clear_card(bot, message)
        await bot.send_message(chat_id, text, reply_markup=kb)
        return

    idx %= len(rows)
    row = rows[idx]
    # AI ніколи не блокує показ: картка йде одразу, аналіз (якщо треба) — у фоні.
    task = start_analysis(row)
    text, kb = _card(row, section, sort, idx, len(rows))
    if message:
        await clear_card(bot, message)
    msg = await send_listing(bot, chat_id, row, text, kb)
    _current[chat_id] = msg.message_id
    if task is not None:
        await _refresh_card(bot, msg, row["key"], section, sort, idx, task)
    prefetch(rows, idx)


def stats_text() -> str:
    groups: dict[tuple, list[int]] = defaultdict(list)
    for row in db.visible(None, "price"):
        chip, ram, ssd, screen = effective_specs(row)
        if row["price_uah"] and ram and ssd:
            groups[(chip, screen, ram, ssd)].append(row["price_uah"])
    lines = [
        "<b>📊 Ринок OLX зараз</b>",
        f"Медіанна ціна за конфігурацією (оголошення за останні {settings.max_age_days} дн.)",
        "",
    ]
    for (chip, screen, ram, ssd), prices in sorted(groups.items()):
        if len(prices) < 2:
            continue
        lines.append(
            f"• {chip} {screen}\" · {ram} ГБ / {fmt_storage(ssd)}: <b>{fmt_uah(round(statistics.median(prices)))}</b>"
            f" ({len(prices)} шт.)"
        )
    s = db.stats()
    lines.append(f"\nПоказую: {s['passed']}, з AI-оцінкою: {s['analyzed_passed']}")
    if s["rejected"]:
        lines.append("Відсіяно: " + ", ".join(f"{r[0]} — {r[1]}" for r in s["rejected"][:6]))
    last = db.meta_get("last_scan")
    if last:
        lines.append(f"Останній скан: {last[:16].replace('T', ' ')} UTC")
    return "\n".join(lines)


async def notify(bot: Bot) -> int:
    """Пуші: нові вигідні угоди та зниження ціни на вже хороших. Повертає кількість надісланих карток."""
    new_rows = db.to_notify(settings.notify_min_score)
    drops = db.price_drops(settings.best_min_score)
    subs = db.subscribers()
    sent = 0
    for row, header in (
        *((r, "🆕 <b>Нова вигідна пропозиція</b>") for r in new_rows),
        *((r, f"📉 <b>Ціна впала</b>: {fmt_uah(r['notified_price'])} → {fmt_uah(r['price_uah'])}") for r in drops),
    ):
        market = market_for_row(db, row)
        text = card_text(row, market, header=header, extra=card_extra(db, row, market))
        for chat_id in subs:
            try:
                await send_listing(bot, chat_id, row, text, notify_kb(row))
                sent += 1
            except Exception:  # noqa: BLE001
                log.exception("Сповіщення %s → %s", row["key"], chat_id)
        await asyncio.sleep(0.5)
    db.mark_notified([r["key"] for r in (*new_rows, *drops)])
    return sent


async def scan_and_notify(bot: Bot) -> ScanReport:
    report = await scanner.run()
    db.meta_set("last_scan", now())
    await notify(bot)
    return report


async def start_scan(bot: Bot, chat_id: int) -> None:
    if scanner.busy:
        await bot.send_message(chat_id, "Скан уже йде, зачекайте кілька хвилин.")
        return
    await bot.send_message(chat_id, "🔄 Сканую OLX і проганяю нові оголошення через AI. Це займе кілька хвилин…")

    async def job() -> None:
        try:
            report = await scan_and_notify(bot)
            await bot.send_message(chat_id, "✅ Готово\n" + report.text(), reply_markup=menu_kb(chat_id))
        except Exception as e:  # noqa: BLE001
            log.exception("scan")
            await bot.send_message(chat_id, f"Скан упав: {e.__class__.__name__}: {e}")

    asyncio.create_task(job())


async def periodic(bot: Bot) -> None:
    while True:
        if not scanner.busy:
            try:
                await scan_and_notify(bot)
            except Exception:  # noqa: BLE001
                log.exception("Періодичний скан упав")
        await asyncio.sleep(settings.scan_interval_min * 60)


async def run() -> None:
    global db, scanner
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задано у .env")
    if not settings.gemini_api_key:
        log.warning("GEMINI_API_KEY не задано — картки будуть без AI-висновку")
    db = DB(settings.db_path)
    scanner = Scanner(db)
    bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    router.message.middleware(AccessMiddleware())
    router.callback_query.middleware(AccessMiddleware())
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню"),
        BotCommand(command="m1", description="MacBook Air M1"),
        BotCommand(command="m2", description="MacBook Air M2"),
        BotCommand(command="top", description="Найкраще ціна/якість"),
        BotCommand(command="stats", description="Ринкові ціни"),
        BotCommand(command="scan", description="Оновити зараз"),
    ])
    task = asyncio.create_task(periodic(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await scanner.close()
        await bot.session.close()
