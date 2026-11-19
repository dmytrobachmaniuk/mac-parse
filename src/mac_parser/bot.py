"""Telegram-бот: розділи M1 / M2 / Топ, картка = фото + звіт + висновок AI + посилання."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
from collections import defaultdict
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

from .analyzer import QuotaExceeded
from .cards import caption, fmt_storage, fmt_uah
from .config import settings
from .db import DB, now
from .pricing import effective_specs, market_for_row
from .scanner import Scanner, ScanReport

log = logging.getLogger(__name__)
router = Router()

SECTIONS = {"M1": "💻 MacBook Air M1", "M2": "💻 MacBook Air M2", "TOP": "🔥 Топ угоди M1 + M2"}
SORTS = {"score": "⭐ за оцінкою AI", "price": "💰 за ціною", "new": "🆕 спершу нові"}
SORT_CYCLE = {"score": "price", "price": "new", "new": "score"}

# Заповнюються в run().
db: DB
scanner: Scanner


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
                await event.answer("Бот приватний", show_alert=True)
            return None
        return await handler(event, data)


# ---------------------------------------------------------------- клавіатури
def menu_kb(chat_id: int) -> InlineKeyboardMarkup:
    m1, m2 = len(db.visible("M1", "score")), len(db.visible("M2", "score"))
    notify = "🔔 Сповіщення: увімк." if db.get_notify(chat_id) else "🔕 Сповіщення: вимк."
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text=f"💻 M1 · {m1}", callback_data="v:M1:score:0"), b(text=f"💻 M2 · {m2}", callback_data="v:M2:score:0")],
        [b(text="🔥 Топ угоди", callback_data="v:TOP:score:0")],
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
        [b(text="🔗 Відкрити оригінал на OLX", url=row["url"])],
        [
            b(text=f"Сорт: {SORTS[sort]}", callback_data=f"v:{section}:{SORT_CYCLE[sort]}:0"),
        ],
        [
            b(text="🔁 Переаналізувати", callback_data=f"re:{row['ext_id']}:{section}:{sort}:{idx}"),
            b(text="⬅ Меню", callback_data="menu"),
        ],
    ])


def notify_kb(row: sqlite3.Row) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="🔗 Відкрити оригінал на OLX", url=row["url"])],
        [b(text="⬅ Меню", callback_data="menu")],
    ])


MENU_TEXT = (
    "<b>Мисливець на MacBook Air M1/M2</b>\n"
    "Парсю OLX кожні {interval} хв, Gemini дивиться фото й опис і ставить оцінку угоді.\n\n"
    "Оберіть розділ:"
)


# ---------------------------------------------------------------- фото
async def photo_input(row: sqlite3.Row) -> str | BufferedInputFile | None:
    if row["photo_file_id"]:
        return row["photo_file_id"]
    photos = json.loads(row["photos"])
    if not photos:
        return None
    try:
        r = await scanner.http.get(photos[0])
        if r.status_code == 200:
            return BufferedInputFile(r.content, filename=f"{row['ext_id']}.jpg")
    except Exception:  # noqa: BLE001
        log.warning("Фото %s не завантажилось", row["key"])
    return None


def _remember_file_id(row: sqlite3.Row, msg: Message | bool | None) -> None:
    if isinstance(msg, Message) and msg.photo and not row["photo_file_id"]:
        db.set_photo_file_id(row["key"], msg.photo[-1].file_id)


async def send_card(bot: Bot, chat_id: int, row: sqlite3.Row, text: str, kb: InlineKeyboardMarkup) -> None:
    photo = await photo_input(row)
    if photo is None:
        await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
        return
    msg = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
    _remember_file_id(row, msg)


# ---------------------------------------------------------------- хендлери
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message) -> None:
    db.add_subscriber(message.chat.id)
    await message.answer(MENU_TEXT.format(interval=settings.scan_interval_min), reply_markup=menu_kb(message.chat.id))


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
    await cb.answer()


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    text = MENU_TEXT.format(interval=settings.scan_interval_min)
    if cb.message.photo:
        await cb.message.delete()
        await cb.message.answer(text, reply_markup=menu_kb(cb.message.chat.id))
    else:
        await cb.message.edit_text(text, reply_markup=menu_kb(cb.message.chat.id))


@router.callback_query(F.data == "notif")
async def cb_notif(cb: CallbackQuery) -> None:
    chat_id = cb.message.chat.id
    db.set_notify(chat_id, not db.get_notify(chat_id))
    await cb.answer("Збережено")
    await cb.message.edit_reply_markup(reply_markup=menu_kb(chat_id))


@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(stats_text())


@router.callback_query(F.data == "scan")
async def cb_scan(cb: CallbackQuery) -> None:
    await cb.answer()
    await start_scan(cb.bot, cb.message.chat.id)


@router.callback_query(F.data.startswith("v:"))
async def cb_view(cb: CallbackQuery) -> None:
    _, section, sort, idx = cb.data.split(":")
    await cb.answer()
    await show_card(cb.bot, cb.message.chat.id, cb.message, section, sort, int(idx))


@router.callback_query(F.data.startswith("re:"))
async def cb_reanalyze(cb: CallbackQuery) -> None:
    _, ext_id, section, sort, idx = cb.data.split(":")
    row = db.get(f"olx:{ext_id}")
    if row is None or scanner.analyzer is None:
        await cb.answer("AI не налаштовано" if scanner.analyzer is None else "Оголошення не знайдено", show_alert=True)
        return
    await cb.answer("Аналізую, ~10 с…")
    try:
        result = await scanner.analyzer.analyze(row, market_for_row(db, row))
    except QuotaExceeded:
        await cb.message.answer("Ліміт Gemini вичерпано, спробуйте пізніше.")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("reanalyze")
        await cb.message.answer(f"Не вдалося: {e.__class__.__name__}")
        return
    db.save_analysis(row["key"], result, row["price_uah"])
    # Порядок у списку міг змінитись — показуємо саме це оголошення.
    rows = rows_for(section, sort)
    pos = next((i for i, r in enumerate(rows) if r["key"] == row["key"]), int(idx))
    await show_card(cb.bot, cb.message.chat.id, cb.message, section, sort, pos)


# ---------------------------------------------------------------- логіка
def rows_for(section: str, sort: str) -> list[sqlite3.Row]:
    return db.visible(None if section == "TOP" else section, sort)


async def show_card(bot: Bot, chat_id: int, message: Message | None, section: str, sort: str, idx: int) -> None:
    rows = rows_for(section, sort)
    if not rows:
        text = f"{SECTIONS[section]}\n\nПоки порожньо. Натисніть «🔄 Оновити зараз»."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Меню", callback_data="menu")]])
        if message and not message.photo:
            await message.edit_text(text, reply_markup=kb)
        else:
            await bot.send_message(chat_id, text, reply_markup=kb)
        return

    idx %= len(rows)
    row = rows[idx]
    text = caption(row, market_for_row(db, row))
    kb = card_kb(row, section, sort, idx, len(rows))
    photo = await photo_input(row)

    if message and message.photo and photo is not None:
        try:
            edited = await message.edit_media(InputMediaPhoto(media=photo, caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
            _remember_file_id(row, edited)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            log.warning("edit_media не вдався: %s", e)
    if message:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
    if photo is None:
        await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    else:
        msg = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
        _remember_file_id(row, msg)


def stats_text() -> str:
    groups: dict[tuple, list[int]] = defaultdict(list)
    for row in db.visible(None, "price"):
        chip, ram, ssd, screen = effective_specs(row)
        if row["price_uah"] and ram and ssd:
            groups[(chip, screen, ram, ssd)].append(row["price_uah"])
    lines = ["<b>📊 Ринок OLX зараз (медіана · мін · кількість)</b>"]
    for (chip, screen, ram, ssd), prices in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], kv[0][3])):
        if len(prices) < 2:
            continue
        lines.append(
            f"{chip} {screen}\" {ram}/{fmt_storage(ssd)}: <b>{fmt_uah(round(statistics.median(prices)))}</b>"
            f" · від {fmt_uah(min(prices))} · {len(prices)} шт."
        )
    s = db.stats()
    lines.append(
        f"\nУ базі активних: {s['active']}, пройшли фільтр: {s['passed']}, з AI-оцінкою: {s['analyzed']}"
    )
    if s["rejected"]:
        lines.append("Відсіяно: " + ", ".join(f"{r[0]} — {r[1]}" for r in s["rejected"][:6]))
    last = db.meta_get("last_scan")
    if last:
        lines.append(f"Останній скан: {last[:16].replace('T', ' ')} UTC")
    return "\n".join(lines)


async def notify(bot: Bot) -> None:
    rows = db.to_notify(settings.notify_min_score)
    if not rows:
        return
    subs = db.subscribers()
    for row in rows:
        text = caption(row, market_for_row(db, row), position="🚨 <b>нова пропозиція</b>")
        for chat_id in subs:
            try:
                await send_card(bot, chat_id, row, text, notify_kb(row))
            except Exception:  # noqa: BLE001
                log.exception("Сповіщення %s → %s", row["key"], chat_id)
        await asyncio.sleep(0.5)
    db.mark_notified([r["key"] for r in rows])


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
        BotCommand(command="top", description="Топ угоди"),
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
