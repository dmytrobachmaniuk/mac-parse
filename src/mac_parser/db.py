"""SQLite-сховище оголошень, аналізів і підписників."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Listing, Specs

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key            TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    ext_id         TEXT NOT NULL,
    url            TEXT NOT NULL,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL,
    price          INTEGER,
    currency       TEXT,
    price_uah      INTEGER,
    negotiable     INTEGER,
    photos         TEXT NOT NULL,
    city           TEXT,
    region         TEXT,
    is_business    INTEGER,
    seller_name    TEXT,
    created_at     TEXT,
    refreshed_at   TEXT,
    params         TEXT,
    chip           TEXT,
    ram_gb         INTEGER,
    ssd_gb         INTEGER,
    screen         TEXT,
    battery_pct    INTEGER,
    battery_cycles INTEGER,
    reject_reason  TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    price_history  TEXT NOT NULL DEFAULT '[]',
    analysis       TEXT,
    analyzed_price INTEGER,
    analyzed_at    TEXT,
    ai_score       INTEGER,
    notified       INTEGER NOT NULL DEFAULT 0,
    photo_file_id  TEXT
);
CREATE INDEX IF NOT EXISTS ix_listings_chip ON listings(chip, active, reject_reason);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    notify  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- listings -------------------------------------------------------
    def upsert(self, listing: Listing, specs: Specs, price_uah: int | None) -> str:
        """Повертає 'new', 'price_changed' або 'same'."""
        ts = now()
        row = self.conn.execute("SELECT price_uah, price_history FROM listings WHERE key=?", (listing.key,)).fetchone()
        data = asdict(listing)
        values: dict[str, Any] = {
            **{k: data[k] for k in (
                "source", "ext_id", "url", "title", "description", "price", "currency", "city",
                "region", "seller_name", "created_at", "refreshed_at",
            )},
            "negotiable": int(listing.negotiable),
            "is_business": int(listing.is_business),
            "photos": json.dumps(listing.photos),
            "params": json.dumps(listing.params, ensure_ascii=False),
            "price_uah": price_uah,
            **{k: getattr(specs, k) for k in ("chip", "ram_gb", "ssd_gb", "screen", "battery_pct", "battery_cycles", "reject_reason")},
            "last_seen": ts,
            "active": 1,
        }
        if row is None:
            values.update(key=listing.key, first_seen=ts, price_history=json.dumps([[ts, price_uah]]))
            cols = ", ".join(values)
            marks = ", ".join(f":{k}" for k in values)
            self.conn.execute(f"INSERT INTO listings ({cols}) VALUES ({marks})", values)
            self.conn.commit()
            return "new"

        status = "same"
        if row["price_uah"] != price_uah:
            history = json.loads(row["price_history"])
            history.append([ts, price_uah])
            values["price_history"] = json.dumps(history)
            status = "price_changed"
        sets = ", ".join(f"{k}=:{k}" for k in values)
        self.conn.execute(f"UPDATE listings SET {sets} WHERE key=:key", {**values, "key": listing.key})
        self.conn.commit()
        return status

    def deactivate_missing(self, source: str, seen_keys: set[str]) -> int:
        rows = self.conn.execute("SELECT key FROM listings WHERE source=? AND active=1", (source,)).fetchall()
        gone = [r["key"] for r in rows if r["key"] not in seen_keys]
        self.conn.executemany("UPDATE listings SET active=0 WHERE key=?", [(k,) for k in gone])
        self.conn.commit()
        return len(gone)

    def get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM listings WHERE key=?", (key,)).fetchone()

    def needs_analysis(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM listings
               WHERE active=1 AND reject_reason IS NULL
                 AND (analysis IS NULL OR analyzed_price IS NOT price_uah)
               ORDER BY first_seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def save_analysis(self, key: str, analysis: dict, price_uah: int | None) -> None:
        self.conn.execute(
            "UPDATE listings SET analysis=?, analyzed_price=?, analyzed_at=?, ai_score=? WHERE key=?",
            (json.dumps(analysis, ensure_ascii=False), price_uah, now(), analysis.get("deal_score"), key),
        )
        self.conn.commit()

    def visible(self, chip: str | None, sort: str) -> list[sqlite3.Row]:
        """Активні оголошення, що пройшли фільтр і AI не визнав «не ноутбук»."""
        order = {
            "score": "COALESCE(ai_score, 0) DESC, price_uah ASC",
            "price": "price_uah ASC",
            "new": "first_seen DESC, created_at DESC",
        }[sort]
        where = (
            "active=1 AND reject_reason IS NULL AND (analysis IS NULL OR json_extract(analysis,'$.is_relevant') != 0)"
            " AND COALESCE(json_extract(analysis,'$.chip'), chip) IN ('M1','M2')"
        )
        args: list[Any] = []
        if chip:
            where += " AND COALESCE(json_extract(analysis,'$.chip'), chip)=?"
            args.append(chip)
        return self.conn.execute(f"SELECT * FROM listings WHERE {where} ORDER BY {order}", args).fetchall()

    def comparable_prices(self, chip: str, ram: int | None, ssd: int | None, screen: str | None) -> list[int]:
        rows = self.conn.execute(
            """SELECT price_uah FROM listings
               WHERE active=1 AND reject_reason IS NULL AND price_uah IS NOT NULL
                 AND COALESCE(json_extract(analysis,'$.chip'), chip)=?
                 AND COALESCE(json_extract(analysis,'$.ram_gb'), ram_gb) IS ?
                 AND COALESCE(json_extract(analysis,'$.ssd_gb'), ssd_gb) IS ?
                 AND COALESCE(screen,'13') = ?
                 AND (analysis IS NULL OR json_extract(analysis,'$.is_relevant') != 0)""",
            (chip, ram, ssd, screen or "13"),
        ).fetchall()
        return [r["price_uah"] for r in rows]

    def to_notify(self, min_score: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM listings WHERE active=1 AND notified=0 AND ai_score>=? AND reject_reason IS NULL "
            "AND json_extract(analysis,'$.is_relevant') != 0 ORDER BY ai_score DESC",
            (min_score,),
        ).fetchall()

    def mark_notified(self, keys: list[str]) -> None:
        self.conn.executemany("UPDATE listings SET notified=1 WHERE key=?", [(k,) for k in keys])
        self.conn.commit()

    def mark_all_notified(self) -> None:
        self.conn.execute("UPDATE listings SET notified=1")
        self.conn.commit()

    def set_photo_file_id(self, key: str, file_id: str) -> None:
        self.conn.execute("UPDATE listings SET photo_file_id=? WHERE key=?", (file_id, key))
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        q = self.conn.execute
        return {
            "total": q("SELECT COUNT(*) FROM listings").fetchone()[0],
            "active": q("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0],
            "passed": q("SELECT COUNT(*) FROM listings WHERE active=1 AND reject_reason IS NULL").fetchone()[0],
            "analyzed": q("SELECT COUNT(*) FROM listings WHERE active=1 AND analysis IS NOT NULL").fetchone()[0],
            "rejected": q(
                "SELECT reject_reason, COUNT(*) c FROM listings WHERE active=1 AND reject_reason IS NOT NULL "
                "GROUP BY reject_reason ORDER BY c DESC"
            ).fetchall(),
        }

    # ---- subscribers / meta --------------------------------------------
    def add_subscriber(self, chat_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO subscribers(chat_id) VALUES (?)", (chat_id,))
        self.conn.commit()

    def set_notify(self, chat_id: int, on: bool) -> None:
        self.conn.execute("INSERT OR REPLACE INTO subscribers(chat_id, notify) VALUES (?, ?)", (chat_id, int(on)))
        self.conn.commit()

    def subscribers(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT chat_id FROM subscribers WHERE notify=1")]

    def get_notify(self, chat_id: int) -> bool:
        r = self.conn.execute("SELECT notify FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r[0])

    def meta_get(self, k: str) -> str | None:
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, v))
        self.conn.commit()
