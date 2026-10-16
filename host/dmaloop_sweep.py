#!/usr/bin/env python3
"""Sweep the `dmaloop` firmware command (DMA row-loop latency) and log raw JSON lines.

Build and flash separately (experiment build). USB-only Pico. Every record is the firmware's
own JSON plus the command line; nothing is filtered.
"""
import argparse, datetime as dt, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import iqbench as iq

def configs():
    for words in (1, 2):
        for sink in (0, 1):
            for chains in (1, 2, 4):
                yield (chains, words, sink, 0, 0, 0, 0)
    for words in (1, 2):
        for kind in (0, 1):
            for div in (32, 16, 8, 6, 4, 3, 2):
                yield (4, words, 1, div, kind, 0, 0)
    for words in (1, 2):
        for prio in (0, 13):
            for hammer in (0, 1):
                yield (4, words, 1, 0, 0, prio, hammer)
                yield (1, words, 1, 0, 0, prio, hammer)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--port")
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists(): raise SystemExit(f"{out} exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    iq.LOGGING = False
    b = iq.Board(a.port)
    info, _ = b.cmd("info")
    with out.open("a") as f:
        f.write(json.dumps(dict(label="info", recorded_at=dt.datetime.now().isoformat(), **info)) + "\n")
        for rep in range(a.reps):
            for cfg in configs():
                line = "dmaloop " + " ".join(map(str, cfg))
                r, _ = b.cmd(line, timeout=30)
                f.write(json.dumps(dict(label=line, rep=rep, recorded_at=dt.datetime.now().isoformat(), **r)) + "\n")
                f.flush()
                print(line, r.get("error") or r.get("cycles_per_iteration"), flush=True)

if __name__ == "__main__":
    main()
