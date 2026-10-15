"""Device measurements of the opt/limits-hotpaths candidates at SYS128.

A: DVB-S2 stage cycles, `ldpcpack 0|1`, bchkernel 2 / mapkernel 1, normal codes with pilots;
   frame CRCs must match between packers.
B: QPSK 5/6 16 Msym/s complete pipeline, SPI GCS source, four writers, L8 quad profile,
   60 s for each (bchbank, ldpcpack) in {0,1}^2, validated with dual_trial's reference.
C: The same with SPI TS input (core-1-limited) with the packer on, against the recorded
   TS ceiling (results/dac-clock/2026-10-15/ts-ceiling-ab).
Flash ./iqbench.uf2 first. Run from the repository root:
  python3 results/optimizations/2026-10-15/driver.py
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


def stream(stage, label, source):
    r, lines = cmd(f"dualstream 1 60000 2 {source} 4096 0 0 0 4 8", 150)
    if "error" not in r:
        r.update(dual_trial.validate_capture(r, lines, source, 4096))
    r["faults"] = dual_trial.stream_faults(r, True, source)
    if r.get("elapsed_us", 0) < 60e6:
        r["faults"].append("run ended before the requested duration")
    log(stage, label, r)
    print(stage, label, {k: r.get(k) for k in ("elapsed_us", "underruns", "core0_active_busy", "core1_work_busy",
                                               "split_bch_busy", "split_bch_max_slice_cyc", "s2_busy",
                                               "cap_mismatches", "faults")}, flush=True)


try:
    for line in ("info", "dualprio 0", "dualwriters 4", "mapkernel 1", "quadprofile 1", "clock 128000",
                 "bchkernel 2", "bchbank 0", "ldpcpack 0"):
        log("setup", line, cmd(line)[0])
    for code in (5, 6, 8, 10):
        crcs = set()
        for pack in (0, 1):
            cmd(f"ldpcpack {pack}")
            r, _ = cmd(f"dvbs2 {code} 1 20", 180)
            crcs.add(r.get("crc"))
            log("A", f"code{code}-pack{pack}", r)
            print("A", code, pack, r.get("code"), r.get("crc"), r.get("cyc_frame"), r.get("prof"), flush=True)
        log("A", f"code{code}-crc-match", dict(crc_match=len(crcs) == 1))
    for bank, pack in ((0, 0), (1, 0), (0, 1), (1, 1)):
        cmd(f"bchbank {bank}"); cmd(f"ldpcpack {pack}")
        stream("B", f"gcs16-bank{bank}-pack{pack}", 6)
    for bank in (0, 1):
        cmd(f"bchbank {bank}"); cmd("ldpcpack 1")
        stream("C", f"ts16-bank{bank}-pack1", 5)
finally:
    for line in ("ldpcpack 0", "bchbank 0", "bchkernel 0", "mapkernel 0", "quadprofile 0", "dualwriters 2"):
        board.cmd(line, timeout=60)
    board.s.close()
