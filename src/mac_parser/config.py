"""Налаштування з .env / змінних середовища."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _entries(name: str) -> list[str]:
    return [x for x in os.getenv(name, "").replace(" ", "").split(",") if x]


def _ids(name: str) -> set[int]:
    return {int(x) for x in _entries(name) if x.isdigit()}


def _usernames(name: str) -> set[str]:
    # Дозволяємо і @username замість числового id.
    return {x.lstrip("@").lower() for x in _entries(name) if not x.isdigit()}


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip())
    # Скільки запитів до Gemini на хвилину (безкоштовний tier має жорсткі ліміти).
    gemini_rpm: int = field(default_factory=lambda: _int("GEMINI_RPM", 8))
    # Скільки нових оголошень аналізувати за один прохід, щоб не спалити квоту.
    max_analyze_per_scan: int = field(default_factory=lambda: _int("MAX_ANALYZE_PER_SCAN", 40))
    # Скільки фото оголошення віддавати в Gemini.
    photos_for_ai: int = field(default_factory=lambda: _int("PHOTOS_FOR_AI", 4))

    scan_interval_min: int = field(default_factory=lambda: _int("SCAN_INTERVAL_MIN", 30))
    # Нижче цієї ціни (грн) це майже напевно аксесуар, запчастини або скам.
    min_price_uah: int = field(default_factory=lambda: _int("MIN_PRICE_UAH", 12000))
    max_price_uah: int = field(default_factory=lambda: _int("MAX_PRICE_UAH", 80000))
    # Пуш-сповіщення про нове оголошення з AI-оцінкою від цього значення (1..10).
    notify_min_score: int = field(default_factory=lambda: _int("NOTIFY_MIN_SCORE", 8))
    # Якщо задано — бот відповідає лише цим Telegram user id.
    allowed_user_ids: set[int] = field(default_factory=lambda: _ids("ALLOWED_USER_IDS"))
    allowed_usernames: set[str] = field(default_factory=lambda: _usernames("ALLOWED_USER_IDS"))
    usd_rate: float = field(default_factory=lambda: float(os.getenv("USD_UAH_RATE", "41.5")))

    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", str(ROOT / "data" / "macbot.sqlite3"))))


settings = Settings()
