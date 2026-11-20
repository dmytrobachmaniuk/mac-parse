"""Telegram-барахолки (групи) через Telethon — від імені вашого акаунта.

- Читаємо лише нові повідомлення (останній прочитаний id групи — у meta бази);
  на першому запуску — за останні TG_FIRST_DAYS днів.
- Альбом (кілька фото одним постом) = одне оголошення.
- Беремо пости, схожі на продаж MacBook Air; «куплю/шукаю» відсіює classify.
- Фото з Telegram не мають публічних посилань: завантажуємо в TG_PHOTO_DIR
  (публікується разом із сайтом як tgphotos/…).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

from ..config import ROOT, settings
from ..models import Listing

log = logging.getLogger(__name__)

PHOTO_PREFIX = "tgphotos/"
MAX_PHOTOS = 6
PHOTO_TTL_DAYS = 45

_SALE_RE = re.compile(r"(macbook|макбук|mac\s*book)", re.I)
_AIR_RE = re.compile(r"(air|аір|аир|эйр|ейр)", re.I)

# Ціна з вільного тексту. Порядок важливий: спершу з явною валютою.
_NUM = r"(\d{1,3}(?:[  .,]\d{3})+|\d+(?:[.,]\d+)?)"
_PRICE_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(_NUM + r"\s*(?:грн|uah|₴|гривен\w*|гривн\w*|гр\b)", re.I), "UAH", 1),
    (re.compile(r"(?:\$|usd)\s*" + _NUM, re.I), "USD", 1),
    (re.compile(_NUM + r"\s*(?:\$|usd|дол\w*|у\.?\s?е\.?(?!\w))", re.I), "USD", 1),
    (re.compile(_NUM + r"\s*(?:€|eur|євро|евро)", re.I), "EUR", 1),
    (re.compile(_NUM + r"\s*(?:к|k|тис\.?|тыс\.?)(?![a-zа-яіїє])", re.I), "UAH", 1000),
    (re.compile(r"(?:ціна|цена|price|прайс)\W{0,5}" + _NUM, re.I), "UAH", 1),
]
_PLAUSIBLE = {"UAH": (5000, 150000), "USD": (120, 3500), "EUR": (110, 3200)}


class TgError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.tg_api_id and settings.tg_api_hash and settings.tg_session)


def _to_number(raw: str) -> float | None:
    s = raw.replace(" ", " ").strip()
    if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", s):  # 26 000 / 26.000 / 26,000
        return float(re.sub(r"[ .,]", "", s))
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_price(text: str) -> tuple[int | None, str]:
    """(ціна, валюта) з тексту поста або (None, 'UAH'). Відкидає неправдоподібні числа (8/256, 2020…)."""
    for pattern, currency, mult in _PRICE_PATTERNS:
        for m in pattern.finditer(text or ""):
            n = _to_number(m.group(1))
            if n is None:
                continue
            value = round(n * mult)
            lo, hi = _PLAUSIBLE[currency]
            if lo <= value <= hi:
                return value, currency
    return None, "UAH"


_AIR_CHIP_RE = re.compile(r"(?<![a-zа-я])(air|аір|аир|эйр|ейр)(?![a-zа-я]).{0,40}?(?<![a-zа-я0-9])[mм]\s?[12](?![0-9])"
                          r"|(?<![a-zа-я0-9])[mм]\s?[12](?![0-9]).{0,40}?(?<![a-zа-я])(air|аір|аир|эйр|ейр)(?![a-zа-я])", re.I | re.S)


# Оптові прайси перекупів і рекламні розсилки: не продаж одного ноутбука.
_SPAM_RE = re.compile(r"₽|руб(?:\.|л|\b)|\bопт\b|оптом|оптов|прайс|дропшип|iphone duo|в наявності.*моделі", re.I | re.S)
_MODEL_RE = re.compile(r"iphone|ipad|macbook|макбук|airpods|apple watch|imac|mac mini", re.I)


def is_price_list(text: str) -> bool:
    """Прайс/розсилка: явні маркери або згадка 6+ різних товарів Apple в одному пості."""
    t = text or ""
    return bool(_SPAM_RE.search(t)) or len(_MODEL_RE.findall(t)) >= 6


def looks_like_sale(text: str) -> bool:
    """Пост про продаж одного MacBook Air: «MacBook … Air» або в Apple-барахолці просто «Air M1»; не прайс."""
    t = text or ""
    if is_price_list(t):
        return False
    if re.search(r"\bipad\b|айпад", t, re.I) and not _SALE_RE.search(t):
        return False  # «iPad Air M2» — не MacBook
    return bool((_SALE_RE.search(t) and _AIR_RE.search(t)) or _AIR_CHIP_RE.search(t))


def make_title(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in lines:
        if _SALE_RE.search(ln):
            return ln[:120]
    return (lines[0] if lines else "MacBook Air")[:120]


def build_listing(chat: str, msgs: list, sender, photos: list[str]) -> Listing | None:
    """Одне оголошення з повідомлень одного альбому (або одного поста)."""
    text = "\n".join(m.message for m in sorted(msgs, key=lambda m: m.id) if getattr(m, "message", None)).strip()
    if not looks_like_sale(text):
        return None
    main = min(msgs, key=lambda m: m.id)
    price, currency = parse_price(text)
    created = main.date.astimezone(timezone.utc).isoformat(timespec="seconds")
    name = ""
    if sender is not None:
        name = " ".join(x for x in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if x) \
            or getattr(sender, "title", "") or ""
        if getattr(sender, "username", None):
            name = f"{name} (@{sender.username})".strip()
    return Listing(
        source="tg",
        ext_id=f"{chat}/{main.id}",
        url=f"https://t.me/{chat}/{main.id}",
        title=make_title(text),
        description=text,
        price=price,
        currency=currency,
        negotiable=bool(re.search(r"\bторг", text, re.I)),
        photos=photos,
        city="",
        region="",
        is_business=False,
        seller_name=name,
        created_at=created,
        refreshed_at=created,
        params={"chat": chat},
        seller_id=f"tg:{sender.id}" if sender is not None and getattr(sender, "id", None) else "",
    )


def _cleanup_photos() -> None:
    d = settings.tg_photo_dir
    if not d.exists():
        return
    cutoff = time.time() - PHOTO_TTL_DAYS * 86400
    for p in d.glob("*.jpg"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


async def _fetch_chat(client, db, chat: str) -> tuple[list[Listing], int]:
    entity = await client.get_entity(chat)
    last = int(db.meta_get(f"tg_last_id:{chat}") or 0)
    since = datetime.now(timezone.utc) - timedelta(days=settings.tg_first_days)
    groups: dict[int, list] = {}
    max_id = last
    async for m in client.iter_messages(entity, limit=settings.tg_max_messages, min_id=last):
        if not last and m.date < since:
            break
        max_id = max(max_id, m.id)
        groups.setdefault(m.grouped_id or m.id, []).append(m)

    out: list[Listing] = []
    settings.tg_photo_dir.mkdir(parents=True, exist_ok=True)
    for msgs in groups.values():
        text = " ".join(m.message or "" for m in msgs)
        if not looks_like_sale(text):
            continue  # фото качаємо лише для схожих на продаж MacBook Air
        photos = []
        for m in sorted((m for m in msgs if m.photo), key=lambda m: m.id)[:MAX_PHOTOS]:
            name = f"{chat}_{m.id}.jpg"
            path = settings.tg_photo_dir / name
            if not path.exists():
                await client.download_media(m, file=str(path))
            photos.append(PHOTO_PREFIX + name)
        sender = await min(msgs, key=lambda m: m.id).get_sender()
        item = build_listing(chat, msgs, sender, photos)
        if item:
            out.append(item)
    log.info("Telegram @%s: нових повідомлень %d, схожих на продаж MacBook Air %d",
             chat, sum(len(g) for g in groups.values()), len(out))
    return out, max_id


async def fetch(db) -> tuple[list[Listing], dict[str, int], list[str]]:
    """(оголошення, {чат: останній id}, помилки). Порожньо, якщо Telegram не налаштовано."""
    if not configured():
        return [], {}, []
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError, RPCError
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(settings.tg_session), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    listings: list[Listing] = []
    last_ids: dict[str, int] = {}
    errors: list[str] = []
    try:
        if not await client.is_user_authorized():
            return [], {}, ["Telegram: сесія недійсна — запустіть `mac-parser tg-login` і оновіть TG_SESSION"]
        for chat in settings.tg_chats:
            try:
                items, last_ids[chat] = await _fetch_chat(client, db, chat)
                listings += items
            except FloodWaitError as e:
                errors.append(f"Telegram @{chat}: ліміт запитів, пауза {e.seconds} с")
            except (RPCError, ValueError) as e:
                errors.append(f"Telegram @{chat}: {e.__class__.__name__}: {e}")
        _cleanup_photos()
    finally:
        await client.disconnect()
    return listings, last_ids, errors


def _save_env(key: str, value: str) -> None:
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def normalize_phone(raw: str) -> str:
    """0631234567 / 631234567 / 380631234567 / +380 63 123 45 67 → +380631234567."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 9:
        digits = "380" + digits
    elif len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    return "+" + digits


async def login() -> None:
    """Одноразовий вхід: телефон і код вводите ви; сесія пишеться в .env (TG_SESSION), у консоль не виводиться."""
    from getpass import getpass

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = settings.tg_api_id or int(re.sub(r"\D", "", input("api_id з my.telegram.org (число): ")))
    # Видиме поле: у терміналі Windows у приховане (getpass) не працює вставка Ctrl+V.
    api_hash = settings.tg_api_hash or input("api_hash з my.telegram.org (32 символи, вставте Ctrl+V): ").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        print(f"api_hash має бути рівно 32 символи 0–9/a–f, а введено {len(api_hash)}. Скопіюйте ще раз і повторіть.")
        return
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(
        phone=lambda: normalize_phone(input("Телефон Telegram (можна 0631234567 або +380…): ")),
        code_callback=lambda: input("Код, що прийшов у Telegram: ").strip(),
        password=lambda: getpass("Пароль двоетапної перевірки (якщо є): "),
    )
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    _save_env("TG_API_ID", str(api_id))
    _save_env("TG_API_HASH", api_hash)
    _save_env("TG_SESSION", session)
    print(f"Готово: увійшли як {me.first_name}. TG_API_ID, TG_API_HASH і TG_SESSION збережено в .env.")
    print("TG_SESSION дає доступ до акаунта — нікому не показуйте; у хмару він іде лише через секрет CREDENTIAL_MAC.")
