# KI News Digest — agents.md

Dokumentation des KI-News-Digest-Jobs, wie er auf **mistral** im Hermes-Agent läuft.
Stand der Analyse: **2026-10-09** (nur lesend, auf mistral wurde nichts verändert).

> **Historisch.** Seit 2026-10-09 läuft der Digest auf dem Hetzner-Server und versendet
> über Brevo (siehe [README.md](README.md)). Die drei Hermes-Jobs `news-digest-*` auf mistral
> wurden am 2026-10-09 gelöscht (Sicherung: `~/.hermes/cron/jobs.json.bak-before-news-digest-removal-20261009`);
> Code und `news.db` unter `~/.hermes/news-digest/` liegen dort noch unverändert auf dem Stand vom 08.10.
> Dieses Dokument beschreibt, wie der Job bis dahin auf mistral lief.

## Kurzfassung

Täglich um 17:00 sammelt ein Python-Skript RSS-Feeds zu KI-Themen in eine SQLite-DB,
um 17:30 fasst MiniMax M2.7 jeden neuen Artikel auf Deutsch zusammen (Titel, 2 Sätze,
Topic, Wichtigkeit 1–5), um 17:45 wird daraus eine HTML-Mail gerendert und per
Gmail-SMTP an zwei Empfänger geschickt.

Die drei Schritte sind **drei getrennte Hermes-Cron-Jobs**. Jeder Job ist ein
LLM-Agent-Lauf, der nur ein Shell-Kommando ausführt und das Ergebnis berichtet —
die eigentliche Logik steckt vollständig in den Python-Skripten.

> **Nachtabschaltung:** mistral wird nachts ausgeschaltet. Dafür werden alle
> Hermes-Cron-Jobs abends pausiert (zuletzt 2026-10-08 21:00) und morgens wieder
> gestartet. Ein `paused`-Status am frühen Morgen ist also normal, kein Ausfall.

## Zugang

| | |
|---|---|
| Host | `mistral` / `mistral.local` / `192.168.178.105` (Ubuntu, Kernel 6.8) |
| Login | `ssh henning@192.168.178.105` (Key `~/.ssh/id_ed25519`, kein Eintrag in `~/.ssh/config`) |
| Projektordner | `/home/henning/.hermes/news-digest/` |
| Job-Definitionen | `/home/henning/.hermes/cron/jobs.json` |
| Lauf-Protokolle | `/home/henning/.hermes/cron/output/<job-id>/YYYY-MM-DD_HH-MM-SS.md` |
| Secrets | `/home/henning/.hermes/.env` (`MINIMAX_API_KEY`, `GMAIL_SMTP_*`) |
| Scheduler | `hermes-gateway.service` (systemd `--user`, aktiv) — kein System-Crontab, kein systemd-Timer |

## Ablauf

```
17:00  news-digest-collect    collect.py             RSS  -> news.db (status=raw)
17:30  news-digest-summarize  summarize_parallel.py  raw  -> Dedup -> MiniMax -> status=processed
17:45  news-digest-render     render.py --send       processed (heute) -> out/DATUM.html -> Gmail-SMTP
```

Zeiten sind Ortszeit (Europe/Berlin). Bis Mitte September liefen die Jobs morgens
(07:00 / 07:30 / 07:45), seitdem abends.

## Die drei Hermes-Cron-Jobs

Alle drei: `deliver: email:henning.jaeger@gmail.com`, Ursprung Telegram-Chat „Outspoken",
kein eigenes Modell gesetzt → Hermes-Default **MiniMax-M2.7** (Provider `minimax`).
Der Abschlussbericht des Agenten geht zusätzlich als eigene Mail an Henning.

| Job | ID | Cron | Läufe | Letzter Lauf |
|---|---|---|---|---|
| `news-digest-collect` | `7f6b4a39d14d` | `0 17 * * *` | 57 | 2026-10-08 17:01, ok |
| `news-digest-summarize` | `b6db5ffc4958` | `30 17 * * *` | 58 | 2026-10-08 17:32, ok |
| `news-digest-render` | `a53004d53cf3` | `45 17 * * *` | 54 | 2026-10-08 17:45, ok |

**collect** — führt `python3 /home/henning/.hermes/news-digest/collect.py` aus und
berichtet auf Deutsch: neue Artikel, Top-5-Quellen, fehlgeschlagene Feeds, DB-Stand.
Ausdrücklich: nur berichten, nicht selbst lesen oder filtern.

**summarize** — holt `MINIMAX_API_KEY` per `grep` aus `~/.hermes/.env`, startet
`summarize_parallel.py`, berichtet verarbeitete Artikel und Fehler.

**render** — führt aus:

```bash
cd /home/henning/.hermes/news-digest && python3 render.py --send \
  --to henning.jaeger@gmail.com --to marcel.szabo@googlemail.com \
  --since "$(date +%Y-%m-%d) 00:00:00" 2>&1
```

und meldet „Versendet: N Artikel an …". `--since` sorgt dafür, dass nur heute
verarbeitete Artikel in die Mail kommen.

Es gibt **keine technische Verkettung** der Jobs — nur den Zeitabstand. Dauert das
Zusammenfassen länger als 15 Minuten, verschickt render einen unvollständigen Digest.

## Dateien in `~/.hermes/news-digest/`

| Datei | Zweck |
|---|---|
| `collect.py` | Modul 1: Feeds holen, deduplizieren, in DB schreiben |
| `sources.yaml` | Feed-Liste + Einstellungen |
| `summarize_parallel.py` | Modul 2 (aktiv): Ähnlichkeits-Dedup + parallele MiniMax-Calls |
| `summarize.py` | Modul 2 (alt, sequenziell) — nicht mehr im Cron, enthält aber die Schema-Migration |
| `prompts/summarize.txt` | System-Prompt + User-Template (YAML trotz `.txt`) |
| `render.py` | Modul 3: HTML rendern, Datei schreiben, SMTP-Versand |
| `templates/digest-email.html.j2` | Jinja2-Mail-Template (640px, Inline-CSS) |
| `news.db` | SQLite, 5,3 MB, die eigentliche Datenbank |
| `digest.db` | leer (0 Byte), ungenutzt |
| `out/YYYY-MM-DD.html` | Archiv der versendeten Mails (52 Dateien seit 14.08.) |
| `test_api.py` | Minimaltest gegen die MiniMax-API |

Abhängigkeiten (System-Python 3.12.3, kein venv): `feedparser` 6.0.10, `pyyaml` 6.0.1,
`requests` 2.31.0, `jinja2` 3.1.2.

### Modul 1 — `collect.py`

- Liest `sources.yaml`, holt jeden Feed mit `feedparser` (Socket-Timeout 15 s,
  eigener User-Agent), nimmt maximal **20 Einträge pro Feed**.
- Dedup: Eintrag wird übersprungen, wenn **URL** oder **Titel-Hash**
  (sha256 des kleingeschriebenen, whitespace-normalisierten Titels, 16 Zeichen) schon existiert.
- Frische-Filter: älter als **3 Tage** → verworfen; ohne Datum → behalten.
- Schreibt `source, url, title, author, summary, published, topics, title_hash, fetched_at`.
- Ausgabe: JSON mit `new / dup / rejected / errors`, Detail pro Feed, `db_total`, `db_per_source`.
- CLI: `--source NAME`, `--dry-run`.

### Modul 2 — `summarize_parallel.py`

1. Lädt alle Artikel mit `status IS NULL OR status='raw'`.
2. **Cross-Source-Dedup vor den LLM-Calls:** Titel → signifikante Wörter (≥3 Buchstaben,
   ohne allgemeine und KI-typische Stoppwörter). Zwei Artikel gelten als gleich bei
   **≥3 gemeinsamen Wörtern oder Jaccard ≥ 0,35**; Cluster transitiv per Union-Find.
   Pro Cluster bleibt der Artikel mit der kleinsten ID, der Rest bekommt `status='duplicate'`.
3. Pro Artikel ein Call an `https://api.minimax.io/v1/text/chatcompletion_v2`,
   Modell `MiniMax-M2.7`, `temperature 0.2`, `max_tokens 800`, **5 Worker** parallel,
   3 Versuche mit Backoff bei 429/5xx.
4. Antwort muss JSON sein: `{"summary_de", "title_de", "topic", "importance"}`.
   Unbekanntes Topic → `industry`; Importance wird auf 1–5 geklemmt.
   Bei Parse-Fehler bleibt der Artikel `raw` und wird am nächsten Tag erneut versucht.
5. Erfolg → `status='processed'`, `processed_at=CURRENT_TIMESTAMP` (UTC).
- CLI: `--limit`, `--dry-run`, `--model`, `--workers`.

Der Prompt bekommt nur **Titel, Quelle, Datum und die Feed-Beschreibung** — der
Artikel selbst wird nie abgerufen. Topics: `coding-agent | llm | stt_tts | audio |
research | industry`. Importance: 5 = breaking, 1 = Rauschen; bei Unklarheit
`industry` / 2.

### Modul 3 — `render.py`

- Holt `status='processed' AND importance >= N` (Default 1), optional `processed_at >= --since`.
- Gruppiert nach Topic; Sektionen sortiert nach höchster Importance, dann Anzahl;
  innerhalb nach Importance absteigend.
- Betreff: `News-Digest YYYY-MM-DD — N Artikel · M Highlights` (Highlights = ★4–5).
- Mail zeigt: Kopf, Zähler pro Topic, je Artikel ★-Wert, Quelle, Datum, deutscher
  Titel (Fallback Originaltitel) als Link, deutsche Zusammenfassung, Fußzeile mit Quellen.
- Schreibt immer `out/DATUM.html`, auch beim Versand.
- Versand: `smtplib.SMTP_SSL` an `smtp.gmail.com:465`, Login und Absender
  `GMAIL_SMTP_USER`, multipart (Plaintext + HTML), mit `List-Unsubscribe`- und `List-Id`-Headern.
- CLI: `--preview`, `--send`, `--to` (mehrfach), `--since`, `--date`, `--min-importance`,
  `--limit`, `--smtp-check`.

## Quellen (`sources.yaml`)

17 aktive Feeds (der Kommentar in der Datei spricht noch von 22):

| Quelle | Artikel in DB | Anmerkung |
|---|---|---|
| arXiv cs.CL | 700 | täglich am 20er-Cap |
| TechCrunch AI | 523 | |
| The Decoder | 406 | |
| Hacker News (AI-tagged, ≥100 Punkte) | 201 | **liefert seit 03.10. nichts mehr**, siehe unten |
| Simon Willison's Weblog | 160 | |
| OpenAI Blog | 121 | |
| Lobsters (AI tag) | 60 | |
| Hugging Face Blog | 48 | |
| Ben's Bites Daily | 23 | |
| Google DeepMind Blog | 18 | |
| Mistral AI Blog | 9 | |
| Import AI | 7 | |
| GitHub Blog (Engineering) | 6 | |
| Sebastian Raschka | 4 | |
| Aider Blog, Lilian Weng, The Gradient | 0 | konfiguriert, aber noch nie ein Artikel in der DB |

Auskommentiert, weil kein Feed mehr / blockiert: Cursor, Anthropic, Meta AI,
ElevenLabs, Replicate, Reddit (r/MachineLearning, r/LocalLLaMA, r/ClaudeAI).

## Datenbank `news.db`

Eine Tabelle `articles`: Rohfelder aus Modul 1 plus `summary_de, title_de, topic,
importance, status, processed_at`.

Stand 2026-10-08: **2.306 Artikel** — 2.226 `processed`, 71 `duplicate`, 9 `dropped`
(manuell aussortierte Nicht-KI-Treffer vom ersten Tag; kein Skript setzt diesen Status).
Kein Rückstau: 0 Artikel in `raw`.

| Topic | Anzahl | | Importance | Anzahl |
|---|---|---|---|---|
| industry | 1.045 | | 5 | 55 |
| research | 702 | | 4 | 505 |
| llm | 229 | | 3 | 1.147 |
| coding-agent | 187 | | 2 | 472 |
| stt_tts | 48 | | 1 | 47 |
| audio | 15 | | | |

Volumen der letzten 10 Tage: 11–76 neue Artikel pro Tag, zuletzt 50 (08.10.).

## Auffälligkeiten und bekannte Schwächen

**Betrieb**

1. **Nachtabschaltung:** Jobs sind abends/nachts planmäßig pausiert (siehe oben). Folge: Der Digest hängt daran, dass mistral um 17:00 läuft und die Jobs morgens wieder gestartet wurden.
2. **Hacker-News-Feed defekt:** seit 03.10. fast täglich
   `feed error: <unknown>:7:2: mismatched tag` — hnrss.org liefert offenbar kein
   gültiges XML mehr (vermutlich eine HTML-Fehlerseite; nicht nachgeprüft).
   Letzter HN-Artikel in der DB: 02.10.
3. **Kein Importance-Filter beim Versand:** Die Mail enthält alle Artikel des Tages
   (zuletzt 50, HTML bis 94 KB), darunter bis zu 20 arXiv-Paper. `--min-importance`
   existiert, wird im Cron aber nicht genutzt.

**Fehler im Code**

4. **Plaintext-Teil der Mail ist falsch:** `send()` ignoriert `--since` und listet
   immer die Top-20 mit Importance ≥4 aus der **gesamten** DB — also jeden Tag
   dieselben alten Artikel. Sichtbar nur in Clients ohne HTML, zählt aber für Spam-Bewertung.
5. **Topic `audio` ohne Metadaten:** fehlt in `TOPIC_META` → erscheint als Sektion
   „📰 audio" und fehlt in der Zählerleiste.
6. **Dedup wählt nicht den besten Artikel:** Sortierung nach Importance greift nicht,
   weil die zum Dedup-Zeitpunkt noch leer ist — es gewinnt einfach die kleinste ID.
   Außerdem sagt der Docstring „≥2 Wörter / Jaccard 0,25", der Code nutzt ≥3 / 0,35.
7. **`max_age_days` aus `sources.yaml` wird ignoriert** — in `collect.py` fest auf 3 verdrahtet.
8. **Neuaufbau der DB würde scheitern:** Spalte `title_de` wurde von Hand angelegt und
   steht in keiner Migration; `summarize_parallel.py` ruft gar kein `ensure_schema` auf.
9. **Dedup-Vergleich ist O(n²) über alle `raw`-Artikel** — bei 50/Tag egal, bei Rückstau nicht.

**Veraltete Reste**

10. Fehlermeldung in `send()` nennt noch `HETZNER_MAIL_PASS` (gemeint: `GMAIL_SMTP_PASS`).
11. Render-Job-Prompt spricht von „MiniMax VF endpoint" — der Vodafone-Endpoint ist seit
    August abgeschaltet, und render ruft ohnehin kein LLM auf.
12. `render.py`-Docstring nennt `digest.sqlite`; `digest.db` ist leer; `summarize.py` ist
    überholt (kennt `title_de` nicht). 1.347 ältere Artikel haben kein `title_de`.
13. **Uncommittete Änderungen** im Git-Repo auf mistral (letzter Commit 18.08.):
    Umstellung Hetzner → Gmail-SMTP, zweiter Empfänger, deutsche Titel (`title_de`),
    deutsche Topic-Labels, robusteres Response-Parsing. `test_api.py` ist untracked.
    Remote ist `github.com/outspokende/news-digest`, dort liegt aber nur der Stand vom
    18.08. — der laufende Code existiert nur auf mistral (und seit 09.10. als Kopie hier).
14. **GitHub-Token im Klartext:** Die Remote-URL in `.git/config` enthält ein
    Personal Access Token (`https://outspokende:ghp_…@github.com/…`). Token rotieren
    und auf SSH-Remote oder Credential-Helper umstellen.
15. **HN-Feed, Ursache:** `https://hnrss.org/frontpage?points=100&tags=AI` antwortet
    mit HTTP 502 (HTML). `frontpage?points=100` und `newest?q=AI&points=100` liefern
    weiterhin gültiges XML (geprüft 09.10.) — der `tags`-Parameter ist das Problem.

## Handgriffe

```bash
ssh henning@192.168.178.105
```

```bash
cd ~/.hermes/news-digest && python3 collect.py --dry-run
```

```bash
cd ~/.hermes/news-digest && python3 render.py --preview --since "$(date +%Y-%m-%d) 00:00:00"
```

```bash
cd ~/.hermes/news-digest && python3 render.py --smtp-check
```

`--preview` schreibt nur `out/DATUM.html` und versendet nichts. `collect.py` ohne
`--dry-run` und `summarize_parallel.py` verändern die DB; `render.py --send`
verschickt sofort an beide Empfänger.
