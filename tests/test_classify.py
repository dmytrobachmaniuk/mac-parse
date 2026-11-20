import json

import pytest

from mac_parser.classify import classify, detect_chip, extract_memory
from mac_parser.models import Listing
from mac_parser.sources import olx


def make(title: str, desc: str = "", price: int = 25000) -> Listing:
    return Listing(
        source="olx", ext_id="1", url="u", title=title, description=desc, price=price, currency="UAH",
        negotiable=False, photos=[], city="Київ", region="", is_business=False, seller_name="",
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), refreshed_at="", params={},
    )


@pytest.mark.parametrize(
    ("title", "chip", "reason"),
    [
        ("MacBook Air M1 16GB RAM 256GB SSD", "M1", None),
        ("MacBook Air 13 2022  (M2/16 gb/256 ssd) Гарантія. 55350", "M2", None),
        ("100% АКБ| М1 8gb |256gb Macbook Air 13 2020(2022)", "M1", None),  # кирилична «М»
        ("Продам Apple Macbook Air13 m1", "M1", None),
        ("Macbook air ml 2020 року колір space gray", "M1", None),
        ("MacBook Air M1 8/512gb 2020 Neverlock не макбук про м1 м2 м3 м4 м5 intel", "M1", None),
        ("Open box! Macbook Air 13 M2(ні M3) 2022(2023) Макбук М2|8|256 Гарантія", "M2", None),
        ("Apple MacBook Air 13.6 M1/M2/M3/M4 256/512/1TB new", None, "кілька чіпів у заголовку"),
        ("Used MacBook Pro 13 M1 8Gb / 256Gb Gold", "M1", "MacBook Pro"),
        ("MacBook Air 13 M3 2024 8/256", None, "чіп M3"),
        ("Чохол на Macbook Air 13 M1", "M1", "аксесуар/запчастина"),
        ("Коробка 13-inch Apple MacBook Air M1 оригінал", "M1", "аксесуар/запчастина"),
        ("MacBook Air M2 на запчастини", "M2", "несправний/на запчастини"),
        ("MacBook Air 13 2020 Intel Core i5 16/256", None, "Intel"),
        ("МакБук Аир 13.3 2019 i5/8/256Гб Сильвер", None, "Intel"),
        ("Ноутбуки Apple (3шт) MacBook: Air 2020, Pro 2017, Air 2015", None, "Intel"),
        ("Macbook Air 13 2020(2023) M1 8Gb|256Gb ГАРАНТІЯ", "M1", None),
    ],
)
def test_classify_titles(title, chip, reason):
    specs = classify(make(title), 12000, 80000, 25000)
    assert specs.chip == chip
    assert specs.reject_reason == reason


def test_chip_from_description_and_model_number():
    assert detect_chip("MacBook Air 2020 Space Gray", "Процесор Apple M1, 8 ядер") == ("M1", None)
    assert detect_chip("MacBook Air 15 Midnight", "Модель A2941") == ("M2", None)
    assert detect_chip("MacBook Air 13", "модель A2179, Intel") == (None, "модель Intel")
    # Шаблон магазину з «Apple M2» в описі Intel-ноутбука не має робити його M2.
    assert detect_chip("MacBook Air 13.3 Silver", "MacBook Air Intel Core i5\nЧип: Apple M2") == (None, "чіп не визначено")


@pytest.mark.parametrize(
    ("title", "ram", "ssd"),
    [
        ("Macbook Air M1 8/256", 8, 256),
        ("macbook air m2 24gb/1tb", 24, 1024),
        ("MacBook Air 13 M1 13.3\" M1/ 8Gb RAM 128 Gb SSD", 8, 128),
        ("MacBook Air 13  M1 / RAM 16 / SSD 512 GB", 16, 512),
        ("Apple MacBook Air 13 M1 2020 8\\256 GB Silver", 8, 256),
    ],
)
def test_memory(title, ram, ssd):
    assert extract_memory(title, "") == (ram, ssd)


def test_price_bounds_and_screen():
    assert classify(make("MacBook Air M1 8/256"), 12000, 80000, 5500).reject_reason == "підозріло дешево"
    specs = classify(make("MacBook Air 15 2023 (M2 / 16 Gb / 512 SSD)"), 12000, 80000, 45000)
    assert (specs.chip, specs.screen, specs.reject_reason) == ("M2", "15", None)


def test_age_filter_uses_creation_date():
    from datetime import datetime, timedelta, timezone

    old = make("MacBook Air M1 8/256")
    old.created_at = (datetime.now(timezone.utc) - timedelta(days=173)).isoformat()
    old.refreshed_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()  # оновлене, але старе
    assert classify(old, 12000, 80000, 25000, max_age_days=37).reject_reason == "висить > 37 дн."
    fresh = make("MacBook Air M1 8/256")
    fresh.created_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    assert classify(fresh, 12000, 80000, 25000, max_age_days=37).reject_reason is None


def test_battery_and_cycles():
    specs = classify(make("MacBook Air M1 8/256", "Батарея 87%, 312 циклів"), 12000, 80000, 25000)
    assert (specs.battery_pct, specs.battery_cycles) == (87, 312)


def test_olx_page_parsing():
    ad = {
        "id": 917950450,
        "title": "MacBook Air 13 2020 (M1 / 8gb / 256)",
        "url": "https://www.olx.ua/d/uk/obyavlenie/x-ID107CQq.html",
        "description": "Стан ідеальний<br />\n<br />\nАКБ 90%",
        "isBusiness": True,
        "createdTime": "2026-03-19T21:49:58+02:00",
        "lastRefreshTime": "2026-10-22T20:26:30+03:00",
        "price": {"regularPrice": {"value": 29900, "currencyCode": "UAH", "negotiable": True}},
        "photos": ["https://ireland.apollo.olxcdn.com:443/v1/files/abc-UA/image;s=3024x3024"],
        "location": {"cityName": "Київ", "regionName": "Київська область"},
        "contact": {"name": "MacZone"},
        "params": [{"key": "state", "value": "Вживане", "normalizedValue": "used"}],
    }
    state = {"listing": {"listing": {"ads": [ad], "totalPages": 3}}}
    page = "<script>window.__PRERENDERED_STATE__= " + json.dumps(json.dumps(state)) + ";</script>"
    ads, total = olx.parse_page(page)
    assert total == 3
    a = ads[0]
    assert (a.ext_id, a.price, a.negotiable, a.is_business, a.city) == ("917950450", 29900, True, True, "Київ")
    assert a.description == "Стан ідеальний\n\nАКБ 90%"
    assert a.photos == ["https://ireland.apollo.olxcdn.com:443/v1/files/abc-UA/image;s=1000x700"]
    assert a.params == {"state": "used"}
    assert "q-macbook-air-m1" in olx.search_url("macbook air m1")
