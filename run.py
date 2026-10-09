#!/usr/bin/env python3
"""
news-digest run.py

Ein Einstiegspunkt fuer Cron: collect -> summarize -> render --send.
Bricht beim ersten Fehler ab, schreibt logs/YYYY-MM-DD.log und meldet
Erfolg/Fehler an den Totmannschalter (HEALTHCHECK_URL, optional).

CLI:
    python3 run.py              # kompletter Lauf inkl. Versand
    python3 run.py --no-send    # nur sammeln + zusammenfassen + HTML-Vorschau
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys

import requests

from common import LOG_DIR, ROOT, load_env


def ping(suffix: str = "", body: str = "") -> None:
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        requests.post(url.rstrip("/") + suffix, data=body[-10000:].encode("utf-8"), timeout=10)
    except requests.RequestException:
        pass  # Monitoring darf den Lauf nie zum Scheitern bringen


def step(name: str, args: list[str], log) -> tuple[int, str]:
    log.write(f"\n===== {name}  {dt.datetime.now():%H:%M:%S} =====\n")
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write(proc.stdout)
    log.write(f"----- exit {proc.returncode} -----\n")
    log.flush()
    return proc.returncode, proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true",
                    help="nicht versenden, nur HTML-Vorschau schreiben")
    args = ap.parse_args()

    load_env()
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{dt.date.today().isoformat()}.log"

    steps = [
        ("collect",   ["collect.py"]),
        ("summarize", ["summarize_parallel.py"]),
        ("render",    ["render.py", "--preview" if args.no_send else "--send"]),
    ]

    ping("/start")
    with log_path.open("a", encoding="utf-8") as log:
        for name, cmd in steps:
            rc, out = step(name, cmd, log)
            if rc != 0:
                print(f"news-digest: Schritt '{name}' fehlgeschlagen (exit {rc}), "
                      f"siehe {log_path}", file=sys.stderr)
                print(out[-2000:], file=sys.stderr)
                ping("/fail", f"{name} exit {rc}\n{out}")
                return rc
    ping("", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
