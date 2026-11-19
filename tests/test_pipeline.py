"""Наскрізно без мережі: БД → ринок → аналізатор (фейковий Gemini) → картка."""

import asyncio
import re
from types import SimpleNamespace

from mac_parser import analyzer as analyzer_mod
from mac_parser.cards import CAPTION_LIMIT, caption
from mac_parser.classify import classify
from mac_parser.db import DB
from mac_parser.pricing import market_for_row
from tests.test_classify import make

AI = analyzer_mod.Analysis(
    is_relevant=True, chip="M1", ram_gb=8, ssd_gb=256, screen_inch=13.3, color="Space Gray",
    battery_health_pct=91, battery_cycles=180, condition_score=8,
    visual_defects=["дрібна подряпина на кришці " * 3] * 4, kit=["коробка", "оригінальна зарядка"],
    red_flags=["фото частково стокові " * 3] * 4, pros=["приватний продавець"] * 3,
    seller_type="приватна особа", price_verdict="нижче ринку", deal_score=9,
    summary="Хороший варіант: ціна на 12% нижча за медіану, батарея свіжа. " * 4,
    questions_to_seller=["Чи є iCloud прив'язка?"] * 3,
)


class FakeModels:
    async def generate_content(self, **kwargs):
        FakeModels.kwargs = kwargs
        return SimpleNamespace(parsed=AI, text=AI.model_dump_json())


class FakeHttp:
    async def get(self, url):
        return SimpleNamespace(status_code=200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})


def fill(db: DB) -> None:
    for i, price in enumerate([20000, 24000, 25000, 26000, 30000]):
        item = make("MacBook Air M1 8/256", "АКБ 91%", price)
        item.ext_id = str(i)
        item.photos = ["https://x/1.jpg", "https://x/2.jpg"]
        db.upsert(item, classify(item, 12000, 80000, price), price)


def test_pipeline(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    fill(db)
    row = db.get("olx:0")
    market = market_for_row(db, row)
    assert market.median == 25000 and market.count == 5
    assert round(market.delta_pct(row["price_uah"])) == -20

    an = object.__new__(analyzer_mod.Analyzer)
    an.client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    an.http = FakeHttp()
    an.limiter = analyzer_mod._RateLimiter(6000)
    result = asyncio.run(an.analyze(row, market))
    assert result["deal_score"] == 9 and result["market_median"] == 25000
    contents = FakeModels.kwargs["contents"]
    assert len(contents) == 3  # 2 фото + промпт
    assert "медіана 25 000 грн" in contents[-1] and "{n}" not in contents[-1]

    db.save_analysis(row["key"], result, row["price_uah"])
    assert [r["key"] for r in db.visible("M1", "score")][0] == "olx:0"
    assert db.needs_analysis(10)[0]["key"] != "olx:0"
    assert db.visible("M2", "score") == []

    text = caption(db.get("olx:0"), market)
    assert len(text) <= CAPTION_LIMIT
    assert "9/10" in text and "−20% від медіани 25 000 грн" in text and "🤖" in text
    # Теги збалансовані після обрізання.
    for tag in ("b", "i"):
        assert len(re.findall(f"<{tag}>", text)) == len(re.findall(f"</{tag}>", text))

    # Зміна ціни → оголошення знову в черзі на аналіз, історія ціни зберігається.
    item = make("MacBook Air M1 8/256", "АКБ 91%", 18000)
    item.ext_id = "0"
    assert db.upsert(item, classify(item, 12000, 80000, 18000), 18000) == "price_changed"
    assert "olx:0" in [r["key"] for r in db.needs_analysis(10)]
    assert "була 20 000 грн" in caption(db.get("olx:0"), market)

    # Зникле з OLX оголошення ховається.
    assert db.deactivate_missing("olx", {"olx:0", "olx:1"}) == 3
    assert len(db.visible("M1", "price")) == 2
