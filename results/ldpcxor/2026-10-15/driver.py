"""C1 device A/B/A of `ldpcxor` (scheduled LDPC row updates), SYS128, packer + BCH bank on.

A: `dvbs2 <code> 1 20` for normal 2/3, 3/4, 5/6, 9/10 with ldpcxor 0, 1, 0; CRCs must match.
B: QPSK 5/6 16 Msym/s complete SPI GCS pipeline, four writers, L8, GPIO28 DDR clock, 60 s each
   with ldpcxor 0, 1, 0, validated with dual_trial (bus + clock parity).
Flash ./iqbench.uf2 first. Run from this worktree root: python3 results/ldpcxor/2026-10-15/driver.py
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
    if "error" in r and not line.startswith(("dualstream", "dvbs2")):
        raise RuntimeError(f"{line}: {r['error']}")
    return r, lines


def log(stage, label, r):
    with out.open("a") as f:
        f.write(json.dumps(dict(stage=stage, label=label, recorded_at=dt.datetime.now().isoformat(), **r)) + "\n")


try:
    for line in ("info", "dualprio 0", "dualwriters 4", "mapkernel 1", "quadprofile 1", "clock 128000",
                 "bchkernel 2", "bchbank 1", "ldpcpack 1", "dualclock 1"):
        log("setup", line, cmd(line)[0])
    for code in (5, 6, 8, 10):
        for n, xor in enumerate((0, 1, 0)):
            cmd(f"ldpcxor {xor}")
            r, _ = cmd(f"dvbs2 {code} 1 20", 180)
            log("A", f"code{code}-xor{xor}-{n}", r)
            print("A", code, xor, r.get("crc"), r.get("cyc_ldpc"), r.get("cyc_frame"), r.get("prof", {}).get("f_ldpc"), flush=True)
    for n, xor in enumerate((0, 1, 0)):
        cmd(f"ldpcxor {xor}")
        r, lines = cmd("dualstream 1 60000 2 6 4096 0 0 0 4 8", 150)
        if "error" not in r:
            r.update(dual_trial.validate_capture(r, lines, 6, 4096))
        r["faults"] = dual_trial.stream_faults(r, True, 6)
        if r.get("elapsed_us", 0) < 60e6:
            r["faults"].append("run ended before the requested duration")
        log("B", f"gcs16-xor{xor}-{n}", r)
        print("B", xor, n, {k: r.get(k) for k in ("ldpc_xor", "ldpc_pack", "bch_bank", "elapsed_us", "underruns",
                                                  "core0_active_busy", "core1_work_busy", "s2_busy", "cap_mismatches",
                                                  "cap_clock_mismatches", "faults")}, flush=True)
finally:
    for line in ("dualclock 0", "ldpcxor 0", "ldpcpack 0", "bchbank 0", "bchkernel 0", "mapkernel 0",
                 "quadprofile 0", "dualwriters 2"):
        board.cmd(line, timeout=60)
    board.s.close()
