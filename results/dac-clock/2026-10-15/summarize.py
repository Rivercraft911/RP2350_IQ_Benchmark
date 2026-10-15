"""Summarize a dual_trial.py --dac-clock ddr campaign from its immutable runs.jsonl.

Run from the repository root: python3 results/dac-clock/2026-10-15/summarize.py <campaign>
Writes <campaign>/summary.json. It reads only the recorded rows; no hardware is accessed.
"""
import hashlib
import json
import sys
from pathlib import Path

folder = Path(sys.argv[1])
rows = [json.loads(line) for line in (folder / "runs.jsonl").read_text().splitlines()]
dual = [r for r in rows if r.get("label", "").startswith("dual-")]
clean = [r for r in dual if r["expectation"] == "clean"]
ceiling = [r for r in dual if r["expectation"] == "ceiling"]  # exploratory; may fail by design
negative = [r for r in dual if r["expectation"] == "negative"]
keys = ("label", "classification", "source", "sym_rate", "ddr_dacclk_hz", "elapsed_us", "dac_clock",
        "dac_clock_pio", "dac_clock_sm", "cap_format", "cap_checked_bus_words", "cap_mismatches",
        "cap_clock_checked", "cap_clock_mismatches", "cap_clock_high", "underruns", "own_errors",
        "txstalls_i", "txstalls_q", "errors", "core0_active_busy", "core0_work_busy",
        "core1_work_busy", "s2_runtime_frames", "negative_control_fault", "negative_control_detected",
        "faults")
summary = dict(
    passed=bool(clean) and all(r["classification"] == "clean" for r in clean) and
    all(r["classification"] == "detected" for r in negative) and
    all(r["classification"] == "clean" for r in rows if r["expectation"] == "clean"),
    clean_60s_ddr_rows=sum(r["classification"] == "clean" and r.get("elapsed_us", 0) >= 60e6 and
                           r.get("dac_clock") == "ddr_pio_gp28" for r in clean + ceiling),
    ceiling_probes=[dict(label=r["label"], classification=r["classification"]) for r in ceiling],
    negative_controls_detected=sum(r["classification"] == "detected" for r in negative),
    runs=[{k: r.get(k) for k in keys if k in r} for r in dual],
    runs_sha256=hashlib.sha256((folder / "runs.jsonl").read_bytes()).hexdigest(),
    scope="Digital only: one-SYS-cycle I/Q parity of GPIO28 against IQ_FLAG at the RP2350 pins, "
          "sparse 17-cycle snapshots; on-chip SPI master; no SN65, AFE, pad-skew or RF measurement")
(folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({k: summary[k] for k in ("passed", "clean_60s_ddr_rows", "negative_controls_detected")}))
