"""Combined-image check on integration/yapogee-2026-10-15: DDR clock + LDPC packer + BCH bank.

QPSK 5/6 16 Msym/s complete pipeline, four writers, L8 quad profile, SYS128, GPIO28 DDR
clock (pins32 captures, clock parity), ldpcpack 1, bchbank 1: 60 s with SPI GCS and SPI TS
input, then the inverted-clock control. Flash ./iqbench.uf2 first. Run from repo root:
  python3 results/integration/2026-10-15/driver.py
"""
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "host"))
import dual_trial  # noqa: E402
import iqbench as iq  # noqa: E402

out = Path(__file__).parent / "runs.jsonl"
if out.exists():
    sys.exit("runs.jsonl exists; keep previous results immutable")
iq.LOGGING = False
board = iq.Board(None)


def cmd(line, timeout=60):
    r, lines = board.cmd(line, timeout=timeout)
    if "error" in r and not line.startswith("dualstream"):
        raise RuntimeError(f"{line}: {r['error']}")
    return r, lines


def log(label, r):
    with out.open("a") as f:
        f.write(json.dumps(dict(label=label, recorded_at=dt.datetime.now().isoformat(), **r)) + "\n")


def stream(label, source, ms, mode):
    cmd(f"dualclock {mode}")
    r, lines = cmd(f"dualstream {'1' if ms > 1000 else '2'} {ms} 2 {source} 4096 0 0 0 4 8", 150)
    if "error" not in r:
        r.update(dual_trial.validate_capture(r, lines, source, 4096))
    r["faults"] = dual_trial.stream_faults(r, True, source)
    if r.get("elapsed_us", 0) < ms * 1000:
        r["faults"].append("run ended before the requested duration")
    if mode == 2:
        r["negative_control_detected"] = dual_trial.negative_control_detected(3, r)
    log(label, r)
    print(label, {k: r.get(k) for k in ("git", "dac_clock", "elapsed_us", "underruns", "core0_active_busy",
                                        "core1_work_busy", "cap_mismatches", "cap_clock_mismatches",
                                        "cap_clock_checked", "negative_control_detected", "faults")}, flush=True)


try:
    for line in ("info", "dualprio 0", "dualwriters 4", "mapkernel 1", "quadprofile 1", "clock 128000",
                 "bchkernel 2", "bchbank 1", "ldpcpack 1"):
        log(line, cmd(line)[0])
    stream("gcs16-ddr-pack1-bank1", 6, 60000, 1)
    stream("ts16-ddr-pack1-bank1", 5, 60000, 1)
    stream("prbs8-ddr-inverted-control", 1, 1000, 2)
finally:
    for line in ("dualclock 0", "ldpcpack 0", "bchbank 0", "bchkernel 0", "mapkernel 0", "quadprofile 0",
                 "dualwriters 2"):
        board.cmd(line, timeout=60)
    board.s.close()
