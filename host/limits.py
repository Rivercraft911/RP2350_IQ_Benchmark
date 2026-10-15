"""Reproducible digital-only experiment sweep; disconnect every GPIO before use.

Runs the current firmware at 128/150 MHz, preserves failed ceiling probes as data,
and restores 128 MHz and the baseline BCH selection on exit. Never flashes firmware.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import iqbench as iq


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("profile", choices=("quick", "soak", "ddr", "two-sps"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--build", type=Path, default=Path("firmware/build-limits"))
    p.add_argument("--port")
    p.add_argument("--ms", type=int, default=60000, help="soak duration per stream")
    a = p.parse_args()
    if not 1000 <= a.ms <= 600000:
        p.error("--ms must be 1000..600000")
    a.out.mkdir(parents=True, exist_ok=True)
    if (a.out / "runs.jsonl").exists():
        p.error("choose a new --out directory so previous runs stay immutable")
    iq.LOGGING = False  # this experiment keeps its own log, including negative results

    def git(*args):
        return subprocess.run(["git", *args], cwd=iq.ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()

    source = [f for folder in ("firmware/src", "host", "reference") for f in (iq.ROOT / folder).rglob("*")
              if f.suffix in (".c", ".h", ".S", ".py") and "build" not in f.parts]
    source += [iq.ROOT / "firmware/CMakeLists.txt"]
    hashes = {str(f.relative_to(iq.ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()
              for f in sorted(source)}
    for name in ("iqbench.uf2", "iqbench.elf"):
        f = a.build / name
        hashes[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), profile=a.profile,
                    head=git("rev-parse", "HEAD"), branch=git("branch", "--show-current"),
                    status=git("status", "--short"), sha256=hashes,
                    provenance="Local source/reference and build snapshot; flash this UF2 separately before running.",
                    setup="USB only, GPIOs unconnected; stock voltage; digital-only")
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    board = iq.Board(a.port)
    failures = []

    def record(label, fn):
        try:
            r = fn()
        except Exception as e:
            r = dict(exception=type(e).__name__, detail=str(e))
            with (a.out / "runs.jsonl").open("a") as f:
                f.write(json.dumps(dict(label=label, **r)) + "\n")
            raise
        with (a.out / "runs.jsonl").open("a") as f:
            f.write(json.dumps(dict(label=label, recorded_at=dt.datetime.now().isoformat(), **r)) + "\n")
        print(label, json.dumps({k: v for k, v in r.items() if k in (
            "clk_hz", "cyc_bch", "cyc_frame", "cyc_per_sym", "crc_ok", "crc_match", "faults",
            "one_core_busy", "s2_runtime_frames", "sym_rate", "txstall", "data_match",
            "bad_clock_words", "clock_words")}), flush=True)
        faults = list(r.get("faults", []))
        for field in ("crc_ok", "crc_match"):
            if field in r and not r[field]: faults.append(field)
        if faults and not label.endswith("-ceiling-probe"):
            failures.append((label, faults))
        return r

    def raw(cmd):
        def run():
            r, _ = board.cmd(cmd)
            if "error" in r: raise RuntimeError(r["error"])
            if cmd.startswith("bench8psk"):
                r["crc_ok"] = r["crc_c"] == r["crc_asm"] == 4224917667
            return r
        return record(cmd, run)

    def tx(label, cpw, one, ms, sps=4):
        return record(label, lambda: iq.run_txs2(board, 5, 1, "lut_asm_p" if sps == 4 else "lut_asm", sps, 12,
                                                 cpw, ms, 4096, label, single_core=one))

    def pv(label, cpw, one, ms):
        return record(label, lambda: iq.run_pvtx(board, cpw, ms, True, 4096, 20,
                                                label, single_core=one))

    try:
        raw("info")
        raw("clock 128000")
        if a.profile == "quick":
            for kernel in (0, 1, 2):
                raw(f"bchkernel {kernel}")
                for code in (5, 8):
                    record(f"fec-k{kernel}-code{code}",
                           lambda code=code: iq.run_dvbs2(board, code, 1, 8, None))
            r = raw("bench8psk 4")
            r_ok = r["crc_c"] == r["crc_asm"] == 4224917667
            if not r_ok: raise RuntimeError("8PSK differs from independent Python reference")
            record("qpsk-after-8psk", lambda: iq.run_bench(board, "lut_asm_p", 4, 12, 4, None))
            raw("bchkernel 0")
            tx("two-core-baseline-128", 2, False, 3000)
            raw("bchkernel 2")
            tx("two-core-optimized-128", 2, False, 3000)
            for cpw in (16, 8, 4, 3): tx(f"one-core-128-cpw{cpw}", cpw, True, 3000)
            for cpw in (4, 3): pv(f"one-core-spi-128-cpw{cpw}", cpw, True, 3000)
            raw("clock 150000")
            raw("bench8psk 4")
            tx("two-core-optimized-150", 2, False, 3000)
            for cpw in (4, 3): tx(f"one-core-150-cpw{cpw}", cpw, True, 3000)
            pv("one-core-spi-150-cpw4", 4, True, 3000)
            pv("one-core-spi-150-cpw3", 3, True, 3000)
        elif a.profile == "two-sps":
            raw("bchkernel 2")
            tx("two-sps-two-core-128-10.667Msym", 3, False, 3000, sps=2)
            tx("two-sps-one-core-128-6.4Msym", 5, True, 3000, sps=2)
            tx("two-sps-one-core-128-8Msym-ceiling-probe", 4, True, 3000, sps=2)
            raw("clock 150000")
            tx("two-sps-two-core-150-12.5Msym", 3, False, 10000, sps=2)
            tx("two-sps-one-core-150-7.5Msym", 5, True, 10000, sps=2)
        elif a.profile == "soak":
            raw("bchkernel 2")
            pv("one-core-spi-128-5.333Msym-soak", 3, True, a.ms)
            raw("clock 150000")
            pv("one-core-spi-150-6.25Msym-soak", 3, True, a.ms)
            tx("two-core-150-9.375Msym-soak", 2, False, a.ms)
            tx("two-sps-two-core-150-12.5Msym-soak", 3, False, a.ms, sps=2)
            tx("two-sps-one-core-150-7.5Msym-soak", 5, True, a.ms, sps=2)
        else:
            r = raw("ddrtest 100")
            if (r["txstall"] or not r["data_match"] or r["bad_clock_words"] or
                    r["clock_words"] != 4096 or r["data_checked_words"] != 8192):
                raise RuntimeError("DDR component check failed")
            # Verify the standalone test released resources and restored baseline mux/state.
            tx("two-core-after-ddr", 2, False, 3000)
    finally:
        try:
            raw("clock 128000")
            raw("bchkernel 0")
        finally:
            board.s.close()
    if failures:
        raise RuntimeError(f"unexpected validation failures: {failures}")


if __name__ == "__main__":
    main()
