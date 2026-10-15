"""DDR clock parity on every dual-DMA writer layout, short PRBS runs.

The 60 s campaign covers four writers with four-symbol rows only. This runs 3 s cyclic-
PRBS streams (source 1, interpolator descriptors) for each writer/row layout at divider
2 and 1, plus the inverted-clock control per layout, and validates bus and clock parity
with host/dual_trial.py. Two-writer layouts put the clock SM in PIO2, four-writer in PIO0.
Divider 1 with two writers is a known DMA ceiling for some layouts; such runs are kept
as recorded and classified separately. Flash the firmware under test first. Run from the
repository root: python3 results/dac-clock/2026-10-15/layout-coverage/driver.py
"""
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "host"))
import dual_trial  # noqa: E402
import iqbench as iq  # noqa: E402

LAYOUTS = (  # (writers, row_symbols, firmware row words) -> PIO programs used
    (2, 1, "out_code"), (2, 4, "paired_i/q_code"), (4, 2, "four_i/q_code"), (4, 4, "quad_four_i/q_code"))
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


def log(row):
    with out.open("a") as f:
        f.write(json.dumps(dict(recorded_at=dt.datetime.now().isoformat(), **row)) + "\n")


try:
    for setup in ("info", "dualprio 0", "mapkernel 0", "quadprofile 0", "clock 128000", "bchkernel 0"):
        log(dict(label=setup, **cmd(setup)[0]))
    for writers, rows, programs in LAYOUTS:
        log(dict(label=f"dualwriters {writers}", **cmd(f"dualwriters {writers}")[0]))
        for mode, cpw in ((1, 2), (1, 1), (2, 2)):
            log(dict(label=f"dualclock {mode}", **cmd(f"dualclock {mode}")[0]))
            r, lines = cmd(f"dualstream {cpw} 3000 2 1 4096 0 0 0 {rows} 5", 90)
            if "error" not in r:
                r.update(dual_trial.validate_capture(r, lines, 1, 4096))
            r["faults"] = dual_trial.stream_faults(r, True, 1)
            r["programs"] = programs
            r["negative_control_detected"] = dual_trial.negative_control_detected(3, r) if mode == 2 else None
            label = f"w{writers}-row{rows}-cpw{cpw}-clock{mode}"
            log(dict(label=label, **r))
            print(label, {k: r.get(k) for k in ("error", "sym_rate", "dac_clock_pio", "dac_clock_sm",
                                                "cap_mismatches", "cap_clock_mismatches", "cap_clock_checked",
                                                "underruns", "txstalls_i", "txstalls_q",
                                                "negative_control_detected")}, flush=True)
finally:
    for line in ("dualclock 0", "dualwriters 2"):
        board.cmd(line, timeout=60)
    board.s.close()
