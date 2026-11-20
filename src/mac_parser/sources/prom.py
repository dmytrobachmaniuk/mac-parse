"""Парсер Prom.ua (магазини, частина продає вживані MacBook).

- Пошук: дані лежать у `window.ApolloCacheState` (_FAST_CACHE → SearchListingQuery → products).
- Сторінка товару: schema.org Product у ld+json — опис, усі фото й `itemCondition`
  (UsedCondition / NewCondition). Беремо лише вживані.
Дати створення Prom не віддає — її ставить сканер (перша поява в нашій базі).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from urllib.parse import quote_plus

import httpx

from ..models import Listing
from .olx import HEADERS, clean_description

log = logging.getLogger(__name__)

BASE = "https://prom.ua"
QUERIES = ["macbook air m1 б/у", "macbook air m2 б/у"]
PAGES_PER_QUERY = 3  # пошук Prom шумний: далі третьої сторінки MacBook Air майже нема

_LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
_CANDIDATE_RE = re.compile(r"macbook|макбук", re.I)
_AIR_RE = re.compile(r"air|аір|эйр|ейр", re.I)


class PromError(RuntimeError):
    pass


def search_url(query: str, page: int = 1) -> str:
    url = f"{BASE}/ua/search?search_term={quote_plus(query)}"
    return url + (f"&page={page}" if page > 1 else "")


def product_url(pid: str | int, url_text: str) -> str:
    return f"{BASE}/ua/p{pid}-{url_text}.html"


def _apollo(page_html: str) -> dict:
    i = page_html.find("window.ApolloCacheState")
    if i < 0:
        raise PromError("На сторінці Prom нема ApolloCacheState (змінилась верстка або капча)")
    tail = page_html[i:]
    tail = tail[tail.index("=") + 1:].lstrip()
    return json.JSONDecoder().raw_decode(tail)[0].get("_FAST_CACHE", {})


def _find_products(obj) -> list | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "products" and isinstance(v, list):
                return v
            found = _find_products(v)
            if found is not None:
                return found
    return None


def parse_search(page_html: str) -> list[dict]:
    """Кандидати з пошуку: лише те, що за назвою схоже на MacBook Air."""
    cache = _apollo(page_html)
    key = next((k for k in cache if k.startswith("SearchListingQuery")), None)
    products = _find_products(cache[key]) if key else None
    out = []
    for item in products or []:
        p = item.get("product") or {}
        name = p.get("name") or ""
        if not p.get("id") or not _CANDIDATE_RE.search(name) or not _AIR_RE.search(name):
            continue
        company = p.get("company") if isinstance(p.get("company"), dict) else {}
        out.append({
            "id": str(p["id"]),
            "name": name.strip(),
            "price": int(float(p["price"])) if p.get("price") not in (None, "") else None,
            "currency": p.get("priceCurrency") or "UAH",
            "url": product_url(p["id"], p.get("urlText") or "item"),
            "image": p.get("image640") or p.get("image") or "",
            "company": (company or {}).get("name") or "",
            "company_id": str(p.get("company_id") or (company or {}).get("id") or ""),
            "available": ((p.get("presence") or {}).get("isAvailable") is not False),
        })
    return out


def parse_product(page_html: str) -> dict:
    """Опис, фото й стан зі сторінки товару (schema.org Product)."""
    for block in _LD_RE.findall(page_html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            images = data.get("image") or []
            if isinstance(images, str):
                images = [images]
            cond = str(offers.get("itemCondition") or "")
            return {
                "description": clean_description(data.get("description") or ""),
                "photos": [re.sub(r"_w\d+_h\d+_", "_w1280_h1280_", u) for u in images][:10],
                "condition": "used" if "Used" in cond else "new" if "New" in cond else "",
                "price": int(float(offers["price"])) if offers.get("price") not in (None, "") else None,
            }
    raise PromError("На сторінці товару Prom нема schema.org Product")


def to_listing(c: dict, details: dict, created_at: str) -> Listing:
    return Listing(
        source="prom",
        ext_id=c["id"],
        url=c["url"],
        title=c["name"],
        description=details.get("description", ""),
        price=details.get("price") or c["price"],
        currency=c["currency"],
        negotiable=False,
        photos=details.get("photos") or ([c["image"]] if c["image"] else []),
        city="",
        region="",
        is_business=True,  # на Prom продають магазини
        seller_name=c["company"],
        created_at=created_at,
        refreshed_at=created_at,
        params={"condition": details.get("condition", "")},
        seller_id=f"prom:{c['company_id']}" if c["company_id"] else "",
    )


async def _get(client: httpx.AsyncClient, url: str, attempts: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
            last = PromError(f"Prom {r.status_code}: {url}")
            if r.status_code in (403, 404):
                break
        except httpx.HTTPError as e:
            last = e
        await asyncio.sleep(2 * (attempt + 1) + random.random())
    raise last if isinstance(last, PromError) else PromError(f"Prom недоступний ({last.__class__.__name__})")


async def fetch_candidates(client: httpx.AsyncClient) -> list[dict]:
    seen: dict[str, dict] = {}
    for q in QUERIES:
        for page in range(1, PAGES_PER_QUERY + 1):
            found = parse_search(await _get(client, search_url(q, page)))
            for c in found:
                seen.setdefault(c["id"], c)
            log.info("Prom «%s»: сторінка %d, +%d MacBook Air", q, page, len(found))
            await asyncio.sleep(random.uniform(1.0, 2.0))
    return [c for c in seen.values() if c["available"]]


async def fetch_details(client: httpx.AsyncClient, c: dict) -> dict:
    return parse_product(await _get(client, c["url"]))
