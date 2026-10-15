"""A/B: is the 16 Msym/s TS (source 5) QPSK 5/6 underrun caused by the DDR clock change?

Runs one 60 s `dualstream 1 60000 2 5 4096 0 0 0 4 8` (four writers, L8 quad profile,
packed mapper, BCH kernel 2, SYS128) with the setup commands of dual_trial.py gcs, and
validates it with dual_trial's reference. Flash the firmware under test first.
Run from the repository root:
  python3 results/dac-clock/2026-10-15/ts-ceiling-ab/driver.py --label new-hstx --dac-clock 0
  python3 results/dac-clock/2026-10-15/ts-ceiling-ab/driver.py --label head-hstx
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "host"))
import dual_trial  # noqa: E402
import iqbench as iq  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--label", required=True)
p.add_argument("--dac-clock", type=int, choices=(0, 1), help="omit for firmware without dualclock")
p.add_argument("--repeat", type=int, default=1)
a = p.parse_args()
out = Path(__file__).parent / "runs.jsonl"
iq.LOGGING = False
board = iq.Board(None)


def log(row):
    row = dict(condition=a.label, recorded_at=dt.datetime.now().isoformat(), **row)
    with out.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


try:
    setup = ["info", "dualprio 0", "dualwriters 4"]
    setup += [f"dualclock {a.dac_clock}"] if a.dac_clock is not None else []
    setup += ["mapkernel 1", "quadprofile 1", "clock 128000", "bchkernel 2"]
    for command in setup:
        r, _ = board.cmd(command, timeout=60)
        log(dict(label=command, **r))
        if "error" in r:
            raise RuntimeError(r["error"])
    for n in range(a.repeat):
        r, lines = board.cmd("dualstream 1 60000 2 5 4096 0 0 0 4 8", timeout=150)
        if "error" not in r:
            r.update(dual_trial.validate_capture(r, lines, 5, 4096))
        r["faults"] = dual_trial.stream_faults(r, True, 5)
        row = log(dict(label=f"ts-16msym-5_6-{n}", **r))
        print(a.label, n, {k: row.get(k) for k in ("git", "dac_clock", "elapsed_us", "underruns",
                                                    "core0_active_busy", "core1_work_busy", "faults")},
              flush=True)
finally:
    for command in ("bchkernel 0", "mapkernel 0", "quadprofile 0", "dualwriters 2"):
        board.cmd(command, timeout=60)
    board.s.close()
