"""AI-аналіз оголошення через Gemini: фото + опис → структурований висновок."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import date

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from .config import settings
from .pricing import Market

log = logging.getLogger(__name__)


# Підняти, коли змінюється промпт/схема: старі аналізи автоматично переробляться.
PROMPT_VERSION = 3


def is_fresh(row: sqlite3.Row) -> bool:
    """Чи актуальний AI-аналіз: є, зроблений за поточної ціни й поточною версією промпту."""
    if not row["analysis"] or row["analyzed_price"] != row["price_uah"]:
        return False
    return json.loads(row["analysis"]).get("prompt_version", 1) >= PROMPT_VERSION


class Analysis(BaseModel):
    is_relevant: bool = Field(
        description="true лише якщо продається сам ноутбук MacBook Air на M1 або M2 у робочому стані. "
        "false для аксесуарів, запчастин, обміну без продажу, MacBook Pro, M3/M4, Intel, «куплю»."
    )
    chip: str = Field(description="'M1', 'M2' або 'інше'")
    ram_gb: int | None = Field(None, description="Оперативна пам'ять, ГБ")
    ssd_gb: int | None = Field(None, description="Накопичувач, ГБ (1 ТБ = 1024)")
    screen_inch: float | None = Field(None, description="Діагональ: 13.3, 13.6 або 15.3")
    color: str | None = None
    photo_facts: list[str] = Field(
        default_factory=list,
        description="Що прочитано з фото дослівно: скріни «Про цей Mac» (чіп, пам'ять, рік, серійник), "
        "«Звіт про систему → Живлення» (цикли, макс. ємність, стан), «Сховище», наклейки, коробка. "
        "Кожен факт окремим пунктом, з цифрами. Порожньо, якщо таких фото нема.",
    )
    battery_health_pct: int | None = Field(
        None, description="Максимальна ємність батареї у %. Спершу шукай на скрінах, потім в описі"
    )
    battery_cycles: int | None = Field(
        None, description="Кількість циклів (Cycle Count). Спершу шукай на скрінах, потім в описі"
    )
    battery_source: str = Field(
        description="Звідки дані про батарею: 'фото' (видно на скріні), 'опис' (лише зі слів продавця) або 'невідомо'"
    )
    condition_score: int = Field(description="Зовнішній стан 1..10 за фото й описом (10 — як новий)")
    visual_defects: list[str] = Field(default_factory=list, description="Що видно на фото: подряпини, вм'ятини, плями на екрані тощо")
    kit: list[str] = Field(default_factory=list, description="Комплект: коробка, оригінальна зарядка, чек, гарантія")
    red_flags: list[str] = Field(
        default_factory=list,
        description="Ризики: iCloud/MDM-блок, був ремонт/заміна, батарея 'Service', передоплата, стокові фото "
        "замість реальних, розмиті/чужі фото, невідповідність опису й фото, «оцінка ринку» у перекупа, тощо",
    )
    pros: list[str] = Field(default_factory=list, description="Сильні сторони пропозиції")
    seller_type: str = Field(description="'приватна особа' або 'магазин/перекуп'")
    price_verdict: str = Field(description="'нижче ринку', 'в ринку' або 'вище ринку'")
    deal_score: int = Field(
        description="Підсумкова оцінка угоди 1..10: співвідношення ціни до стану, конфігурації й ризиків. "
        "8+ — варто дзвонити сьогодні; 5–7 — нормально; ≤4 — пропустити."
    )
    summary: str = Field(description="Висновок 1–3 речення українською, конкретно і по суті")
    questions_to_seller: list[str] = Field(default_factory=list, description="2–3 питання, які варто поставити продавцю")


SYSTEM = """Ти — досвідчений перекупник техніки Apple в Україні, який допомагає другу купити вживаний
MacBook Air M1 або M2 за найкращим співвідношенням ціна/стан. Ти скептичний: шукаєш приховані дефекти
й шахрайство, але не вигадуєш проблем, яких не видно. Відповідай українською. Оцінюй лише за наданими
фото й текстом; якщо чогось не видно — так і скажи, не домислюй.

Фото — головне джерело правди. Продавці часто фотографують екран зі скрінами macOS:
«Про цей Mac» / «About This Mac» (чіп, пам'ять, рік, серійний номер), «Звіт про систему → Живлення» /
«System Report → Power» (Cycle Count, Maximum Capacity, Condition), «Налаштування → Батарея → Стан»,
«Сховище». Уважно вчитуйся в кожну цифру на таких фото, навіть дрібну чи під кутом.
Якщо фото й опис розходяться (інший чіп, пам'ять, цикли, ємність) — вір фото й додай розбіжність у red_flags.
Якщо продавець пише про батарею, але скріна нема — battery_source = 'опис' і порадь попросити скрін.

Актуальні версії macOS: Sonoma 14, Sequoia 15, Tahoe 26 (нумерація перескочила з 15 на 26) і новіші.
Не вважай незнайому тобі нову версію macOS чи рік у копірайті підробкою — це не ризик."""


def _fmt_uah(v: int | None) -> str:
    return f"{v:,}".replace(",", " ") + " грн" if v else "невідомо"


SOURCE_NAMES = {"olx": "OLX", "prom": "Prom.ua", "tg": "Telegram"}


def build_prompt(row: sqlite3.Row, market: Market, extra: str = "") -> str:
    params = json.loads(row["params"] or "{}")
    extra_block = f"\nДодатково (наші дані):\n{extra}\n" if extra else ""
    return f"""Сьогодні {date.today().isoformat()}. Проаналізуй оголошення з {SOURCE_NAMES.get(row['source'], row['source'])}.

Заголовок: {row['title']}
Ціна: {row['price']} {row['currency']} (≈ {_fmt_uah(row['price_uah'])}){' , торг' if row['negotiable'] else ''}
Продавець: {'бізнес-акаунт' if row['is_business'] else 'приватна особа'} «{row['seller_name']}», {row['city']}
Створено: {row['created_at'][:10]}, оновлено: {row['refreshed_at'][:10]}
Параметри OLX: {json.dumps(params, ensure_ascii=False)}
Регулярки витягли: чіп={row['chip']}, RAM={row['ram_gb']}, SSD={row['ssd_gb']}, екран={row['screen']},
батарея={row['battery_pct']}%, цикли={row['battery_cycles']}

Ринок на OLX зараз для такої ж конфігурації: медіана {_fmt_uah(market.median)},
діапазон P25–P75 {_fmt_uah(market.low)} – {_fmt_uah(market.high)}, вибірка {market.count} оголошень.
(Якщо вибірка менша за 3 — спирайся на власне знання ринку вживаних MacBook в Україні.)
{extra_block}
Опис продавця:
\"\"\"
{(row['description'] or '')[:4000]}
\"\"\"

До повідомлення додано {{n}} фото з оголошення (у порядку, як у продавця).
Спершу переглянь кожне фото й випиши в photo_facts усе, що читається зі скрінів, наклейок і коробки;
потім заповни решту полів схеми, спираючись насамперед на ці факти."""


class _RateLimiter:
    def __init__(self, rpm: int):
        self.interval = 60.0 / max(rpm, 1)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class QuotaExceeded(RuntimeError):
    pass


class Analyzer:
    def __init__(self, http: httpx.AsyncClient):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY не задано")
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.http = http
        self.limiter = _RateLimiter(settings.gemini_rpm)
        # Модель → час (epoch), до якого її денна квота вичерпана.
        self.exhausted: dict[str, float] = {}

    async def _photos(self, urls: list[str]) -> list[types.Part]:
        parts: list[types.Part] = []
        for url in urls[: settings.photos_for_ai]:
            if not url.startswith("http"):  # фото з Telegram лежать локально (tgphotos/…)
                path = settings.tg_photo_dir.parent / url
                if path.exists():
                    parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg"))
                continue
            try:
                r = await self.http.get(url)
                if r.status_code == 200 and r.content:
                    mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
                    if not mime.startswith("image/"):
                        mime = "image/jpeg"
                    parts.append(types.Part.from_bytes(data=r.content, mime_type=mime))
            except httpx.HTTPError as e:
                log.warning("Фото не завантажилось %s: %s", url, e)
        return parts

    def available_models(self) -> list[str]:
        now = time.time()
        return [m for m in settings.gemini_models if self.exhausted.get(m, 0) <= now]

    def _mark_exhausted(self, model: str, err: Exception) -> None:
        # Безкоштовний рівень: ~20 запитів на добу на кожну модель. Gemini каже, скільки чекати.
        m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", str(err))
        wait = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))) if m else 3600
        self.exhausted[model] = time.time() + wait
        log.warning("Квота Gemini для %s вичерпана, пауза %.1f год; лишились: %s",
                    model, wait / 3600, ", ".join(self.available_models()) or "жодної")

    async def _generate(self, contents: list, config: types.GenerateContentConfig):
        """Пробує моделі по черзі: 429 (квота) → наступна модель, 503 (перевантаження) → наступна з паузою."""
        overloaded = 0
        while True:
            models = self.available_models()
            if not models:
                raise QuotaExceeded("усі моделі Gemini вичерпали денну квоту")
            model = models[overloaded % len(models)]
            await self.limiter.wait()
            try:
                resp = await self.client.aio.models.generate_content(model=model, contents=contents, config=config)
                return resp, model
            except genai_errors.ClientError as e:
                code = getattr(e, "code", None)
                if code == 429:
                    self._mark_exhausted(model, e)
                    continue
                if code == 400 and "thinking" in str(e).lower() and config.thinking_config:
                    # Деякі моделі не підтримують заданий рівень роздумів — без нього.
                    config = config.model_copy(update={"thinking_config": None})
                    continue
                if code == 404:  # модель прибрали/перейменували
                    self.exhausted[model] = time.time() + 86400
                    continue
                raise
            except genai_errors.ServerError as e:
                overloaded += 1
                if overloaded >= 2 * len(models) + 2:
                    raise
                log.warning("Gemini %s на %s, пробую далі", getattr(e, "code", "5xx"), model)
                await asyncio.sleep(5 * overloaded)

    async def analyze(self, row: sqlite3.Row, market: Market, extra: str = "") -> dict:
        photos = await self._photos(json.loads(row["photos"]))
        prompt = build_prompt(row, market, extra).replace("{n}", str(len(photos)))
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Analysis,
            temperature=0.2,
            # Без цього Gemini 3.x «думає» хвилинами; для оцінки оголошення вистачає low.
            thinking_config=types.ThinkingConfig(thinking_level=settings.gemini_thinking.upper()),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp, used = await self._generate([*photos, prompt], config)
        parsed = resp.parsed
        if isinstance(parsed, Analysis):
            result = parsed.model_dump()
        else:
            result = Analysis.model_validate_json(resp.text).model_dump()
        result["deal_score"] = max(1, min(10, int(result["deal_score"])))
        result["condition_score"] = max(1, min(10, int(result["condition_score"])))
        result["market_median"] = market.median
        result["market_count"] = market.count
        result["model"] = used
        result["prompt_version"] = PROMPT_VERSION
        return result
