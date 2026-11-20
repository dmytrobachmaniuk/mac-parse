import json

from mac_parser import site
from mac_parser.db import DB
from tests.test_pipeline import AI, fill


def test_site_build(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    fill(db)
    db.save_analysis("olx:0", {**AI.model_dump(), "prompt_version": 3}, 20000)
    db.add_subscriber(123456)

    n = site.build(db, tmp_path / "site", ai_note="Ліміт AI вичерпано")
    data = json.loads((tmp_path / "site" / "data.json").read_text(encoding="utf-8"))

    assert n == 5 and len(data["listings"]) == 5
    assert (tmp_path / "site" / "index.html").exists() and (tmp_path / "site" / ".nojekyll").exists()
    first = next(item for item in data["listings"] if item["key"] == "olx:0")
    assert first["ai"]["deal_score"] == 9 and first["delta"] == -20.0 and first["median"] == 25000
    assert first["battery_source"] == "фото" and first["photos"] == ["https://x/1.jpg", "https://x/2.jpg"]
    assert data["ai_note"] == "Ліміт AI вичерпано" and data["analyzed"] == 1
    assert "123456" not in (tmp_path / "site" / "data.json").read_text(encoding="utf-8")  # chat id не на сайті
