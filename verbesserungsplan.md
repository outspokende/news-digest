# KI News Digest — Verbesserungsplan

Stand 2026-10-09. Grundlage: Code-Kopie von mistral in diesem Ordner und die Analyse
in [agents.md](agents.md). Reihenfolge = empfohlene Bearbeitungsreihenfolge.

## Stand der Umsetzung (2026-10-09, nur lokal in diesem Ordner — nicht committet, nicht ausgerollt)

Erledigt und lokal getestet (gegen eine Kopie der `news.db`, Brevo-API simuliert):
1.1–1.7, 2.6, 3.1, 3.5, 3.6, 3.7, B.2–B.4 (Code), B.6 (Code: `run.py`, Env-Datei, Healthcheck-Ping).

Offen: Phase 0 komplett · 1.8 entfällt mit Abschalten der Hermes-Jobs · 1.9 · Phase 2 außer 2.6
(wartet auf Entscheidung zur Artikelzahl) · 3.2–3.4, 3.8 · B.1 API-Key + Listen-ID eintragen ·
B.5 · B.6 Ausrollen auf Hetzner · B.7 · echter Testversand über Brevo.

Leitgedanke: Die Pipeline funktioniert und ist klein. Der Plan baut sie nicht um,
sondern macht sie erst sicher und korrekt, dann lesenswerter, dann robuster.

## Phase 0 — Sofort (vor jeder Code-Änderung)

| # | Was | Warum | Aufwand |
|---|---|---|---|
| 0.1 | **GitHub-Token rotieren.** Das PAT steht im Klartext in `.git/config` (mistral und diese Kopie). Auf GitHub widerrufen, Remote auf `git@github.com:outspokende/news-digest.git` oder Credential-Helper umstellen. | Token ist in zwei Dateien und im Sitzungsprotokoll sichtbar | 10 min |
| 0.2 | **Laufenden Stand committen und pushen.** Fünf geänderte Dateien seit 18.08. (Gmail-SMTP, zweiter Empfänger, `title_de`, deutsche Labels). `test_api.py` löschen oder committen. | Einzige Kopie des Produktivcodes lag bis heute nur auf mistral | 10 min |
| 0.3 | ~~Klären, warum alle Cron-Jobs pausiert sind~~ — geklärt: planmäßige Nachtabschaltung von mistral (abends pausieren, morgens starten). Kein Handlungsbedarf. | — | — |
| 0.4 | **`news.db` sichern.** Prüfen, ob `ospn-backup-daily` `~/.hermes/news-digest/` erfasst; sonst aufnehmen. | 2.306 zusammengefasste Artikel, nicht reproduzierbar | 10 min |

## Phase 1 — Fehler beheben (ein Nachmittag)

Alles kleine, voneinander unabhängige Änderungen.

| # | Datei | Änderung |
|---|---|---|
| 1.1 | `sources.yaml` | HN-Feed reparieren: `tags=AI` führt zu HTTP 502. Ersetzen durch `https://hnrss.org/newest?q=AI&points=100` (liefert XML, geprüft) und einen Tag lang auf Trefferqualität beobachten. |
| 1.2 | `render.py` `send()` | Plaintext-Teil aus derselben Artikelliste bauen wie das HTML (Liste als Parameter übergeben) statt eigener DB-Abfrage ohne `--since`. |
| 1.3 | `render.py` | `audio` in `TOPIC_META` aufnehmen; Zählerleiste aus `TOPIC_META` ableiten statt fester Liste. |
| 1.4 | `collect.py` | `max_age_days` aus `settings` lesen statt fest `3`. |
| 1.5 | `summarize_parallel.py` | `ensure_schema()` (inkl. `title_de`) hierher verschieben und beim Start aufrufen. Danach `summarize.py` löschen. |
| 1.6 | `summarize_parallel.py` | Dedup: Docstring an Code angleichen (≥3 Wörter / Jaccard 0,35). Auswahl des „besten" ehrlich machen — z. B. Quelle mit Priorität (Originalquelle vor Aggregator) statt wirkungsloser Importance-Sortierung. |
| 1.7 | `render.py` | Veraltetes bereinigen: Meldung `HETZNER_MAIL_PASS`, Docstring `digest.sqlite`; leere `digest.db` löschen. |
| 1.8 | Hermes `jobs.json` | Render-Prompt: „using MiniMax VF endpoint" streichen. |
| 1.9 | `sources.yaml` | Aider, Lilian Weng, The Gradient: 0 Artikel in zwei Monaten. Prüfen (Feed tot oder nur selten?) und ggf. entfernen; Kommentar „22 Quellen" korrigieren. |

## Phase 2 — Der Digest wird lesbar (größter Nutzen)

Heute: 50 Artikel pro Mail, ungefiltert, bis 94 KB. Davon ~20 arXiv-Paper, die nur
anhand von Titel und Abstract-Anfang bewertet werden.

| # | Änderung | Details |
|---|---|---|
| 2.1 | **Wichtigkeitsfilter aktivieren** | Cron auf `--min-importance 3` (würde heute ~23 % entfernen) oder besser zweistufig: ★4–5 ausführlich, ★3 als einzeilige Linkliste „Außerdem", ★1–2 weglassen. |
| 2.2 | **arXiv bändigen** | arXiv liefert 30 % aller Artikel und stößt täglich an den 20er-Cap — d. h. es wird willkürlich abgeschnitten. Optionen: Keyword-Filter im Collector (Kommentar in `sources.yaml` kündigt ihn seit August an), eigener Cap pro Quelle (`max_items: 5`), oder arXiv nur ab ★4 in die Mail. |
| 2.3 | **„Top 3 des Tages" oben in die Mail** | Die ★5/★4-Artikel topic-übergreifend als Einstieg; danach erst die Sektionen. |
| 2.4 | **Importance kalibrieren** | 25 % aller Artikel sind ★4–5 — zu großzügig für „kritisch/breaking". Prompt schärfen (konkrete Beispiele pro Stufe, Zielverteilung nennen) und an 50 Alt-Artikeln gegenprüfen. |
| 2.5 | **Dedup verbessern** | Nur 71 Duplikate bei 2.306 Artikeln; dieselbe Meldung von TechCrunch, The Decoder und HN kommt meist dreifach durch, weil der Vergleich rein auf englischen Titelwörtern beruht. Günstigste Stufe: Dedup *nach* dem Zusammenfassen zusätzlich auf `title_de` + Topic; nächste Stufe: Duplikate nicht verwerfen, sondern als „auch bei: …" am Hauptartikel anzeigen. |
| 2.6 | **Leere Tage** | `render.py` beendet sich bei 0 Artikeln mit Exit 1 → Agent meldet „Fehler". Sauberer: Exit 0 + Meldung, Agent antwortet `[SILENT]`. |

Empfehlung: 2.1 und 2.2 zuerst — zwei kleine Änderungen, die die Mail etwa halbieren.

## Phase 3 — Robustheit und Betrieb

| # | Änderung | Details |
|---|---|---|
| 3.1 | **Ein Einstiegspunkt statt drei Zeitfenster** | `run.py` (oder `run.sh`) ruft collect → summarize → render nacheinander auf und bricht bei Fehlern ab. Ein Hermes-Job statt drei; kein Risiko mehr, dass render vor fertigem summarize läuft; zwei LLM-Agent-Läufe und zwei Statusmails pro Tag weniger. |
| 3.2 | **Agent-Lauf überdenken** | Der Agent führt nur ein Kommando aus und formuliert die Ausgabe um (und irrt dabei, z. B. „token counts" im Summarize-Bericht vom 08.10.). Alternative: systemd-Timer wie bei `ospn-backup-daily`, Mail nur bei Fehler. Hermes bleibt sinnvoll, wenn die Telegram-Anbindung gewünscht ist. |
| 3.3 | **Feed-Ausfälle erkennen** | Tabelle `feed_runs` (Quelle, Zeit, ok, Anzahl, Fehler). Warnung im Digest-Footer, wenn eine Quelle ≥3 Tage in Folge fehlschlägt oder 14 Tage nichts liefert. HN war sechs Tage kaputt, ohne dass es auffiel. |
| 3.4 | **Fehlgeschlagene Zusammenfassungen begrenzen** | Parse-Fehler bleiben `raw` und werden endlos neu versucht. Zähler `attempts`, nach 3 Versuchen `status='failed'`. |
| 3.5 | **`--since` zeitzonenfest machen** | `processed_at` ist UTC, `--since` wird als Ortszeit übergeben. Passt nur, weil der Job abends läuft. Besser: „seit letztem erfolgreichen Versand" über Spalte `sent_at` — dann geht auch nach einem ausgefallenen Tag nichts verloren und nichts doppelt raus. |
| 3.6 | **Empfänger an einer Stelle** | Derzeit in `render.py` (`MAIL_TO`) *und* im Cron-Prompt. In `sources.yaml`/eigene `config.yaml` verschieben. Bei zwei Empfängern außerdem einzeln versenden statt beide im `To:`. |
| 3.7 | **Abhängigkeiten festhalten** | `requirements.txt` + venv statt System-Python. |
| 3.8 | **Tests für die reine Logik** | `title_hash`, `is_fresh`, `find_duplicate_clusters`, `parse_response`, `group_by_topic` — alles ohne Netz testbar, ca. 15 Tests. |
| 3.9 | **README + `agents.md` ins Repo** | Betriebsdoku liegt bisher nur hier. |

## Phase 3b — Zielarchitektur: Hetzner + Brevo (entschieden 2026-10-09)

Rahmen: 10–15 bekannte Empfänger; gewünscht ist eine Oberfläche zum Ein-/Austragen
und zur Kontrolle von Zustellung und Öffnungen. Dafür wird **keine eigene UI gebaut** —
das Brevo-Dashboard deckt beides ab.

| # | Schritt | Details |
|---|---|---|
| B.1 | Vorhandenes Brevo-Konto nutzen | Das Konto von outspoken.de (Projekt `os2026-claude`, Versand als `info@outspoken.de` über die Transaktions-API) wird mitbenutzt. Dafür: **eigener API-Key** nur für den Digest (getrennt widerrufbar), Absender z. B. `digest@outspoken.de` — die Domain ist dort vermutlich schon authentifiziert (im Brevo-Dashboard prüfen). Jede Mail mit Tag `news-digest`, damit sie sich im gemeinsamen Log von den App-Mails trennen lässt. Tarif/Tageslimit des Kontos prüfen: Digest und App-Registrierungsmails teilen sich das Kontingent. Nicht von der Gmail-Adresse senden (DMARC). |
| B.2 | Kontaktliste „KI News Digest" in Brevo | Einzige Quelle für Empfänger. Ein-/Austragen im Brevo-Dashboard. `MAIL_TO` und `--to` im Cron entfallen (ersetzt 3.6). |
| B.3 | `render.py`: Versand über Brevo-API | Vor dem Versand Kontakte der Liste abrufen (`GET /v3/contacts/lists/{id}/contacts`), dann **eine transaktionale Mail pro Empfänger** (`POST /v3/smtp/email`) mit Tag `news-digest` — nur so sind Zustellung und Öffnung pro Person sichtbar. SMTP bleibt als Fallback hinter derselben Funktion. |
| B.4 | Abmeldelink in der Mail | Brevo-Abmeldeplatzhalter im Template; abgemeldete/gesperrte Kontakte beim Abruf überspringen. |
| B.5 | Zustellkontrolle | Brevo → Transaktional → Logs/Statistik zeigt je Empfänger gesendet / zugestellt / geöffnet / geklickt / Bounce. Optional: Tagesbericht im Skript über `GET /v3/smtp/statistics/events`, Warnung bei Bounce. |
| B.6 | Umzug auf Hetzner | Ein `run.py` + systemd-Timer (3.1), Secrets in `.env` (`MINIMAX_API_KEY`, `BREVO_API_KEY`), Totmannschalter (healthchecks.io), tägliches Backup von `news.db`. Der Server ist laut `os2026-claude` ein Hetzner **Managed Server** (`dedi3724.your-server.de`, SSH-Port 222, kein Root). **Geprüft am 09.10. (Account `ospnz`, nur lesend):** Python 3.11.2, venv + pip funktionieren, SQLite 3.40, `crontab` vorhanden, ausgehendes HTTPS zu Feeds, MiniMax und Brevo offen, Zeitzone Europe/Berlin, 47 GB frei. `feedparser` und `jinja2` fehlen systemweit → eigenes venv mit `requirements.txt` (3.7 wird damit Pflicht). Zeitsteuerung per Cron statt systemd-Timer: `0 17 * * * ~/news-digest/venv/bin/python ~/news-digest/run.py`. Secrets nach dem Muster von outspoken in `~/secrets/news-digest.env` außerhalb des Webroots. Datenbank bleibt SQLite — eine Tabelle, ein Schreiber, 5 MB; MySQL/Postgres brächte hier nur Umbauaufwand. |
| B.7 | Empfänger informieren | Kurz mitteilen, dass Öffnungen/Klicks gemessen werden. |

Grenzen der Öffnungsmessung: Sie beruht auf einem Zählpixel. Apple Mail lädt Bilder
vorab (zeigt „geöffnet", obwohl ungelesen); Clients mit blockierten Bildern zeigen
„nicht geöffnet", obwohl gelesen. Klicks auf Artikel-Links sind das verlässlichere Signal.

Mengen: 15 Empfänger × 1 Mail/Tag liegt weit unter dem Brevo-Freikontingent (300/Tag).

## Phase 4 — Optional, erst nach Rückmeldung der Leser

- **Volltext statt Feed-Teaser:** Für ★4–5 den Artikel abrufen und auf Basis des
  Inhalts zusammenfassen. Bessere Zusammenfassungen, aber mehr Tokens und Scraping-Pflege.
- **Fehlende Quellen ersetzen:** Anthropic, Meta AI, Cursor, ElevenLabs haben keinen
  Feed mehr; Reddit blockiert. Kandidaten: GitHub-Releases-Feeds (Claude Code, Aider,
  Ollama), HN-Algolia-API, Substack-Feeds.
- **Wochenrückblick** am Sonntag aus den ★5 der Woche.
- **`title_de` nachziehen** für die 1.347 Altartikel — nur nötig, falls ein Archiv/Web-Ansicht entsteht.
- **Web-Archiv** der `out/*.html` (z. B. über den vorhandenen `podcast-server`).

## Offene Entscheidungen

1. **Wie viele Artikel soll die Mail haben?** Bestimmt Schwelle und arXiv-Behandlung in Phase 2.
2. **Hermes-Agent behalten oder systemd-Timer?** (3.2) — hängt davon ab, ob die
   täglichen Statusberichte per Mail/Telegram gewünscht sind.
3. **Arbeitsweise:** Entwickeln hier im Ordner, per `git push` / `git pull` nach
   mistral ausrollen? Setzt 0.1 und 0.2 voraus.
