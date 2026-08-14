#!/usr/bin/env python3
"""
News-Digest Modul 3 — HTML-Email-Generator + SMTP-Versand

Pipeline:
  digest.sqlite (processed articles, status=processed)
  → Jinja2 HTML-Template
  → HTML-File in out/YYYY-MM-DD.html
  → Vorschau-Dry-Run (--preview)
  → SMTP-Versand (--send)
"""
import argparse
import datetime as dt
import json
import os
import smtplib
import sqlite3
import sys
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    print("FEHLER: jinja2 nicht installiert.  sudo apt install -y python3-jinja2", file=sys.stderr)
    sys.exit(1)

# ------------------------------------------------------------------ paths / config
BASE = Path(__file__).parent
DB = BASE / "news.db"
TEMPLATE_DIR = BASE / "templates"
OUT_DIR = BASE / "out"
OUT_DIR.mkdir(exist_ok=True)

# load SMTP-Config from ~/.hermes/.env
ENV_FILE = Path.home() / ".hermes" / ".env"
def load_env():
    env = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

ENV = load_env()
SMTP_HOST = ENV.get("HETZNER_MAIL_HOST", "mail.your-server.de")
SMTP_PORT = int(ENV.get("HETZNER_MAIL_SMTP_PORT", "465"))
SMTP_USER = ENV.get("HETZNER_MAIL_USER", "mistral@ospn.de")
SMTP_PASS = ENV.get("HETZNER_MAIL_PASS", "")
MAIL_FROM = SMTP_USER  # Absender = SMTP-Login
MAIL_TO = [SMTP_USER]  # Default: an Absender selbst

# ------------------------------------------------------------------ topic meta
TOPIC_META = {
    "industry":     {"label": "Industry",  "emoji": "🏢", "color": "#2563eb"},
    "research":     {"label": "Research",  "emoji": "🔬", "color": "#16a34a"},
    "llm":          {"label": "LLM",       "emoji": "🧠", "color": "#ea580c"},
    "coding-agent": {"label": "Coding-Agent", "emoji": "💻", "color": "#9333ea"},
    "stt_tts":      {"label": "STT/TTS",   "emoji": "🎙️", "color": "#0891b2"},
}

# ------------------------------------------------------------------ DB
def fetch_articles(min_importance: int = 1, limit: int | None = None) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = """
        SELECT id, source, url, title, summary_de, published, topic, importance
        FROM articles
        WHERE status='processed' AND importance >= ?
        ORDER BY importance DESC, published DESC
    """
    params = [min_importance]
    if limit:
        q += " LIMIT ?"
        params.append(limit)
    rows = [dict(r) for r in con.execute(q, params)]
    con.close()
    return rows

# ------------------------------------------------------------------ build sections
def group_by_topic(articles: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        meta = TOPIC_META.get(a["topic"], {"label": a["topic"] or "Other", "emoji": "📰", "color": "#64748b"})
        a["_meta"] = meta
        buckets[a["topic"] or "other"].append(a)

    # Sort topics: by highest importance in bucket, then by count
    def topic_key(t):
        arts = buckets[t]
        max_imp = max(a["importance"] for a in arts)
        return (-max_imp, -len(arts))

    sections = []
    for topic in sorted(buckets.keys(), key=topic_key):
        arts = sorted(buckets[topic], key=lambda a: (-a["importance"], a["published"] or ""))
        meta = TOPIC_META.get(topic, {"label": topic, "emoji": "📰", "color": "#64748b"})
        sections.append({
            "topic": topic,
            "label": meta["label"],
            "emoji": meta["emoji"],
            "color": meta["color"],
            "count": len(arts),
            "articles": arts,
        })
    return sections

def fmt_published(iso: str) -> str:
    if not iso:
        return ""
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%d.%m.%Y")
    except Exception:
        return iso[:10]

def fmt_articles(sections: list[dict]) -> list[dict]:
    for s in sections:
        for a in s["articles"]:
            a["published"] = fmt_published(a["published"])
    return sections

# ------------------------------------------------------------------ render
def render_html(articles: list[dict], date: dt.date) -> tuple[str, str, str]:
    sections = fmt_articles(group_by_topic(articles))

    # topic stats (count per topic across ALL articles, even low importance)
    topic_counts = defaultdict(int)
    for a in articles:
        topic_counts[a["topic"] or "other"] += 1

    topic_stats = []
    for topic in ["industry", "research", "llm", "coding-agent", "stt_tts"]:
        meta = TOPIC_META[topic]
        topic_stats.append({
            "label": meta["label"],
            "color": meta["color"],
            "count": topic_counts.get(topic, 0),
        })

    total = len(articles)
    high = sum(1 for a in articles if a["importance"] >= 4)

    sources = sorted({a["source"] for a in articles})
    sources_count = len(sources)

    if high:
        preheader = f"{total} Artikel, davon {high} hochrelevant (★4-5)."
    else:
        preheader = f"{total} Artikel aus {sources_count} Quellen."

    subject = f"News-Digest {date.isoformat()} — {total} Artikel"
    if high:
        subject += f" · {high} Highlights"

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template("digest-email.html.j2")

    html = tpl.render(
        subject=subject,
        preheader=preheader,
        date_human=date.strftime("%A, %d. %B %Y"),
        date_iso=date.isoformat(),
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_articles=total,
        sources_count=sources_count,
        sources=[{"source": s} for s in sources],
        topic_stats=topic_stats,
        sections=sections,
    )
    return html, subject, preheader

# ------------------------------------------------------------------ preview
def preview(html: str, subject: str, out_path: Path) -> None:
    out_path.write_text(html, encoding="utf-8")
    print(f"✓ HTML geschrieben: {out_path}")
    print(f"  Subject: {subject}")
    print(f"  Größe:   {len(html):,} bytes")
    print()
    print("📋 VORSCHAU (erste 2000 Zeichen):")
    print("-" * 70)
    print(html[:2000])
    print("-" * 70)
    print()
    print(f"📂 Vollständige HTML-Datei: {out_path}")
    print()

# ------------------------------------------------------------------ send
def send(html: str, subject: str, recipients: list[str], dry: bool = False) -> bool:
    if not SMTP_PASS:
        print("FEHLER: HETZNER_MAIL_PASS nicht gesetzt.", file=sys.stderr)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="ospn.de")
    msg["MIME-Version"] = "1.0"
    # List-Unsubscribe ist Pflicht-Header für Newsletter (RFC 8058 / Gmail Bulk-Sender-Anforderungen)
    msg["List-Unsubscribe"] = f"<mailto:{MAIL_FROM}?subject=unsubscribe-news-digest>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["List-Id"] = f"<news-digest.ospn.de>"
    msg["X-Mailer"] = "news-digest/0.3.0 (Modul 3)"

    # Plain-Text-Fallback — substantive copy, nicht nur Stub
    # Wichtig: Gmail bewertet dieses Plain/HTML-Verhältnis
    plain_lines = [subject, "=" * len(subject), "", f"{len(recipients)} Empfänger · generiert am {dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    # Plain-Text-Variante der wichtigsten Artikel
    try:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT title, source, url, importance, topic, summary_de FROM articles "
            "WHERE status='processed' AND importance >= 4 ORDER BY importance DESC LIMIT 20"
        ):
            plain_lines.append(f"★ {r['importance']}/5 [{r['topic']}] {r['title']}")
            if r['summary_de']:
                plain_lines.append(f"   {r['summary_de'][:200]}")
            plain_lines.append(f"   Quelle: {r['source']}")
            plain_lines.append(f"   {r['url']}")
            plain_lines.append("")
        con.close()
    except Exception:
        pass
    plain_lines.append("---")
    plain_lines.append("News-Digest Pipeline · Modul 3 · unsubscribe: " + MAIL_FROM)
    plain_fallback = "\n".join(plain_lines)
    msg.attach(MIMEText(plain_fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if dry:
        print(f"🔍 DRY-RUN: würde senden an {recipients}")
        print(f"   Subject: {subject}")
        print(f"   From:    {MAIL_FROM}")
        print(f"   SMTP:    {SMTP_HOST}:{SMTP_PORT} (SSL)")
        return True

    print(f"📤 Verbinde zu {SMTP_HOST}:{SMTP_PORT} (SSL)...")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(MAIL_FROM, recipients, msg.as_string())
        print(f"✓ Versendet an {recipients}")
        return True
    except Exception as e:
        print(f"✗ SMTP-Fehler: {e}", file=sys.stderr)
        return False

# ------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser(description="News-Digest Modul 3 — HTML-Email + SMTP")
    p.add_argument("--min-importance", type=int, default=1, help="nur Artikel mit importance >= N")
    p.add_argument("--limit", type=int, default=None, help="max Anzahl Artikel")
    p.add_argument("--date", default=None, help="Datum für Subject/Output (default: heute)")
    p.add_argument("--preview", action="store_true", help="nur HTML-Datei schreiben + Vorschau, kein Versand")
    p.add_argument("--send", action="store_true", help="HTML-Datei schreiben + SMTP-Versand")
    p.add_argument("--to", action="append", default=None, help="Empfänger (mehrfach möglich)")
    p.add_argument("--smtp-check", action="store_true", help="nur SMTP-Login testen")
    args = p.parse_args()

    # SMTP-Verbindungstest
    if args.smtp_check:
        if not SMTP_PASS:
            print("FEHLER: HETZNER_MAIL_PASS nicht gesetzt.", file=sys.stderr)
            sys.exit(1)
        print(f"Teste SMTP-SSL zu {SMTP_HOST}:{SMTP_PORT}...")
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                print("✓ SMTP-Login OK")
        except Exception as e:
            print(f"✗ SMTP-Login fehlgeschlagen: {e}", file=sys.stderr)
            sys.exit(1)
        return

    date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    articles = fetch_articles(min_importance=args.min_importance, limit=args.limit)
    if not articles:
        print("Keine Artikel gefunden. Pipeline vorher mit summarize_parallel.py füllen.", file=sys.stderr)
        sys.exit(1)

    html, subject, preheader = render_html(articles, date)
    out_path = OUT_DIR / f"{date.isoformat()}.html"
    preview(html, subject, out_path)

    # Vorschau-Modus -> stop
    if args.preview and not args.send:
        print("✅ Vorschau-Modus. Versand erst mit --send.")
        return

    # Send-Modus
    recipients = args.to or MAIL_TO
    print(f"📨 Ziel-Empfänger: {recipients}")
    print(f"📋 Subject: {subject}")
    print()
    ok = send(html, subject, recipients, dry=not args.send)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
