#!/usr/bin/env python3
"""
news-digest summarize_parallel.py

Modul 2: liest Artikel mit status='raw', markiert thematische Duplikate,
ruft MiniMax parallel auf (ThreadPoolExecutor) und schreibt
summary_de / title_de / topic / importance in die DB.

CLI:
    python3 summarize_parallel.py              # alle raw-Artikel
    python3 summarize_parallel.py --limit 5    # Smoke-Test
    python3 summarize_parallel.py --dry-run    # keine API-Calls

Vorbedingung: MINIMAX_API_KEY in der Env-Datei (siehe common.env_files).
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
from collections import defaultdict

import requests
import yaml

from common import DB, PROMPTS, load_env, open_db

# --------------------------------------------------------------------------- #
# Similarity Dedup — Cross-Source Duplikat-Erkennung
# --------------------------------------------------------------------------- #

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
    "be", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must", "shall", "can", "need",
    "it", "its", "this", "that", "these", "those", "i", "we", "you",
    "he", "she", "they", "what", "which", "who", "whom", "how", "when",
    "where", "why", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "also", "now", "here",
    "there", "then", "once", "if", "any", "new", "up", "out", "about",
    "after", "before", "over", "under", "again", "further", "once",
}

# Extra Stopwords für Tech/AI-News — zu generisch für Dedup
TECH_STOPWORDS = {
    "ai", "llm", "gpt", "new", "news", "update", "launch", "introduces",
    "announces", "says", "report", "study", "research", "data", "2024",
    "2025", "2026", "million", "billion", "company", "tech",
    "reveals", "shows", "finds", "raises", "secures", "gets", "makes",
    # cs.CL / ML generische Begriffe — überall, daher nutzlos für Dedup
    "language", "model", "models", "modeling", "large", "small",
    "learning", "system", "systems", "method", "methods", "approach",
    "based", "using", "using", "towards", "toward", "efficient",
    "multi", "task", "tasks", "across", "general", "alignment",
    "performance", "results", "analysis", "framework", "framework",
    "training", "inference", "benchmarks", "benchmark",
    "class", "classes", "classification", "review", "screening",
}
STOPWORDS |= TECH_STOPWORDS


def extract_significant_words(title: str) -> set[str]:
    """Extrahiere signifikante Wörter aus einem Titel (lowercased, ohne Stopwords)."""
    words = re.findall(r"[a-zA-Z]+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def topic_key(title: str) -> frozenset:
    """
    Extrahiere das 'Core Topic' eines Artikels als frozenset.
    Nur Titel-Wörter — summaries sind zu noisy (HTML, Source-Namen, etc.).
    """
    return frozenset(extract_significant_words(title))


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union > 0 else 0.0


def find_duplicate_clusters(articles: list[dict], min_jaccard: float = 0.35,
                            min_common: int = 3) -> list[list[dict]]:
    """
    Finde thematische Duplikat-Cluster in einer Artikel-Liste.
    Zwei Artikel sind ein Cluster wenn:
      (a) sie >= min_common signifikante Titel-Schlüsselwörter teilen, ODER
      (b) ihre Titel-Keywords Jaccard >= min_jaccard haben.
    Transitiv: wenn A~B und B~C, dann A~B~C (union-find pattern).
    
    Returns: Liste von Clustern, jedes Cluster = Liste der Artikel-Dicts.
    """
    if len(articles) < 2:
        return [articles] if articles else []

    # Berechne Topic-Keys für alle Artikel (nur Titel)
    keys = []
    for art in articles:
        keys.append(topic_key(art["title"]))

    # Union-Find für transitive Cluster
    parent = list(range(len(articles)))

    def find(x):
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Prüfe paarweise similarity
    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            common = keys[i] & keys[j]
            jac = jaccard(keys[i], keys[j])
            if len(common) >= min_common or jac >= min_jaccard:
                union(i, j)

    # Baue Cluster
    clusters: dict[int, list[dict]] = defaultdict(list)
    for i in range(len(articles)):
        clusters[find(i)].append(articles[i])

    return list(clusters.values())


def deduplicate_similar(con: sqlite3.Connection) -> dict:
    """
    Prüft alle 'raw' Artikel auf Cross-Source-Duplikate.
    Artikel mit gleichem Core Topic werden zu Clustern zusammengefasst —
    der zuerst gesammelte (kleinste id) bleibt 'raw', die anderen werden
    auf status='duplicate' gesetzt. importance gibt es hier noch nicht:
    der Dedup laeuft bewusst VOR den LLM-Calls.
    
    Returns: Statistik-Dict.
    """
    cur = con.cursor()
    cur.execute("""
        SELECT id, source, title, summary, url, published, importance, topic, status
        FROM articles
        WHERE status IS NULL OR status = 'raw'
        ORDER BY id
    """)
    raw_articles = [dict(r) for r in cur.fetchall()]

    if not raw_articles:
        return {"clusters_found": 0, "duplicates_marked": 0}

    clusters = find_duplicate_clusters(raw_articles)

    dupes_marked = 0
    for cluster in clusters:
        if len(cluster) < 2:
            continue  # kein Duplikat

        cluster_sorted = sorted(cluster, key=lambda a: a["id"])
        best = cluster_sorted[0]
        for dupe in cluster_sorted[1:]:
            cur.execute(
                "UPDATE articles SET status='duplicate', processed_at=CURRENT_TIMESTAMP WHERE id=?",
                (dupe["id"],)
            )
            dupes_marked += 1
            print(f"  DUPE: '{best['title'][:60]}' <- '{dupe['title'][:60]}'",
                  file=sys.stderr)

    con.commit()
    return {
        "clusters_found": sum(1 for c in clusters if len(c) > 1),
        "duplicates_marked": dupes_marked,
    }

API_URL = "https://api.minimax.io/v1/text/chatcompletion_v2"
DEFAULT_MODEL = "MiniMax-M2.7"
# MiniMax Free-Tier (minimax.io) — VF AWS endpoint deprecated Aug 2026

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
        choices = resp.get("choices")
        if not choices or choices[0] is None:
            return None
        msg = choices[0].get("message")
        if msg is None:
            return None
        content = msg.get("content")
        if content is None:
            return None
        content = content.strip()
    except (KeyError, IndexError, TypeError):
        return None
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not all(k in d for k in ("summary_de", "title_de", "topic", "importance")):
        return None
    if not isinstance(d["summary_de"], str) or len(d["summary_de"]) < 5:
        return None
    if not isinstance(d["title_de"], str) or len(d["title_de"]) < 3:
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

    load_env()
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key and not args.dry_run:
        print("fehler: MINIMAX_API_KEY nicht in env", file=sys.stderr)
        return 2

    system, user_tpl = load_prompts()
    con = open_db()

    q = "SELECT * FROM articles WHERE status IS NULL OR status='raw' ORDER BY published DESC"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = [dict(r) for r in con.execute(q).fetchall()]

    if not rows:
        print(json.dumps({"processed": 0, "errors": 0, "msg": "nichts zu tun"}))
        con.close()
        return 0

    # --- Cross-Source Similarity Dedup VOR den LLM-Calls ---
    print("prüfe auf thematische Duplikate ...", file=sys.stderr)
    dedup_stats = deduplicate_similar(con)
    print(f"  {dedup_stats['clusters_found']} Cluster gefunden, "
          f"{dedup_stats['duplicates_marked']} als duplicate markiert", file=sys.stderr)

    # Re-fetch rows after dedup (some may have been marked duplicate)
    rows = [dict(r) for r in con.execute(q).fetchall()]
    if not rows:
        print(json.dumps({"processed": 0, "errors": 0, "msg": "nichts mehr zu tun nach Dedup"}))
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
                    UPDATE articles SET summary_de=?, title_de=?, topic=?, importance=?,
                                        status='processed', processed_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (parsed["summary_de"], parsed["title_de"], parsed["topic"], parsed["importance"], art_id))
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
        "dedup": dedup_stats,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
