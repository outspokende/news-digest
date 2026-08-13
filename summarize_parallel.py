#!/usr/bin/env python3
"""
news-digest summarize_parallel.py

Parallele Variante von summarize.py mit ThreadPoolExecutor.
Faedig mit max_workers parallelen MiniMax-Calls.

CLI: identisch zu summarize.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

ROOT = Path.home() / ".hermes" / "news-digest"
DB = ROOT / "news.db"
PROMPTS = ROOT / "prompts" / "summarize.txt"
API_URL = (os.environ.get("MINIMAX_BASE_URL") or "https://api.minimax.io/v1") + "/chat/completions"
DEFAULT_MODEL = os.environ.get("MINIMAX_MODEL") or "MiniMax-M2.7-highspeed"

MAX_WORKERS = 5
MAX_RETRIES = 3


def load_prompts():
    raw = yaml.safe_load(PROMPTS.read_text(encoding="utf-8"))
    return raw["system"].strip(), raw["user_template"].strip()


def render_user(tpl: str, art: dict) -> str:
    return (tpl
            .replace("{title}",     art.get("title") or "")
            .replace("{source}",    art.get("source") or "")
            .replace("{published}", art.get("published") or "unbekannt")
            .replace("{summary}",   art.get("summary") or "(keine Beschreibung)"))


def call_MiniMax(api_key: str, system: str, user: str, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": 0.2,
        "max_tokens":  800,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    last_err = None
    for attempt in range(MAX_RETRIES):
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
    try:
        content = resp["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        return None
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not all(k in d for k in ("summary_de", "topic", "importance")):
        return None
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


def process_one(api_key: str, system: str, user_tpl: str, model: str,
                art: dict) -> tuple[int, dict | None, str | None]:
    user_prompt = render_user(user_tpl, art)
    try:
        resp = call_MiniMax(api_key, system, user_prompt, model)
        parsed = parse_response(resp)
        if not parsed:
            return art["id"], None, "parse_failed"
        return art["id"], parsed, None
    except Exception as e:
        return art["id"], None, str(e)[:120]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = ap.parse_args()

    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key and not args.dry_run:
        print("fehler: MINIMAX_API_KEY nicht in env", file=sys.stderr)
        return 2

    system, user_tpl = load_prompts()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    q = "SELECT * FROM articles WHERE status IS NULL OR status='raw' ORDER BY published DESC"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = [dict(r) for r in con.execute(q).fetchall()]

    if not rows:
        print(json.dumps({"processed": 0, "errors": 0, "msg": "nichts zu tun"}))
        con.close()
        return 0

    print(f"verarbeite {len(rows)} artikel mit {args.workers} workern ...", file=sys.stderr)

    if args.dry_run:
        print("--- DRY RUN: prompts ok, ueberspringe API-Calls ---")
        print(json.dumps({"processed": 0, "errors": 0, "would_process": len(rows)}))
        return 0

    processed = errors = 0
    error_samples = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_one, api_key, system, user_tpl, args.model, art): art
            for art in rows
        }
        for i, fut in enumerate(as_completed(futures), 1):
            art_id, parsed, err = fut.result()
            if parsed:
                con.execute("""
                    UPDATE articles SET summary_de=?, topic=?, importance=?,
                                        status='processed', processed_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (parsed["summary_de"], parsed["topic"], parsed["importance"], art_id))
                processed += 1
            else:
                errors += 1
                if len(error_samples) < 3:
                    error_samples.append({"id": art_id, "error": err})
            if i % 10 == 0 or i == len(rows):
                con.commit()
                print(f"  progress: {i}/{len(rows)}  ok={processed} err={errors}",
                      file=sys.stderr)

    con.commit()
    cur = con.execute("""
        SELECT topic, COUNT(*) FROM articles
        WHERE status='processed' GROUP BY topic ORDER BY 2 DESC
    """)
    topic_stats = dict(cur.fetchall())
    cur = con.execute("SELECT COUNT(*) FROM articles WHERE status='processed' AND importance>=4")
    important = cur.fetchone()[0]
    con.close()

    out = {
        "processed": processed, "errors": errors,
        "topic_stats": topic_stats, "important_count": important,
        "error_samples": error_samples,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
