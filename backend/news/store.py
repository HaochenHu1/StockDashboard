"""SQLite storage for news items and grouped events (kept 30 days, then deleted)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, event_id TEXT, source TEXT, source_name TEXT, kind TEXT, symbols TEXT, title TEXT,
  summary TEXT, url TEXT, lang TEXT, published REAL, time_precision TEXT, first_seen REAL, level TEXT,
  score REAL, matched TEXT, reasons TEXT, form TEXT, items TEXT, origin TEXT, ukey TEXT);
CREATE INDEX IF NOT EXISTS idx_items_event ON items(event_id);
CREATE INDEX IF NOT EXISTS idx_items_ukey ON items(ukey);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, symbols TEXT, title TEXT, titles TEXT, published REAL, last REAL, level TEXT, score REAL,
  l1_level TEXT, l2 TEXT, sources TEXT, kinds TEXT, keys TEXT, cats TEXT, count INTEGER, notified INTEGER,
  first_seen REAL, reasons TEXT, forced INTEGER);
CREATE INDEX IF NOT EXISTS idx_events_published ON events(published);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
_JSON_ITEM = ("symbols", "matched", "reasons", "items")
_JSON_EVENT = ("symbols", "titles", "l2", "sources", "kinds", "keys", "cats", "reasons")


class NewsStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- items
    def has_item(self, item_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is not None

    def add_item(self, it: dict) -> None:
        row = {k: (json.dumps(it.get(k), ensure_ascii=False) if k in _JSON_ITEM else it.get(k))
               for k in ("id", "event_id", "source", "source_name", "kind", "symbols", "title", "summary", "url",
                         "lang", "published", "time_precision", "first_seen", "level", "score", "matched",
                         "reasons", "form", "items", "origin", "ukey")}
        with self._lock:
            self._db.execute(f"INSERT OR IGNORE INTO items ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                             tuple(row.values()))
            self._db.commit()

    def all_items(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM items").fetchall()
        return [self._item(r) for r in rows]

    def delete_items(self, ids: list[str]) -> int:
        """Delete items, then events that no longer have any item."""
        if not ids:
            return 0
        with self._lock:
            n = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                n += self._db.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk).rowcount
            self._db.execute("DELETE FROM events WHERE id NOT IN (SELECT DISTINCT event_id FROM items WHERE event_id IS NOT NULL)")
            self._db.commit()
        return n

    def event_for_key(self, ukey: str | None) -> str | None:
        """Event of an already stored item that points to the same article / filing."""
        if not ukey:
            return None
        with self._lock:
            r = self._db.execute("SELECT event_id FROM items WHERE ukey=? LIMIT 1", (ukey,)).fetchone()
        return r[0] if r else None

    def event_items(self, event_ids: Iterable[str]) -> dict[str, list[dict]]:
        ids = list(event_ids)
        out: dict[str, list[dict]] = {i: [] for i in ids}
        if not ids:
            return out
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM items WHERE event_id IN ({','.join('?' * len(ids))}) "
                                    f"ORDER BY published", ids).fetchall()
        for r in rows:
            out[r["event_id"]].append(self._item(r))
        return out

    @staticmethod
    def _item(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in _JSON_ITEM:
            d[k] = json.loads(d[k]) if d.get(k) else []
        return d

    # --------------------------------------------------------------- events
    @staticmethod
    def _event(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in _JSON_EVENT:
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "l2" else None)
        return d

    def save_event(self, ev: dict) -> None:
        row = {k: (json.dumps(ev.get(k), ensure_ascii=False) if k in _JSON_EVENT else ev.get(k))
               for k in ("id", "symbols", "title", "titles", "published", "last", "level", "score", "l1_level", "l2",
                         "sources", "kinds", "keys", "cats", "count", "notified", "first_seen", "reasons", "forced")}
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO events ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                             tuple(row.values()))
            self._db.commit()

    def get_event(self, event_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return self._event(r) if r else None

    def recent_events(self, symbols: Iterable[str], since: float) -> list[dict]:
        syms = list(symbols)
        if not syms:
            return []
        clause = " OR ".join("symbols LIKE ?" for _ in syms)
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM events WHERE last >= ? AND ({clause})",
                                    [since] + [f'%"{s}"%' for s in syms]).fetchall()
        return [self._event(r) for r in rows]

    def list_events(self, *, symbol: str | None = None, levels: Iterable[str] | None = None,
                    source: str | None = None, since: float | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if symbol:
            sql += " AND symbols LIKE ?"
            args.append(f'%"{symbol}"%')
        lv = list(levels or [])
        if lv:
            sql += f" AND level IN ({','.join('?' * len(lv))})"
            args += lv
        if source:
            sql += " AND sources LIKE ?"
            args.append(f'%"{source}"%')
        if since:
            sql += " AND published >= ?"
            args.append(since)
        sql += " ORDER BY published DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._event(r) for r in rows]

    def latest_level_time(self, symbol: str, level: str, since: float) -> float | None:
        with self._lock:
            r = self._db.execute("SELECT MAX(published) FROM events WHERE level=? AND published >= ? AND symbols LIKE ?",
                                 (level, since, f'%"{symbol}"%')).fetchone()
        return r[0] if r and r[0] else None

    # ----------------------------------------------------------- retention
    def purge(self, days: float) -> int:
        cutoff = time.time() - days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM items WHERE published < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM events WHERE last < ?", (cutoff,))
            self._db.commit()
        return n

    # ----------------------------------------------------------------- meta
    def meta_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def meta_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             (key, json.dumps(value, ensure_ascii=False)))
            self._db.commit()

    def incr(self, key: str) -> int:
        n = int(self.meta_get(key, 0)) + 1
        self.meta_set(key, n)
        return n
