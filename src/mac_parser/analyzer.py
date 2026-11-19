"""AI-аналіз оголошення через Gemini: фото + опис → структурований висновок."""

from __future__ import annotations

import asyncio
import json
import logging
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
    battery_health_pct: int | None = Field(None, description="Ємність батареї у %, якщо вказана або видна на скріні")
    battery_cycles: int | None = Field(None, description="Кількість циклів, якщо вказана або видна на скріні")
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
фото й текстом; якщо чогось не видно — так і скажи, не домислюй."""


def _fmt_uah(v: int | None) -> str:
    return f"{v:,}".replace(",", " ") + " грн" if v else "невідомо"


def build_prompt(row: sqlite3.Row, market: Market) -> str:
    params = json.loads(row["params"] or "{}")
    return f"""Сьогодні {date.today().isoformat()}. Проаналізуй оголошення з OLX.

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

Опис продавця:
\"\"\"
{(row['description'] or '')[:4000]}
\"\"\"

До повідомлення додано {{n}} фото з оголошення (у порядку, як у продавця).
Заповни всі поля схеми."""


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

    async def _photos(self, urls: list[str]) -> list[types.Part]:
        parts: list[types.Part] = []
        for url in urls[: settings.photos_for_ai]:
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

    async def analyze(self, row: sqlite3.Row, market: Market) -> dict:
        photos = await self._photos(json.loads(row["photos"]))
        prompt = build_prompt(row, market).replace("{n}", str(len(photos)))
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Analysis,
            temperature=0.2,
        )
        for attempt in range(4):
            await self.limiter.wait()
            try:
                resp = await self.client.aio.models.generate_content(
                    model=settings.gemini_model, contents=[*photos, prompt], config=config
                )
                break
            except genai_errors.ClientError as e:
                if getattr(e, "code", None) == 429:
                    raise QuotaExceeded(str(e)) from e
                raise
            except genai_errors.ServerError as e:
                # 503 «high demand» у Gemini зазвичай минає за хвилину.
                if attempt == 3:
                    raise
                delay = 15 * (attempt + 1)
                log.warning("Gemini %s, повтор через %d с", getattr(e, "code", "5xx"), delay)
                await asyncio.sleep(delay)
        parsed = resp.parsed
        if isinstance(parsed, Analysis):
            result = parsed.model_dump()
        else:
            result = Analysis.model_validate_json(resp.text).model_dump()
        result["deal_score"] = max(1, min(10, int(result["deal_score"])))
        result["condition_score"] = max(1, min(10, int(result["condition_score"])))
        result["market_median"] = market.median
        result["market_count"] = market.count
        result["model"] = settings.gemini_model
        return result
