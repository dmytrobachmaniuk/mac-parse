"""Один прохід для GitHub Actions (кожні ~30 хв): OLX → Gemini → пуші в Telegram → сайт.

Постійно увімкненого бота тут немає, тому:
- повідомлення, що прийшли в бот між запусками (/start тощо), обробляються пачкою через getUpdates;
- кнопки в Telegram ведуть на сайт-каталог (GitHub Pages), а не на гортання в чаті.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from . import bot as botmod
from . import site
from .config import settings
from .db import DB, now
from .scanner import Scanner

log = logging.getLogger(__name__)


def _load_exhausted(db: DB, scanner: Scanner) -> None:
    if scanner.analyzer is None:
        return
    saved = json.loads(db.meta_get("gemini_exhausted") or "{}")
    scanner.analyzer.exhausted.update({m: t for m, t in saved.items() if t > time.time()})


def _save_exhausted(db: DB, scanner: Scanner) -> None:
    if scanner.analyzer is not None:
        db.meta_set("gemini_exhausted", json.dumps(scanner.analyzer.exhausted))


def _allowed(user) -> bool:
    if not (settings.allowed_user_ids or settings.allowed_usernames):
        return True
    return user is not None and (
        user.id in settings.allowed_user_ids or (user.username or "").lower() in settings.allowed_usernames
    )


async def handle_updates(bot: Bot) -> int:
    """Відповідає на повідомлення, що прийшли між запусками (бот не працює постійно)."""
    updates = await bot.get_updates(timeout=0, allowed_updates=["message"])
    for upd in updates:
        msg = upd.message
        if msg is None or msg.chat.type != "private":
            continue
        if not _allowed(msg.from_user):
            await bot.send_message(msg.chat.id, "Бот приватний.")
            continue
        text = (
            "👋 Я перевіряю MacBook Air M1/M2 на OLX кожні ~30 хв (тому відповідаю із затримкою).\n"
            f"Весь каталог з оцінками AI: {settings.site_url or '(SITE_URL не задано)'}"
        )
        if msg.chat.id in settings.telegram_chat_ids:
            text += "\n\nНові вигідні оголошення надсилаю сюди автоматично."
        else:
            text += (
                f"\n\nЩоб отримувати пуші, додайте <code>{msg.chat.id}</code> у секрет "
                "TELEGRAM_CHAT_IDS репозиторію на GitHub."
            )
        await bot.send_message(msg.chat.id, text, disable_web_page_preview=True)
    if updates:  # підтверджуємо, щоб Telegram не віддав їх повторно
        await bot.get_updates(offset=updates[-1].update_id + 1, timeout=0)
    return len(updates)


async def run_cycle(out: Path) -> None:
    # Явно кажемо, чого бракує: у хмарі відсутній секрет інакше тихо вимикає частину функцій.
    for name, value in (("GEMINI_API_KEY", settings.gemini_api_key), ("TELEGRAM_BOT_TOKEN", settings.telegram_token),
                        ("TELEGRAM_CHAT_IDS", settings.telegram_chat_ids)):
        if not value:
            log.warning("Секрет %s не задано — %s вимкнено", name,
                        "AI-аналіз" if name == "GEMINI_API_KEY" else "Telegram-пуші")
    db = DB(settings.db_path)
    scanner = Scanner(db)
    _load_exhausted(db, scanner)
    tg = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if settings.telegram_token else None
    try:
        if tg:
            try:
                n = await handle_updates(tg)
                log.info("Telegram: оброблено %d повідомлень", n)
            except TelegramAPIError:
                log.exception("Telegram getUpdates не вдався")

        report = await scanner.run()
        db.meta_set("last_scan", now())
        _save_exhausted(db, scanner)
        log.info("Скан:\n%s", report.text())

        botmod.db, botmod.scanner = db, scanner
        if tg and settings.telegram_chat_ids:
            for chat_id in settings.telegram_chat_ids:
                db.add_subscriber(chat_id)
            await botmod.notify(tg)
        # База їде в публічну гілку data — chat id туди не пишемо.
        db.conn.execute("DELETE FROM subscribers")
        db.conn.commit()

        count = site.build(db, out, ai_note=botmod.ai_status())  # і «ліміт вичерпано», і «нема ключа»
        log.info("Сайт: %d оголошень → %s", count, out)
    finally:
        await scanner.close()
        if tg:
            await tg.session.close()
