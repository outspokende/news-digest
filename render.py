#!/usr/bin/env python3
"""
News-Digest Modul 3 — HTML-Email-Generator + Versand

Pipeline:
  news.db (status=processed, noch nicht versendet)
  → Jinja2 HTML-Template
  → HTML-File in out/YYYY-MM-DD.html
  → Vorschau (--preview) oder Versand (--send)

Versand:
  Brevo-Transaktions-API, eine Mail pro Empfänger (Zustellung/Öffnung pro
  Person im Brevo-Dashboard sichtbar). Empfänger kommen aus der Brevo-
  Kontaktliste `mail.brevo_list_id` (sources.yaml) oder aus --to.
  Fallback ohne BREVO_API_KEY: SMTP (GMAIL_SMTP_*), nur mit --to.
"""
import argparse
import datetime as dt
import os
import smtplib
import sqlite3
import sys
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import requests

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    print("FEHLER: jinja2 nicht installiert.  pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

from common import OUT_DIR, TEMPLATE_DIR, load_config, load_env, open_db

BREVO_API = "https://api.brevo.com/v3"

# ------------------------------------------------------------------ topic meta
TOPIC_META = {
    "industry":     {"label": "Branche",       "emoji": "🏢", "color": "#2563eb"},
    "research":     {"label": "Forschung",      "emoji": "🔬", "color": "#16a34a"},
    "llm":          {"label": "LLM",           "emoji": "🧠", "color": "#ea580c"},
    "coding-agent": {"label": "Code-Assistent", "emoji": "💻", "color": "#9333ea"},
    "stt_tts":      {"label": "Sprach-KI",     "emoji": "🎙️", "color": "#0891b2"},
    "audio":        {"label": "Audio",         "emoji": "🎵", "color": "#db2777"},
}

# ------------------------------------------------------------------ DB
def fetch_articles(con: sqlite3.Connection, min_importance: int = 1,
                   limit: int | None = None, since: str | None = None,
                   max_age_days: int = 7) -> list[dict]:
    """
    Default: alle verarbeiteten, noch nicht versendeten Artikel (sent_at IS NULL).
    Mit --since: alle seit diesem Zeitpunkt (UTC) verarbeiteten, auch bereits versendete.
    """
    q = """
        SELECT id, source, url, title, title_de, summary_de, published, topic, importance
        FROM articles
        WHERE status='processed' AND importance >= ?
    """
    params: list = [min_importance]
    if since:
        q += " AND processed_at >= ?"
        params.append(since)
    else:
        q += " AND sent_at IS NULL AND processed_at >= datetime('now', ?)"
        params.append(f"-{int(max_age_days)} days")
    q += " ORDER BY importance DESC, published DESC"
    if limit:
        q += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in con.execute(q, params)]


def mark_sent(con: sqlite3.Connection, ids: list[int]) -> None:
    con.executemany("UPDATE articles SET sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [(i,) for i in ids])
    con.commit()

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
def render_plain(sections: list[dict], subject: str, reply_to: str) -> str:
    """Plain-Text-Variante derselben Artikel wie im HTML-Teil."""
    lines = [subject, "=" * len(subject), ""]
    for sec in sections:
        lines += [f"## {sec['label']} ({sec['count']})", ""]
        for a in sec["articles"]:
            lines.append(f"★ {a['importance']}/5  {a['title_de'] or a['title']}")
            if a["summary_de"]:
                lines.append(f"   {a['summary_de']}")
            lines.append(f"   {a['source']} · {a['url']}")
            lines.append("")
    lines += ["---", f"Abmelden: kurze Antwort an {reply_to}"]
    return "\n".join(lines)


def render_html(articles: list[dict], date: dt.date, reply_to: str) -> tuple[str, str, str]:
    sections = fmt_articles(group_by_topic(articles))

    # topic stats (count per topic across ALL articles, even low importance)
    topic_counts = defaultdict(int)
    for a in articles:
        topic_counts[a["topic"] or "other"] += 1

    # audio u.ae. nur zeigen, wenn es heute Artikel gibt — sonst wird die Leiste zu breit
    topic_stats = [
        {"label": meta["label"], "color": meta["color"], "count": topic_counts.get(topic, 0)}
        for topic, meta in TOPIC_META.items()
        if topic_counts.get(topic) or topic != "audio"
    ]

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
        reply_to=reply_to,
    )
    return html, subject, render_plain(sections, subject, reply_to)

# ------------------------------------------------------------------ recipients
def brevo_headers(api_key: str) -> dict:
    return {"api-key": api_key, "accept": "application/json",
            "content-type": "application/json"}


def brevo_list_recipients(api_key: str, list_id: int) -> list[str]:
    """Alle nicht gesperrten Kontakte der Brevo-Liste."""
    out, offset, page = [], 0, 500
    while True:
        r = requests.get(f"{BREVO_API}/contacts/lists/{list_id}/contacts",
                         headers=brevo_headers(api_key),
                         params={"limit": page, "offset": offset}, timeout=30)
        r.raise_for_status()
        contacts = r.json().get("contacts", [])
        out += [c["email"] for c in contacts
                if c.get("email") and not c.get("emailBlacklisted")]
        if len(contacts) < page:
            return out
        offset += page


# ------------------------------------------------------------------ send
def send_brevo(api_key: str, mail: dict, recipients: list[str],
               subject: str, html: str, plain: str) -> list[str]:
    """Eine Transaktionsmail pro Empfänger. Returns: Liste der Fehlschläge."""
    failed = []
    for rcpt in recipients:
        payload = {
            "sender":      {"name": mail["sender_name"], "email": mail["sender_email"]},
            "to":          [{"email": rcpt}],
            "replyTo":     {"email": mail["reply_to"]},
            "subject":     subject,
            "htmlContent": html,
            "textContent": plain,
            "tags":        [mail["tag"]],
            "headers":     {"List-Unsubscribe": f"<mailto:{mail['reply_to']}?subject=unsubscribe-news-digest>"},
        }
        try:
            r = requests.post(f"{BREVO_API}/smtp/email", headers=brevo_headers(api_key),
                              json=payload, timeout=30)
            r.raise_for_status()
            print(f"✓ {rcpt}  ({r.json().get('messageId', '?')})")
        except requests.RequestException as e:
            detail = getattr(e.response, "text", "")[:200] if e.response is not None else ""
            print(f"✗ {rcpt}: {e} {detail}", file=sys.stderr)
            failed.append(rcpt)
    return failed


def send_smtp(mail: dict, recipients: list[str],
              subject: str, html: str, plain: str) -> list[str]:
    """Fallback ohne Brevo: eine Mail an alle über GMAIL_SMTP_*."""
    host = os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("GMAIL_SMTP_PORT", "465"))
    user = os.environ.get("GMAIL_SMTP_USER", "")
    password = os.environ.get("GMAIL_SMTP_PASS", "")
    if not user or not password:
        print("FEHLER: weder BREVO_API_KEY noch GMAIL_SMTP_USER/GMAIL_SMTP_PASS gesetzt.",
              file=sys.stderr)
        return recipients

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user  # Absender = SMTP-Login
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["List-Unsubscribe"] = f"<mailto:{mail['reply_to']}?subject=unsubscribe-news-digest>"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.sendmail(user, recipients, msg.as_string())
        print(f"✓ Versendet per SMTP an {recipients}")
        return []
    except Exception as e:
        print(f"✗ SMTP-Fehler: {e}", file=sys.stderr)
        return recipients


# ------------------------------------------------------------------ main
def main() -> int:
    p = argparse.ArgumentParser(description="News-Digest Modul 3 — HTML-Email + Versand")
    p.add_argument("--min-importance", type=int, default=None,
                   help="nur Artikel mit importance >= N (default: mail.min_importance)")
    p.add_argument("--limit", type=int, default=None, help="max Anzahl Artikel")
    p.add_argument("--date", default=None, help="Datum für Subject/Output (default: heute)")
    p.add_argument("--since", default=None,
                   help="alle seit 'YYYY-MM-DD HH:MM:SS' (UTC) verarbeiteten Artikel, "
                        "auch bereits versendete (default: nur unversendete)")
    p.add_argument("--preview", action="store_true", help="nur HTML-Datei schreiben, kein Versand")
    p.add_argument("--send", action="store_true", help="HTML-Datei schreiben + versenden")
    p.add_argument("--to", action="append", default=None,
                   help="Empfänger statt Brevo-Liste (mehrfach möglich) — für Tests; "
                        "markiert Artikel NICHT als versendet")
    p.add_argument("--check", action="store_true",
                   help="Brevo-Zugang und Empfängerliste prüfen, nichts versenden")
    args = p.parse_args()

    load_env()
    mail = load_config().get("mail", {})
    api_key = os.environ.get("BREVO_API_KEY", "")
    list_id = mail.get("brevo_list_id")

    def recipients() -> list[str]:
        if args.to:
            return args.to
        if not api_key or not list_id:
            print("FEHLER: keine Empfänger — BREVO_API_KEY + mail.brevo_list_id setzen "
                  "oder --to angeben.", file=sys.stderr)
            return []
        return brevo_list_recipients(api_key, int(list_id))

    if args.check:
        rcpts = recipients()
        print(f"Absender:  {mail.get('sender_name')} <{mail.get('sender_email')}>")
        print(f"Versand:   {'Brevo-API' if api_key else 'SMTP-Fallback'}")
        print(f"Empfänger: {len(rcpts)}")
        for r in rcpts:
            print(f"  {r}")
        return 0 if rcpts else 1

    date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    min_imp = args.min_importance if args.min_importance is not None else int(mail.get("min_importance", 1))
    con = open_db()
    articles = fetch_articles(con, min_importance=min_imp, limit=args.limit, since=args.since,
                              max_age_days=int(mail.get("max_age_days", 7)))
    if not articles:
        print("Keine neuen Artikel — nichts zu versenden.")
        return 0

    html, subject, plain = render_html(articles, date, mail.get("reply_to", ""))
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{date.isoformat()}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"✓ HTML geschrieben: {out_path}  ({len(html):,} bytes, {len(articles)} Artikel)")
    print(f"  Subject: {subject}")

    if not args.send:
        print("Vorschau-Modus. Versand erst mit --send.")
        return 0

    rcpts = recipients()
    if not rcpts:
        return 1
    print(f"📨 Versand an {len(rcpts)} Empfänger ...")
    if api_key:
        failed = send_brevo(api_key, mail, rcpts, subject, html, plain)
    else:
        failed = send_smtp(mail, rcpts, subject, html, plain)

    # Als versendet markieren, sobald mindestens eine Mail raus ist — sonst bekämen
    # die erfolgreichen Empfänger morgen alles doppelt. Testläufe (--to/--since) zählen nicht.
    if len(failed) < len(rcpts) and not args.to and not args.since:
        mark_sent(con, [a["id"] for a in articles])
    print(f"Versendet: {len(articles)} Artikel an {len(rcpts) - len(failed)}/{len(rcpts)} Empfänger")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
