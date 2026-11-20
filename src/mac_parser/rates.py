"""Офіційний курс НБУ для оголошень у доларах / євро (запасний — з налаштувань)."""

from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)
NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"


def fallback() -> dict[str, float]:
    return {"USD": settings.usd_rate, "EUR": round(settings.usd_rate * 1.08, 2)}


async def fetch(client: httpx.AsyncClient) -> dict[str, float]:
    rates = fallback()
    try:
        r = await client.get(NBU_URL, timeout=15)
        r.raise_for_status()
        for item in r.json():
            if item.get("cc") in rates and item.get("rate"):
                rates[item["cc"]] = float(item["rate"])
        log.info("Курс НБУ: USD %.2f, EUR %.2f", rates["USD"], rates["EUR"])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Курс НБУ недоступний (%s) — беру запасний USD %.2f", e.__class__.__name__, rates["USD"])
    return rates
