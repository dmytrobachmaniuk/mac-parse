"""Картка ніколи не чекає на Gemini: показується одразу, AI дописується у фоні."""

import asyncio
import time
from types import SimpleNamespace

from mac_parser import bot as botmod
from mac_parser.db import DB
from tests.test_pipeline import AI, fill


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []
        self.edited: list[str] = []
        self._id = 100

    async def send_message(self, chat_id, text, **kw):
        self._id += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self._id, chat=SimpleNamespace(id=chat_id), edit_text=self._edit)

    async def _edit(self, text, **kw):
        self.edited.append(text)

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)


class SlowAnalyzer:
    def __init__(self, exhausted: bool):
        self.exhausted = {"m": time.time() + 3600} if exhausted else {}

    def available_models(self):
        return [] if self.exhausted else ["m"]

    async def analyze(self, row, market):
        await asyncio.sleep(0.2)
        return {**AI.model_dump(), "prompt_version": botmod.is_fresh.__globals__["PROMPT_VERSION"]}


def setup(tmp_path, exhausted: bool) -> FakeBot:
    botmod.db = DB(tmp_path / "t.sqlite3")
    fill(botmod.db)
    botmod.scanner = SimpleNamespace(
        analyzer=SlowAnalyzer(exhausted),
        http=SimpleNamespace(get=lambda url: asyncio.sleep(0, SimpleNamespace(status_code=404, content=b""))),
    )
    botmod._analyzing.clear()
    return FakeBot()


def test_card_shown_immediately_when_quota_exhausted(tmp_path):
    fake = setup(tmp_path, exhausted=True)
    t = time.monotonic()
    asyncio.run(botmod.show_card(fake, 1, None, "M1", "price", 0))
    assert time.monotonic() - t < 0.5
    assert len(fake.sent) == 1
    assert "Ліміт AI на сьогодні вичерпано" in fake.sent[0] and "Показую дані з оголошення" in fake.sent[0]
    assert "MacBook Air M1" in fake.sent[0] and "без оцінки AI" in fake.sent[0]
    assert "Ліміт AI" in botmod.menu_text()


def test_card_redraws_after_background_analysis(tmp_path):
    fake = setup(tmp_path, exhausted=False)

    async def scenario():
        t = time.monotonic()
        await botmod.show_card(fake, 1, None, "M1", "price", 0)
        shown_after = time.monotonic() - t
        await asyncio.sleep(0.6)  # аналіз (0.2 с) + префетч наступної картки
        return shown_after

    shown_after = asyncio.run(scenario())
    assert shown_after < 0.2  # не чекали на AI
    assert "без оцінки AI" in fake.sent[0]
    assert any("Gemini аналізує" in e for e in fake.edited)
    assert "9/10" in fake.edited[-1] and "🤖" in fake.edited[-1]
    assert botmod.is_fresh(botmod.db.get("olx:0"))
