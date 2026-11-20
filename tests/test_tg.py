"""Telegram-барахолки: ціна з тексту, альбом → оголошення, «куплю», дублікати, старі пости, інтеграція."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mac_parser import scanner as scanner_mod
from mac_parser.classify import classify
from mac_parser.db import DB
from mac_parser.sources import tg


@pytest.mark.parametrize(
    ("text", "price"),
    [
        ("Продам MacBook Air M1 8/256 2020, ціна 26 000 грн, торг", (26000, "UAH")),
        ("MacBook Air m2 16/512 — 650$", (650, "USD")),
        ("Air M1 8/256 батарея 92%, 24к", (24000, "UAH")),
        ("macbook air m1 2020 8 256 цена 23500", (23500, "UAH")),
        ("Macbook air m2 $720", (720, "USD")),
        ("макбук аір м1, 580 usd", (580, "USD")),
        ("MacBook Air M1 8/256 2020", (None, "UAH")),  # 8/256 і 2020 — не ціна
        ("MacBook Air M2 2022 512, 31.500 грн", (31500, "UAH")),
    ],
)
def test_parse_price(text, price):
    assert tg.parse_price(text) == price


@pytest.mark.parametrize(
    ("text", "ok"),
    [("Air M1 8/256, 24к", True), ("М2 Air 13 midnight", True), ("Продам MacBook Air", True),
     ("AirPods Pro 2", False), ("iPhone 13 128", False), ("Airpods max m1?", False)],
)
def test_looks_like_sale(text, ok):
    assert tg.looks_like_sale(text) is ok


def msg(mid, text="", photo=False, gid=None, days_ago=1):
    return SimpleNamespace(id=mid, message=text, photo=object() if photo else None, grouped_id=gid,
                           date=datetime.now(timezone.utc) - timedelta(days=days_ago))


def test_album_becomes_one_listing():
    album = [msg(11, "", True, 5), msg(10, "Продам MacBook Air M1 8/256\nАКБ 91%, 23 500 грн, торг", True, 5)]
    sender = SimpleNamespace(id=42, first_name="Ігор", last_name=None, username="igor_mac")
    item = tg.build_listing("apple_pub_ua", album, sender, ["tgphotos/apple_pub_ua_10.jpg"])
    assert (item.key, item.url) == ("tg:apple_pub_ua/10", "https://t.me/apple_pub_ua/10")
    assert (item.price, item.currency, item.negotiable) == (23500, "UAH", True)
    assert item.title == "Продам MacBook Air M1 8/256" and item.seller_id == "tg:42"
    assert item.seller_name == "Ігор (@igor_mac)" and not item.is_business
    assert tg.build_listing("x", [msg(1, "Продам iPhone 13")], sender, []) is None


def test_classify_rejects_buy_posts():
    want = tg.build_listing("c", [msg(1, "Куплю MacBook Air M1 до 20к")], None, [])
    assert classify(want, 12000, 80000, 20000).reject_reason == "куплю/шукаю"
    sell = tg.build_listing("c", [msg(2, "Продам MacBook Air M1 8/256, 24к")], None, [])
    assert classify(sell, 12000, 80000, 24000).reject_reason is None


def test_supersede_and_expire(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    sender = SimpleNamespace(id=7, first_name="A", last_name=None, username=None)
    old = tg.build_listing("c", [msg(1, "MacBook Air M1 8/256, 25к", days_ago=3)], sender, [])
    new = tg.build_listing("c", [msg(9, "MacBook Air M1 8/256, 24к", days_ago=0)], sender, [])
    ancient = tg.build_listing("c", [msg(3, "MacBook Air M2 8/256, 30к", days_ago=50)], None, [])
    for item, price in ((old, 25000), (new, 24000), (ancient, 30000)):
        db.upsert(item, classify(item, 12000, 80000, price), price)
    assert db.supersede_tg(new.key, new.seller_id, new.title) == 1
    assert db.get(old.key)["active"] == 0 and db.get(old.key)["gone_at"] is None  # не «продано»
    assert db.expire_tg(37) == 1 and db.get(ancient.key)["active"] == 0
    assert db.get(new.key)["active"] == 1


def test_scanner_tg_integration(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.sqlite3")
    sender = SimpleNamespace(id=7, first_name="A", last_name=None, username=None)
    items = [tg.build_listing("apple_pub_ua", [msg(5, "Продам MacBook Air M2 8/256, 28 000 грн")], sender, [])]

    async def fake_fetch(_db):
        return items, {"apple_pub_ua": 5}, []

    monkeypatch.setattr(scanner_mod.tg, "configured", lambda: True)
    monkeypatch.setattr(scanner_mod.tg, "fetch", fake_fetch)
    sc = object.__new__(scanner_mod.Scanner)
    sc.db, sc.rates = db, {"USD": 41.0, "EUR": 44.0}
    report = scanner_mod.ScanReport()
    asyncio.run(sc._tg(report))
    assert report.by_source == {"tg": 1} and report.new == 1 and not report.failed_sources
    assert db.meta_get("tg_last_id:apple_pub_ua") == "5"
    row = db.get("tg:apple_pub_ua/5")
    assert (row["chip"], row["price_uah"], row["reject_reason"]) == ("M2", 28000, None)


def test_spam_ipad_and_urgent_buy_filtered():
    assert not tg.looks_like_sale("🔥 АКТУАЛЬНИЙ ПРАЙС | ОПТ 🔥 MacBook Air M1 — 600$")
    assert not tg.looks_like_sale("IPHONE DUO 256 — 182 000 ₽; MacBook Air M2 — 95 000 ₽")
    assert not tg.looks_like_sale("iPad Air 13 M2 128 gb used, 20000 грн")
    urgent = tg.build_listing("c", [msg(1, "❗️Срочно куплю макбук аір 13 м2 ❗️")], None, [])
    assert classify(urgent, 12000, 80000, 25000).reject_reason == "куплю/шукаю"
    # «Продам терміново» — не «куплю»
    sale = tg.build_listing("c", [msg(2, "Продам терміново MacBook Air M1 8/256, 23к")], None, [])
    assert classify(sale, 12000, 80000, 23000).reject_reason is None
