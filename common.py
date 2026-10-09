#!/usr/bin/env python3
"""
news-digest common.py

Gemeinsame Pfade, Env-Loader, Config und DB-Schema fuer alle Module.
Alle Pfade sind relativ zum Projektordner — die Pipeline laeuft ueberall,
wo dieser Ordner liegt.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "sources.yaml"
DB = Path(os.environ.get("NEWS_DIGEST_DB") or ROOT / "news.db")
PROMPTS = ROOT / "prompts" / "summarize.txt"
TEMPLATE_DIR = ROOT / "templates"
OUT_DIR = ROOT / "out"
LOG_DIR = ROOT / "logs"


# --------------------------------------------------------------------------- #
# Env / Config
# --------------------------------------------------------------------------- #

def env_files() -> list[Path]:
    """Kandidaten in Prioritaetsreihenfolge; frueher gefundene Werte gewinnen."""
    files = []
    if os.environ.get("NEWS_DIGEST_ENV"):
        files.append(Path(os.environ["NEWS_DIGEST_ENV"]))
    files += [
        Path.home() / "secrets" / "news-digest.env",   # Hetzner (ausserhalb Webroot)
        ROOT / ".env",                                 # lokal
        Path.home() / ".hermes" / ".env",              # mistral (alt)
    ]
    return files


def load_env() -> None:
    """Laedt KEY=VALUE-Dateien in os.environ; bereits gesetzte Variablen bleiben."""
    for f in env_files():
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


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
"""

# Spalten, die nach dem Initial-Schema dazukamen (Modul 2 + 3)
EXTRA_COLUMNS = [
    ("summary_de",   "TEXT"),
    ("topic",        "TEXT"),
    ("importance",   "INTEGER"),
    ("status",       "TEXT DEFAULT 'raw'"),
    ("processed_at", "TEXT"),
    ("title_de",     "TEXT"),
    ("sent_at",      "TEXT"),
]

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_articles_title_hash ON articles(title_hash);
CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles(published);
CREATE INDEX IF NOT EXISTS idx_articles_source     ON articles(source);
CREATE INDEX IF NOT EXISTS idx_articles_status     ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_topic      ON articles(topic);
CREATE INDEX IF NOT EXISTS idx_articles_importance ON articles(importance);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    """Idempotent: legt Tabelle, fehlende Spalten und Indizes an."""
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(articles)")}
    for name, decl in EXTRA_COLUMNS:
        if name in have:
            continue
        con.execute(f"ALTER TABLE articles ADD COLUMN {name} {decl}")
        if name == "sent_at":
            # Bestand gilt als bereits versendet — sonst ginge beim ersten Lauf
            # nach der Migration das komplette Archiv raus.
            con.execute("UPDATE articles SET sent_at = processed_at "
                        "WHERE status = 'processed'")
    con.executescript(INDEXES)
    con.commit()


def open_db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    return con
