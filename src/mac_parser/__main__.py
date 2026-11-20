"""Точка входу.

    uv run mac-parser bot      # Telegram-бот + автоскан кожні SCAN_INTERVAL_MIN хв
    uv run mac-parser scan     # один прохід без Telegram (OLX → БД → Gemini, якщо є ключ)
    uv run mac-parser show M1  # надрукувати картки з БД у консоль
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys

from .config import settings


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def _scan(analyze_limit: int | None) -> None:
    from .db import DB, now
    from .scanner import Scanner, ScanReport

    db = DB(settings.db_path)
    scanner = Scanner(db)
    try:
        report = ScanReport()
        await scanner.scrape(report)
        if analyze_limit != 0:
            await scanner.analyze_pending(report, analyze_limit)
        db.meta_set("last_scan", now())
        print(report.text())
    finally:
        await scanner.close()


def _show(section: str, sort: str, limit: int) -> None:
    from .cards import card_text
    from .db import DB
    from .pricing import market_for_row

    db = DB(settings.db_path)
    rows = db.visible(None if section == "TOP" else section, sort)
    print(f"{section}: {len(rows)} оголошень\n")
    for row in rows[:limit]:
        text = re.sub(r"<[^>]+>", "", card_text(row, market_for_row(db, row)))
        print(text, "\n", row["url"], "\n", "-" * 60, sep="")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="mac-parser")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("bot", help="запустити Telegram-бота")
    p_scan = sub.add_parser("scan", help="один прохід без бота")
    p_scan.add_argument("--analyze", type=int, default=None, help="скільки оголошень віддати в AI (0 — без AI)")
    p_cycle = sub.add_parser("cycle", help="один прохід для GitHub Actions: скан, AI, пуші, сайт")
    p_cycle.add_argument("--out", default="site", help="куди зібрати сайт")
    p_site = sub.add_parser("site", help="лише зібрати сайт з БД")
    p_site.add_argument("--out", default="site")
    p_show = sub.add_parser("show", help="показати картки з БД")
    p_show.add_argument("section", nargs="?", default="TOP", choices=["M1", "M2", "TOP"])
    p_show.add_argument("--sort", default="score", choices=["score", "price", "new"])
    p_show.add_argument("-n", type=int, default=10)
    args = parser.parse_args()

    _setup_logging()
    if args.cmd == "scan":
        asyncio.run(_scan(args.analyze))
    elif args.cmd == "cycle":
        from pathlib import Path

        from .cloud import run_cycle

        asyncio.run(run_cycle(Path(args.out)))
    elif args.cmd == "site":
        from pathlib import Path

        from . import site
        from .db import DB

        print(site.build(DB(settings.db_path), Path(args.out)), "оголошень")
    elif args.cmd == "show":
        _show(args.section, args.sort, args.n)
    else:
        from .bot import run

        asyncio.run(run())


if __name__ == "__main__":
    main()
