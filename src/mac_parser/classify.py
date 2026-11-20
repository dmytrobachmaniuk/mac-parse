"""Дешевий попередній фільтр і витяг характеристик регулярками.

Мета — не пустити в Gemini чохли, запчастини, MacBook Pro та M3/M4,
і мати хоч якісь RAM/SSD для порівняння цін, навіть якщо AI недоступний.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import Listing, Specs

# Номери моделей Apple, які однозначно визначають чіп.
MODEL_NUMBERS = {
    "a2337": ("M1", "13"),
    "a2681": ("M2", "13"),
    "a2941": ("M2", "15"),
    "a3113": ("M3", "13"),
    "a3114": ("M3", "15"),
    "a3240": ("M4", "13"),
    "a3241": ("M4", "15"),
    "a2179": ("Intel", "13"),
    "a1932": ("Intel", "13"),
    "a1466": ("Intel", "13"),
}

_CHIP_RE = re.compile(r"(?<![a-zа-яіїє0-9])[mм]\s?([1-5l])(?:\s?(pro|max|ultra))?(?![0-9a-zа-я])")
_AIR_RE = re.compile(r"(?<![a-z])(air|аир|аір|эйр|ейр)(?![a-zа-я])")
# Intel-маркери: процесор або рік випуску, коли Air ще був на Intel (≤ 2019).
_INTEL_RE = re.compile(r"intel|интел|інтел|(?<![a-z0-9])i[357](?![0-9a-z])|core\s*i\d|(?<!\d)201\d(?!\d)")
# SEO-хвости: «… не макбук про м1 м2 м3 м4», «M2 (ні M3)» — усе після заперечення ігноруємо.
_NEGATION_RE = re.compile(r"(?<![a-zа-яіїє])(не|ні|not)(?![a-zа-яіїє])")
_ACCESSORY_FOR_RE = re.compile(r"\b(для|for|под|під)\s+(apple\s+)?(macbook|макбук|mac\b)")
_ACCESSORY_START_RE = re.compile(
    r"^\W*(чохол|чехол|накладк|кейс|case|плівк|пленк|скло|стекло|зарядк|зарядн|адаптер|блок\s+живлен|"
    r"клавіатур|клавиатур|матриц|дисплей|екран|экран|топкейс|корпус|кришк|крышк|плата|шлейф|кабель|"
    r"хаб|hub|підставк|подставк|наклейк|сумк|рюкзак|акумулятор|аккумулятор|батаре|коробк|кулер|динамік|"
    r"запчаст|док-станц|док\s+станц)"
)
_BROKEN_RE = re.compile(
    r"(на\s+запчаст|донор|розбор|разбор|не\s+вмика|не\s+включа|icloud\s*lock|залочен|заблокован|"
    r"під\s+ремонт|под\s+ремонт|mdm\s*lock)"
)
_PAIR_RE = re.compile(
    r"(?<!\d)(8|16|24)\s*(?:gb|гб|g)?\s*(?:ram)?\s*[/\\|]\s*(128|256|512|1|2)\s*(tb|тб|gb|гб|ssd)?(?!\d)"
)
_RAM_RE = [
    re.compile(r"(?<!\d)(8|16|24)\s*(?:gb|гб)\s*(?:ram|озу|оперативн|unified|об.?єднан|пам)"),
    re.compile(r"(?:ram|озу|оперативн\w*|пам.?ять)\W{0,4}(8|16|24)(?!\d)"),
]
_SSD_TB_RE = re.compile(r"(?<!\d)(1|2)\s*(?:tb|тб)(?![a-zа-я])")
_SSD_GB_RE = re.compile(r"(?<!\d)(128|256|512)\s*(?:gb|гб|g)?(?!\d)")
_SCREEN15_RE = re.compile(r"(?<![\d.,])15(?:[.,]3)?\s*(?:\"|”|″|''|inch|дюйм|-inch)|\b15\s*(?:air|аір)|air\s*15(?!\d)")
_BAT_PCT_RE = [
    re.compile(r"(?:акб|батаре\w*|акумулятор\w*|аккумулятор\w*|battery|ємн\w*|емкост\w*|здоров\w*|health|стан\s+бат\w*)[^\d\n]{0,25}(\d{2,3})\s*%"),
    re.compile(r"(\d{2,3})\s*%\s*(?:акб|батаре|акум|аккум|ємн|емк|battery|здоров)"),
]
_CYCLES_RE = [
    re.compile(r"(?<!\d)(\d{1,4})\s*(?:цикл\w*|cycles?|cycle\s+count)"),
    re.compile(r"(?:цикл\w*|cycles?|cycle\s+count)\W{0,6}(\d{1,4})(?!\d)"),
]


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _strip_negated_tail(title: str) -> str:
    m = _NEGATION_RE.search(title)
    return title[: m.start()] if m else title


def _chips(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).replace("l", "1"), m.group(2) or "") for m in _CHIP_RE.finditer(text)]


def detect_chip(title: str, description: str) -> tuple[str | None, str | None]:
    """Повертає (chip, reject_reason)."""
    t, d = _strip_negated_tail(_norm(title)), _norm(description)

    for code, (chip, _) in MODEL_NUMBERS.items():
        if code in t:
            return (chip, None) if chip in ("M1", "M2") else (None, f"модель {chip}")

    found = _chips(t)
    if not found and _INTEL_RE.search(t):
        return None, "Intel"
    if any(suffix for _, suffix in found):
        return None, "чіп Pro/Max"
    nums = {n for n, _ in found}
    if len(nums) > 1:
        return None, "кілька чіпів у заголовку"
    if nums:
        n = nums.pop()
        return (f"M{n}", None) if n in ("1", "2") else (None, f"чіп M{n}")

    for code, (chip, _) in MODEL_NUMBERS.items():
        if code in d:
            return (chip, None) if chip in ("M1", "M2") else (None, f"модель {chip}")
    # Чіп з опису беремо, лише якщо там немає згадок Intel: магазини вставляють шаблони про інші товари.
    nums = {n for n, suffix in _chips(d) if not suffix}
    if len(nums) == 1 and nums <= {"1", "2"} and not re.search(r"intel|интел|інтел|core\s*i\d", d):
        return f"M{nums.pop()}", None
    return None, "чіп не визначено"


def _first(patterns: list[re.Pattern[str]], *texts: str) -> int | None:
    for text in texts:
        for p in patterns:
            m = p.search(text)
            if m:
                return int(m.group(1))
    return None


def extract_memory(title: str, description: str) -> tuple[int | None, int | None]:
    ram = ssd = None
    for text in (_norm(title), _norm(description)):
        m = _PAIR_RE.search(text)
        if m:
            ram = ram or int(m.group(1))
            val, unit = int(m.group(2)), (m.group(3) or "")
            ssd = ssd or (val * 1024 if val <= 2 or unit in ("tb", "тб") else val)
        ram = ram or _first(_RAM_RE, text)
        if ssd is None:
            tb = _SSD_TB_RE.search(text)
            if tb:
                ssd = int(tb.group(1)) * 1024
            else:
                gb = _SSD_GB_RE.search(text)
                if gb:
                    ssd = int(gb.group(1))
        if ram and ssd:
            break
    return ram, ssd


def age_days(iso: str) -> int | None:
    """Скільки повних днів минуло від дати OLX (ISO з часовою зоною)."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return None


def classify(
    listing: Listing,
    min_price_uah: int,
    max_price_uah: int,
    uah_price: int | None,
    max_age_days: int | None = None,
) -> Specs:
    full_title = _norm(listing.title)
    title, desc = _strip_negated_tail(full_title), _norm(listing.description)
    specs = Specs()

    if "macbook pro" in title or "макбук про" in title or re.search(r"\bpro\b", title) and not _AIR_RE.search(title):
        specs.reject_reason = "MacBook Pro"
    elif not _AIR_RE.search(title) and "macbook" not in title and "макбук" not in title:
        specs.reject_reason = "не MacBook"
    elif not _AIR_RE.search(title) and not _AIR_RE.search(desc[:400]):
        specs.reject_reason = "не Air"
    elif _ACCESSORY_FOR_RE.search(title) or _ACCESSORY_START_RE.search(title):
        specs.reject_reason = "аксесуар/запчастина"
    elif _BROKEN_RE.search(title):
        specs.reject_reason = "несправний/на запчастини"
    elif uah_price is None:
        specs.reject_reason = "без ціни"
    elif uah_price < min_price_uah:
        specs.reject_reason = "підозріло дешево"
    elif uah_price > max_price_uah:
        specs.reject_reason = "задорого"

    chip, chip_reject = detect_chip(listing.title, listing.description)
    specs.chip = chip
    if specs.reject_reason is None and chip_reject:
        specs.reject_reason = chip_reject
    if specs.reject_reason is None and max_age_days is not None:
        days = age_days(listing.created_at)
        if days is not None and days > max_age_days:
            specs.reject_reason = f"висить > {max_age_days} дн."

    specs.ram_gb, specs.ssd_gb = extract_memory(listing.title, listing.description)
    full = f"{title}\n{desc}"
    if any(code in full for code, (_, size) in MODEL_NUMBERS.items() if size == "15") or _SCREEN15_RE.search(title):
        specs.screen = "15"
    else:
        specs.screen = "13"
    specs.battery_pct = _first(_BAT_PCT_RE, title, desc)
    if specs.battery_pct is not None and not 50 <= specs.battery_pct <= 100:
        specs.battery_pct = None
    specs.battery_cycles = _first(_CYCLES_RE, title, desc)
    if specs.battery_cycles is not None and specs.battery_cycles > 2000:
        specs.battery_cycles = None
    return specs


def to_uah(price: int | None, currency: str, usd_rate: float) -> int | None:
    if price is None:
        return None
    cur = (currency or "UAH").upper()
    if cur == "USD":
        return round(price * usd_rate)
    if cur == "EUR":
        return round(price * usd_rate * 1.08)
    return price
