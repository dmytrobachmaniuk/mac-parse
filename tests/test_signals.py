"""Сигнали продавця, ринок з проданих, перевиставлення, алерти, Prom, курс."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mac_parser import cloud
from mac_parser.classify import classify, extract_serial, to_uah
from mac_parser.db import DB
from mac_parser.pricing import market_for_row
from mac_parser.signals import repost_info, seller_info, sold_for_row
from mac_parser.sources import prom
from tests.test_classify import make
from tests.test_pipeline import AI


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def add(db: DB, ext_id: str, price: int, **kw):
    item = make("MacBook Air M1 8/256", "АКБ 90%", price)
    item.ext_id = ext_id
    for k, v in kw.items():
        setattr(item, k, v)
    db.upsert(item, classify(item, 12000, 80000, price), price)
    return db.get(f"olx:{ext_id}")


def test_extract_serial():
    assert extract_serial(["Серійний номер: FVHGW4GQQ6L4"]) == "FVHGW4GQQ6L4"
    assert extract_serial(["Model A2337 EMC 3598 Serial C02F40ELQ6LX"]) == "C02F40ELQ6LX"
    assert extract_serial(["Серийный номер JN50WFK96J", "x"]) == "JN50WFK96J"
    assert extract_serial(["Пам'ять: 8 ГБ", "Батарея 412 циклів"]) is None


def test_seller_signals(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    for i, p in enumerate([25000, 25000, 26000, 24000]):
        add(db, f"m{i}", p)
    scam = add(db, "scam", 17000, seller_id="olx:1", seller_created=iso(5))
    s = seller_info(db, scam, market_for_row(db, scam).delta_pct(17000))
    assert any("шахрайства" in f for f in s.flags)

    for i in range(3):
        add(db, f"r{i}", 26000, seller_id="olx:2", seller_created=iso(2000), seller_verified=True)
    r = seller_info(db, db.get("olx:r0"), 0)
    assert r.active_ads == 3 and any("перекуп" in f for f in r.flags)
    assert "верифікований" in r.notes and any("на майданчику" in n for n in r.notes)

    idle = add(db, "idle", 25000, seller_id="olx:3", seller_created=iso(900), seller_last_seen=iso(30))
    assert any("не заходив" in f for f in seller_info(db, idle, 0).flags)


def test_sold_market_and_repost(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    for i, p in enumerate([20000, 21000, 22000, 30000]):
        add(db, f"s{i}", p, created_at=iso(10))
    # «бачили» їх 2 дні тому, потім зникли — продані
    db.conn.execute("UPDATE listings SET first_seen=?", (iso(2),))
    db.deactivate_missing("olx", set())
    cur = add(db, "now", 25000)
    sold = sold_for_row(db, cur)
    assert (sold.median, sold.count) == (21500, 4) and sold.days_to_sell == 10

    # той самий ноутбук за серійником: старе оголошення з іншою ціною
    db.save_analysis("olx:s0", {**AI.model_dump(), "photo_facts": ["Серійний номер: FVHGW4GQQ6L4"]}, 20000)
    db.save_analysis("olx:now", {**AI.model_dump(), "photo_facts": ["Serial FVHGW4GQQ6L4"]}, 25000)
    rp = repost_info(db, db.get("olx:now"))
    assert rp and rp.by == "серійник" and rp.first_price == 20000
    assert repost_info(db, db.get("olx:s0")) is None  # найстаріше — не перевиставлене


def test_price_drop_alert_queue(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    row = add(db, "d", 30000)
    db.save_analysis(row["key"], {**AI.model_dump(), "deal_score": 8}, 30000)
    db.mark_notified([row["key"]])
    assert db.price_drops(7) == []
    item = make("MacBook Air M1 8/256", "АКБ 90%", 28000)
    item.ext_id = "d"
    db.upsert(item, classify(item, 12000, 80000, 28000), 28000)
    assert db.price_drops(7) == []  # ще нема свіжого аналізу з новою ціною
    db.save_analysis(row["key"], {**AI.model_dump(), "deal_score": 8}, 28000)
    assert [r["key"] for r in db.price_drops(7)] == ["olx:d"]
    db.mark_notified(["olx:d"])
    assert db.price_drops(7) == [] and db.get("olx:d")["notified_price"] == 28000


SEARCH_HTML = "<script>window.ApolloCacheState = " + json.dumps({"_FAST_CACHE": {
    'SearchListingQuery{"search_term":"x"}': {"result": {"products": [
        {"product": {"id": 11, "name": "MacBook Air M1 8/256 б/у", "price": "22500", "priceCurrency": "UAH",
                     "urlText": "macbook-air", "image640": "https://i/1.jpg", "company": {"name": "Next Laptop"},
                     "company_id": 7, "presence": {"isAvailable": True}}},
        {"product": {"id": 12, "name": "Чохол для MacBook Air", "price": "900", "urlText": "case"}},
        {"product": {"id": 13, "name": "Lenovo IdeaPad", "price": "9000", "urlText": "len"}},
    ]}}}}) + ";</script>"
PRODUCT_HTML = '<script type="application/ld+json">' + json.dumps({
    "@type": "Product", "description": "Стан<br>ідеальний", "image": ["https://images.prom.ua/1_w640_h640_a.jpg"],
    "offers": {"price": "22000", "itemCondition": "https://schema.org/UsedCondition"}}) + "</script>"


def test_prom_parsing():
    cands = prom.parse_search(SEARCH_HTML)
    assert [c["id"] for c in cands] == ["11", "12"]  # Lenovo відсіяно; чохол відсіє classify
    c = cands[0]
    assert c["url"] == "https://prom.ua/ua/p11-macbook-air.html" and c["company"] == "Next Laptop"
    d = prom.parse_product(PRODUCT_HTML)
    assert d == {"description": "Стан\nідеальний", "photos": ["https://images.prom.ua/1_w1280_h1280_a.jpg"],
                 "condition": "used", "price": 22000}
    item = prom.to_listing(c, d, iso(0))
    assert (item.key, item.price, item.is_business, item.seller_id) == ("prom:11", 22000, True, "prom:7")
    new = prom.to_listing(c, {**d, "condition": "new"}, iso(0))
    assert classify(new, 12000, 80000, 22000).reject_reason == "новий (не вживаний)"
    assert classify(prom.to_listing(cands[1], d, iso(0)), 12000, 80000, 22000).reject_reason == "аксесуар/запчастина"


def test_to_uah_rates():
    assert to_uah(500, "USD", 41.0) == 20500
    assert to_uah(500, "EUR", 41.0, 44.0) == 22000
    assert to_uah(20000, "UAH", 41.0) == 20000


class FakeTg:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


def test_service_alerts(tmp_path):
    object.__setattr__(cloud.settings, "telegram_chat_ids", {1})
    db = DB(tmp_path / "t.sqlite3")
    tg = FakeTg()
    ok = SimpleNamespace(failed_sources=[], errors=[])
    broken = SimpleNamespace(failed_sources=["olx"], errors=["OLX 403 на сторінці 1"])

    asyncio.run(cloud.send_service_alerts(tg, db, ok))
    asyncio.run(cloud.send_service_alerts(tg, db, ok))
    assert len(tg.sent) == 1 and "підключено" in tg.sent[0]  # привітання лише раз

    asyncio.run(cloud.send_service_alerts(tg, db, broken))
    asyncio.run(cloud.send_service_alerts(tg, db, broken))
    assert len(tg.sent) == 2 and "OLX не відповідає" in tg.sent[1]  # тривога не частіше ніж раз на 6 год

    asyncio.run(cloud.send_service_alerts(tg, db, ok))
    assert len(tg.sent) == 3 and "знову працює" in tg.sent[2]
    object.__setattr__(cloud.settings, "telegram_chat_ids", set())


def test_migration_backfills_gone_at(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = DB(path)
    add(db, "x", 25000)
    db.conn.execute("UPDATE listings SET active=0")
    db.conn.execute("ALTER TABLE listings DROP COLUMN gone_at")
    db.conn.commit()
    db.conn.close()
    db2 = DB(path)
    row = db2.get("olx:x")
    assert row["gone_at"] == row["last_seen"]
    assert isinstance(db2.conn, sqlite3.Connection)
