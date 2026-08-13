#!/usr/bin/env python3
"""
news-digest collect.py

Liest ~/.hermes/news-digest/sources.yaml, fetched RSS/Atom-Feeds,
parsed Eintraege, dedupliziert ueber Titel-Hash + URL, schreibt
in SQLite-DB.

CLI:
    python3 collect.py                # alle Quellen
    python3 collect.py --source NAME  # nur eine Quelle
    python3 collect.py --dry-run      # nichts schreiben, nur Stats

Output: JSON-Stats auf stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import feedparser
import yaml

ROOT = Path.home() / ".hermes" / "news-digest"
CONFIG = ROOT / "sources.yaml"
DB = ROOT / "news.db"


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,
    url           TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    author        TEXT,
    summary       TEXT,
    published     TEXT,                  -- ISO-8601 oder leer
    topics        TEXT,                  -- comma-separated
    title_hash    TEXT    NOT NULL,      -- sha256(normalized title) fuer Cross-URL-Dedup
    fetched_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_title_hash ON articles(title_hash);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);
CREATE INDEX IF NOT EXISTS idx_articles_source     ON articles(source);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def title_hash(title: str) -> str:
    norm = " ".join((title or "").lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def open_db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- #
# Fetch + Parse
# --------------------------------------------------------------------------- #

def parse_date(entry) -> str | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        ts = getattr(entry, attr, None)
        if ts:
            try:
                return datetime(*ts[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return None


def topics_for(src_topics: list[str]) -> str:
    return ",".join(t.strip() for t in src_topics if t.strip())


def fetch_feed(source: dict, settings: dict) -> list[dict]:
    import socket
    ua = settings.get("user_agent", "NewsDigestBot/0.1")
    timeout = int(settings.get("timeout_sec", 15))
    # Hard-cap socket-Timeout bevor feedparser ewig haengt
    socket.setdefaulttimeout(timeout)
    try:
        feed = feedparser.parse(source["url"], agent=ua)
    finally:
        socket.setdefaulttimeout(None)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"feed error: {feed.bozo_exception}")
    out = []
    for e in feed.entries[: int(settings.get("max_items_per_feed", 20))]:
        url = (e.get("link") or "").strip()
        title = (e.get("title") or "").strip()
        if not url or not title:
            continue
        out.append({
            "url":       url,
            "title":     title,
            "author":    (e.get("author") or "").strip() or None,
            "summary":   (e.get("summary") or e.get("description") or "").strip() or None,
            "published": parse_date(e),
            "topics":    topics_for(source.get("topics", [])),
        })
    return out


# --------------------------------------------------------------------------- #
# Dedup + Insert
# --------------------------------------------------------------------------- #

def is_fresh(published_iso: str | None, max_age_days: int) -> bool:
    if not published_iso:
        return True  # unbekanntes Datum → behalten
    try:
        dt = datetime.fromisoformat(published_iso)
    except ValueError:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return dt >= cutoff


def upsert_articles(con: sqlite3.Connection, source_name: str,
                    items: list[dict], dry_run: bool) -> tuple[int, int, int]:
    """Returns (new, dup, fresh_rejected)."""
    cur = con.cursor()
    new, dup, rejected = 0, 0, 0
    for it in items:
        h = title_hash(it["title"])
        cur.execute("SELECT 1 FROM articles WHERE title_hash = ? OR url = ?",
                    (h, it["url"]))
        if cur.fetchone():
            dup += 1
            continue
        if not is_fresh(it["published"], max_age_days=3):
            rejected += 1
            continue
        if dry_run:
            new += 1
            continue
        try:
            cur.execute("""
                INSERT INTO articles (source, url, title, author, summary,
                                      published, topics, title_hash, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source_name, it["url"], it["title"], it["author"],
                  it["summary"], it["published"], it["topics"], h, now_iso()))
            new += 1
        except sqlite3.IntegrityError:
            dup += 1
    con.commit()
    return new, dup, rejected


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="nur diese Quelle (Name aus sources.yaml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="nicht schreiben, nur Stats")
    args = ap.parse_args()

    if not CONFIG.exists():
        print(f"fehler: config nicht gefunden: {CONFIG}", file=sys.stderr)
        return 2

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    sources = cfg.get("sources", [])
    settings = cfg.get("settings", {})

    if args.source:
        sources = [s for s in sources if s["name"] == args.source]
        if not sources:
            print(f"fehler: quelle nicht gefunden: {args.source}", file=sys.stderr)
            return 2

    con = open_db() if not args.dry_run else None
    totals = {"new": 0, "dup": 0, "rejected": 0, "errors": 0, "feeds": []}

    for src in sources:
        name = src["name"]
        try:
            items = fetch_feed(src, settings)
        except Exception as ex:
            totals["errors"] += 1
            totals["feeds"].append({
                "name": name, "ok": False, "error": str(ex)[:120],
                "new": 0, "dup": 0, "rejected": 0, "fetched": 0,
            })
            continue

        if con is not None:
            new, dup, rej = upsert_articles(con, name, items, args.dry_run)
        else:
            new = len(items)
            dup = rej = 0

        totals["new"] += new
        totals["dup"] += dup
        totals["rejected"] += rej
        totals["feeds"].append({
            "name": name, "ok": True,
            "fetched": len(items), "new": new, "dup": dup, "rejected": rej,
        })

    if con is not None:
        con.close()

    # Datenbank-Statistik
    if con is not None:
        cur = sqlite3.connect(DB).cursor()
        cur.execute("SELECT COUNT(*) FROM articles")
        totals["db_total"] = cur.fetchone()[0]
        cur.execute("SELECT source, COUNT(*) FROM articles GROUP BY source "
                    "ORDER BY 2 DESC")
        totals["db_per_source"] = dict(cur.fetchall())
    else:
        totals["db_total"] = None
        totals["db_per_source"] = None

    print(json.dumps(totals, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
