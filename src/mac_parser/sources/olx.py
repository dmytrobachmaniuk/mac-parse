"""Парсер OLX.ua.

Публічний API (/api/v1/offers) закритий CloudFront-ом (403), тому беремо
звичайну сторінку пошуку: у ній є `window.__PRERENDERED_STATE__` з тим самим
JSON, що віддає API (40 оголошень на сторінку, повний опис, фото, ціна).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
from urllib.parse import quote

import httpx

from ..models import Listing

log = logging.getLogger(__name__)

BASE = "https://www.olx.ua/uk/elektronika/noutbuki-i-aksesuary/noutbuki"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}
MAX_PAGES = 25  # OLX усе одно віддає максимум 25 сторінок

_TAG_RE = re.compile(r"<[^>]+>")


class OlxError(RuntimeError):
    pass


def search_url(query: str, page: int = 1) -> str:
    slug = quote(query.strip().lower().replace(" ", "-"))
    url = f"{BASE}/q-{slug}/?search%5Border%5D=created_at:desc"
    if page > 1:
        url += f"&page={page}"
    return url


def extract_state(page_html: str) -> dict:
    marker = "window.__PRERENDERED_STATE__"
    i = page_html.find(marker)
    if i < 0:
        raise OlxError("На сторінці немає __PRERENDERED_STATE__ (OLX змінив верстку або дав капчу)")
    tail = page_html[i:]
    raw, _ = json.JSONDecoder().raw_decode(tail[tail.index('"'):])
    return json.loads(raw)


def clean_description(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = html.unescape(_TAG_RE.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def big_photo(url: str, size: str = "1000x700") -> str:
    return re.sub(r";s=\d+x\d+", f";s={size}", url)


def parse_ad(ad: dict) -> Listing:
    price_block = ad.get("price") or {}
    regular = price_block.get("regularPrice") or {}
    location = ad.get("location") or {}
    contact = ad.get("contact") or {}
    user = ad.get("user") or {}
    params = {p.get("key", ""): str(p.get("normalizedValue") or p.get("value") or "") for p in ad.get("params") or []}
    return Listing(
        source="olx",
        ext_id=str(ad["id"]),
        url=ad.get("url") or f"https://www.olx.ua{ad.get('urlPath', '')}",
        title=(ad.get("title") or "").strip(),
        description=clean_description(ad.get("description") or ""),
        price=int(regular["value"]) if regular.get("value") is not None else None,
        currency=regular.get("currencyCode") or "UAH",
        negotiable=bool(regular.get("negotiable")),
        photos=[big_photo(p) for p in ad.get("photos") or []],
        city=location.get("cityName") or "",
        region=location.get("regionName") or "",
        is_business=bool(ad.get("isBusiness")),
        seller_name=contact.get("name") or user.get("name") or "",
        created_at=ad.get("createdTime") or "",
        refreshed_at=ad.get("lastRefreshTime") or ad.get("createdTime") or "",
        params=params,
        seller_id=f"olx:{user['id']}" if user.get("id") else "",
        seller_created=user.get("created") or user.get("memberSince") or "",
        seller_verified=(user.get("verification") or {}).get("status") not in (None, "", "none"),
        seller_last_seen=user.get("lastSeen") or "",
    )


def parse_page(page_html: str) -> tuple[list[Listing], int]:
    listing = extract_state(page_html)["listing"]["listing"]
    ads = [parse_ad(a) for a in listing.get("ads") or [] if a.get("id")]
    return ads, int(listing.get("totalPages") or 1)


async def _fetch_page(client: httpx.AsyncClient, query: str, page: int, attempts: int = 3) -> tuple[list[Listing], int]:
    """Одна сторінка з повторами: тимчасовий 5xx/429/обрив не має губити весь скан."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await client.get(search_url(query, page))
            if resp.status_code == 200:
                return parse_page(resp.text)
            last = OlxError(f"OLX {resp.status_code} на сторінці {page} для «{query}»")
            if resp.status_code in (403, 404):  # блок або сторінки нема — повтор не допоможе
                break
        except (httpx.HTTPError, OlxError) as e:
            last = e
        await asyncio.sleep(3 * (attempt + 1) + random.random())
    raise last if isinstance(last, OlxError) else OlxError(f"OLX недоступний ({last.__class__.__name__}) для «{query}»")


async def fetch_query(client: httpx.AsyncClient, query: str) -> list[Listing]:
    """Усі сторінки пошуку. Кидає OlxError, якщо не вдалося пройти до кінця."""
    seen: dict[str, Listing] = {}
    page, total = 1, 1
    while page <= min(total, MAX_PAGES):
        ads, total = await _fetch_page(client, query, page)
        for ad in ads:
            seen.setdefault(ad.ext_id, ad)
        log.info("OLX «%s»: сторінка %d/%d, +%d", query, page, total, len(ads))
        page += 1
        await asyncio.sleep(random.uniform(1.0, 2.5))
    return list(seen.values())


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True)
