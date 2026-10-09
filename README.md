# KI News Digest

Täglicher KI-Newsletter: RSS-Feeds sammeln → mit MiniMax auf Deutsch zusammenfassen →
als HTML-Mail über Brevo an eine Kontaktliste senden.

- Ist-Zustand auf mistral (bisherige Produktion): [agents.md](agents.md)
- Offene Punkte und Reihenfolge: [verbesserungsplan.md](verbesserungsplan.md)

## Aufbau

| Datei | Zweck |
|---|---|
| `run.py` | Einstiegspunkt für Cron: collect → summarize → render, Log + Totmannschalter |
| `collect.py` | Feeds aus `sources.yaml` holen, deduplizieren, in `news.db` schreiben |
| `summarize_parallel.py` | Ähnliche Titel aussortieren, MiniMax-Zusammenfassung pro Artikel |
| `render.py` | HTML bauen, über Brevo an jeden Empfänger einzeln senden |
| `common.py` | Pfade, Env-Loader, DB-Schema |
| `sources.yaml` | Feeds, Einstellungen, Absender und Brevo-Liste (`mail:`) |

## Empfänger verwalten

Im Brevo-Dashboard unter **Kontakte → Listen**. Das Skript holt die Liste vor jedem
Versand; gesperrte/abgemeldete Kontakte werden übersprungen. Zustellung, Öffnungen und
Klicks pro Person: **Transaktional → Logs**, Filter Tag `news-digest`.

## Einrichtung

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

Secrets nach `.env.example` anlegen — auf Hetzner in `~/secrets/news-digest.env`
(außerhalb des Webroots, `chmod 600`), lokal in `./.env`:

- `MINIMAX_API_KEY` — Zusammenfassungen
- `BREVO_API_KEY` — eigener Key nur für den Digest
- `HEALTHCHECK_URL` — optional, Ping-URL eines Totmannschalters

In `sources.yaml` unter `mail:` die `brevo_list_id` eintragen.

## Benutzung

```bash
venv/bin/python render.py --check
```

Zeigt Absender, Versandweg und die aktuelle Empfängerliste; versendet nichts.

```bash
venv/bin/python run.py --no-send
```

Sammelt, fasst zusammen und schreibt `out/DATUM.html`; versendet nichts.

```bash
venv/bin/python render.py --send --to henning.jaeger@gmail.com --since "2026-10-08 00:00:00"
```

Testmail nur an eine Adresse. Mit `--to` oder `--since` werden Artikel nicht als
versendet markiert.

```bash
venv/bin/python run.py
```

Produktivlauf. Versendet alle verarbeiteten, noch nicht versendeten Artikel
(`sent_at IS NULL`) und markiert sie danach — ein ausgefallener Tag wird am nächsten
nachgeholt, nichts geht doppelt raus.

## Cron (Hetzner)

```
0 17 * * * cd ~/news-digest && venv/bin/python run.py
```

Bei Fehlern gibt `run.py` einen Exit-Code ≠ 0 zurück und schreibt die Ursache auf
stderr (Cron-Mail) sowie nach `logs/DATUM.log`.

## Umzug von mistral

1. `news.db` von mistral kopieren (`~/.hermes/news-digest/news.db`). Beim ersten Start
   wird die Spalte `sent_at` ergänzt und der Bestand als versendet markiert.
2. Die drei Hermes-Cron-Jobs `news-digest-*` auf mistral **löschen**, nicht nur pausieren:
   mistral pausiert abends alle Jobs und startet sie morgens wieder (Nachtabschaltung) —
   ein bloß pausierter Job würde dabei wieder aktiv und die Pipeline liefe doppelt.
3. `render.py --check`, dann eine Testmail mit `--to`, dann Cron eintragen.
