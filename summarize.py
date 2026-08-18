#!/usr/bin/env python3
"""
news-digest summarize.py

Liest Artikel mit status='raw' aus news.db, ruft MiniMax M2.7 auf,
schreibt summary_de / topic / importance in die DB.

CLI:
    python3 summarize.py                  # alle raw-Artikel, batch=10
    python3 summarize.py --limit 5        # nur 5 (Smoke-Test)
    python3 summarize.py --dry-run        # nichts schreiben, nur prompt preview
    python3 summarize.py --model NAME     # anderes Modell

Vorbedingung: env-var MINIMAX_API_KEY (sk-cp-...)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path.home() / ".hermes" / "news-digest"
DB = ROOT / "news.db"
PROMPTS = ROOT / "prompts" / "summarize.txt"

API_URL = "https://api.minimax.io/v1/text/chatcompletion_v2"
DEFAULT_MODEL = "MiniMax-M2.7"
# MiniMax Free-Tier (minimax.io) — VF AWS endpoint (ai.micro.aws.de.vodafone.com) deprecated Aug 2026 (401 Unauthorized)


# --------------------------------------------------------------------------- #
# Schema (additions)
# --------------------------------------------------------------------------- #

MIGRATIONS = """
ALTER TABLE articles ADD COLUMN summary_de TEXT;
ALTER TABLE articles ADD COLUMN topic       TEXT;
ALTER TABLE articles ADD COLUMN importance  INTEGER;
ALTER TABLE articles ADD COLUMN status      TEXT DEFAULT 'raw';
ALTER TABLE articles ADD COLUMN processed_at TEXT;
CREATE INDEX IF NOT EXISTS idx_articles_status     ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_topic      ON articles(topic);
CREATE INDEX IF NOT EXISTS idx_articles_importance ON articles(importance);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    # idempotent: jede Spalte einzeln versuchen, Duplicate-Error schlucken
    stmts = [
        "ALTER TABLE articles ADD COLUMN summary_de TEXT",
        "ALTER TABLE articles ADD COLUMN topic       TEXT",
        "ALTER TABLE articles ADD COLUMN importance  INTEGER",
        "ALTER TABLE articles ADD COLUMN status      TEXT DEFAULT 'raw'",
        "ALTER TABLE articles ADD COLUMN processed_at TEXT",
    ]
    for s in stmts:
        try:
            con.execute(s)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
    con.executescript("""
        CREATE INDEX IF NOT EXISTS idx_articles_status     ON articles(status);
        CREATE INDEX IF NOT EXISTS idx_articles_topic      ON articles(topic);
        CREATE INDEX IF NOT EXISTS idx_articles_importance ON articles(importance);
    """)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

def load_prompts() -> tuple[str, str]:
    raw = PROMPTS.read_text(encoding="utf-8")
    parts = yaml.safe_load(raw)
    return parts["system"].strip(), parts["user_template"].strip()


def render_user(template: str, art: dict) -> str:
    return (template
            .replace("{title}",    art.get("title") or "")
            .replace("{source}",   art.get("source") or "")
            .replace("{published}", art.get("published") or "unbekannt")
            .replace("{summary}",  art.get("summary") or "(keine Beschreibung)"))


# --------------------------------------------------------------------------- #
# MiniMax call
# --------------------------------------------------------------------------- #

def call_MiniMax(api_key: str, system: str, user: str, model: str,
                 max_retries: int = 3) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": 0.2,
        "max_tokens":  800,
    }
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"MiniMax call failed: {last_err}")


def parse_response(resp: dict) -> dict | None:
    """Extract {summary_de, topic, importance} from MiniMax response."""
    try:
        content = resp["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        return None
    # MiniMax haengt gelegentlich ```json fences an
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not all(k in d for k in ("summary_de", "topic", "importance")):
        return None
    # Validation
    if not isinstance(d["summary_de"], str) or len(d["summary_de"]) < 5:
        return None
    if d["topic"] not in {"coding-agent", "llm", "stt_tts",
                          "audio", "research", "industry"}:
        d["topic"] = "industry"
    try:
        d["importance"] = max(1, min(5, int(d["importance"])))
    except (ValueError, TypeError):
        d["importance"] = 2
    return d


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key and not args.dry_run:
        print("fehler: MINIMAX_API_KEY nicht in env "
              "(source ~/.hermes/.env oder export setzen)", file=sys.stderr)
        return 2

    system, user_tpl = load_prompts()

    con = sqlite3.connect(DB)
    ensure_schema(con)
    con.row_factory = sqlite3.Row

    q = "SELECT * FROM articles WHERE status IS NULL OR status='raw' ORDER BY published DESC"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = con.execute(q).fetchall()

    if not rows:
        print(json.dumps({"processed": 0, "errors": 0, "msg": "nichts zu tun"}))
        con.close()
        return 0

    print(f"verarbeite {len(rows)} artikel ...", file=sys.stderr)
    processed = errors = 0
    examples = []

    for row in rows:
        art = dict(row)
        user_prompt = render_user(user_tpl, art)

        if args.dry_run:
            print("--- DRY RUN Beispiel ---")
            print("USER:", user_prompt[:400])
            examples.append({"id": art["id"], "title": art["title"][:60]})
            processed += 1
            continue

        try:
            resp = call_MiniMax(api_key, system, user_prompt, args.model)
            parsed = parse_response(resp)
            if not parsed:
                errors += 1
                continue
            con.execute("""
                UPDATE articles SET summary_de=?, topic=?, importance=?,
                                    status='processed', processed_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (parsed["summary_de"], parsed["topic"], parsed["importance"],
                  art["id"]))
            con.commit()
            processed += 1
        except Exception as e:
            errors += 1
            print(f"  error id={art['id']}: {e}", file=sys.stderr)

    # Topic-Statistik
    cur = con.execute("""
        SELECT topic, COUNT(*) FROM articles
        WHERE status='processed' GROUP BY topic ORDER BY 2 DESC
    """)
    topic_stats = dict(cur.fetchall())
    cur = con.execute("""
        SELECT COUNT(*) FROM articles
        WHERE status='processed' AND importance >= 4
    """)
    important = cur.fetchone()[0]

    con.close()

    out = {
        "processed": processed, "errors": errors,
        "topic_stats": topic_stats, "important_count": important,
    }
    if args.dry_run:
        out["dry_run_examples"] = examples
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
