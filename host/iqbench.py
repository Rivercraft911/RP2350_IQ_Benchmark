"""Host driver for the iqbench firmware: run commands, verify against the Python model, log.

  python3 host/iqbench.py info
  python3 host/iqbench.py bench lut_win 4 10 [--reps 4] [--note "..."]
  python3 host/iqbench.py stream lut_asm 4 10 --cores 0 --cpw 2 --ms 2000 [--cap 4096]
                                                   [--lanes 4 --half 6]
  python3 host/iqbench.py sweep [--note "..."]
  python3 host/iqbench.py dvbs2 3 5 6 11 [--pilots 1]     (code indices: firmware/src/dvbs2_codes.h)
  python3 host/iqbench.py txs2 5 lut_asm_p 4 10 --cpw 2 --ms 3000 --cap 4096   (full transmitter)
  python3 host/iqbench.py raw "clock 150000"

Every measurement is appended to results/optimization-log.jsonl with the host git revision,
the firmware revision it reported, the verification outcome and an optional note.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
import gen_coeffs  # noqa: E402
import iqlut as m  # noqa: E402

LOG = ROOT / "results" / "optimization-log.jsonl"
RPI_VID = 0x2E8A
FAILURES: list[str] = []                        # any entry -> exit status 1


def fail(msg: str):
    FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)


def find_port() -> str:
    ports = [p.device for p in list_ports.comports() if p.vid == RPI_VID]
    if not ports:
        sys.exit("no RP2350 CDC port found (is iqbench flashed and running?)")
    return ports[0]


class Board:
    def __init__(self, port: str | None = None):
        self.s = serial.Serial(port or find_port(), 115200, timeout=0.2)
        time.sleep(0.2)
        self.s.reset_input_buffer()

    def cmd(self, line: str, timeout: float = 30.0) -> tuple[dict, list[str]]:
        """Send one command; return its JSON result and any '@cap' lines. If a capture was
        announced, the lines are returned only when the '@capend' terminator arrived."""
        self.s.write((line + "\n").encode())
        cap, result, end, done = [], None, time.time() + timeout, False
        while time.time() < end:
            raw = self.s.readline().decode(errors="replace").strip()
            if raw.startswith("@cap "):
                cap.append(raw)
            elif raw == "@capend":
                done = True
                break
            elif raw.startswith("@{"):
                result = json.loads(raw[1:])
                if not result.get("cap_words"):
                    break
        if result is None:
            raise TimeoutError(f"no reply to {line!r}")
        if result.get("cap_words") and not done:
            result["cap_error"] = "capture terminator missing"
        return result, cap


def git_rev() -> str:
    return subprocess.run(["git", "describe", "--always", "--dirty", "--abbrev=10"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()


def model_words(sps: int, L: int, seed: int, nwords: int, periodic: bool) -> np.ndarray:
    """Expected output words: whole input from zero history, or one steady-state period."""
    words = m.xorshift32(seed, nwords)
    lut = m.Lut(gen_coeffs.ALPHA, sps, L, 0.0, gen_coeffs.HEADROOM_DB)
    if not periodic:
        return m.generate(words, lut)
    return m.generate(np.concatenate([words, words]), lut)[nwords * 16 * sps:]


def check_capture(r: dict, cap_lines: list[str], seed: int, in_words: int,
                  ref: np.ndarray | None = None) -> dict:
    """Align captured 16-bit bus words to the model stream (periodic PRBS by default) and count
    mismatches."""
    got = np.array([int(x, 16) for l in cap_lines for x in l.split()[2:]], dtype=np.uint32)
    err = r.get("cap_error")
    if not err and len(got) != r["cap_words"]:
        err = f"received {len(got)} of {r['cap_words']} capture words"
    if not err and zlib.crc32(got.tobytes()) != r["cap_crc"]:
        err = "capture CRC mismatch (serial transfer)"
    if err:
        return dict(cap_aligned=False, cap_bus_words=int(2 * len(got)), cap_error=err)
    bus = got.view(np.uint16)
    if ref is None:
        ref = model_words(r["sps"], r["L"], seed, in_words, periodic=True).view(np.uint16)
    key = bus[:16]
    ext = np.concatenate([ref, ref[:len(key)]])
    win = np.lib.stride_tricks.sliding_window_view(ext, len(key))
    hits = np.flatnonzero((win == key).all(axis=1))
    if not len(hits):
        return dict(cap_aligned=False, cap_bus_words=int(len(bus)))
    o = int(hits[0])
    exp = ref[(o + np.arange(len(bus))) % len(ref)]
    bad = np.flatnonzero(bus != exp)
    return dict(cap_aligned=True, cap_bus_words=int(len(bus)), cap_offset=o,
                cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None)


def stream_faults(r: dict, cap: int) -> list[str]:
    """Every condition that makes a streaming run invalid."""
    f = [k for k in ("underruns", "own_errors", "txstalls", "link_overruns") if r.get(k)]
    if not r.get("tables_ok", 1):
        f.append("tables")
    if cap and not (r.get("cap_aligned") and r.get("cap_mismatches") == 0):
        f.append(r.get("cap_error") or ("capture not aligned" if not r.get("cap_aligned")
                                         else f"{r['cap_mismatches']} capture mismatches"))
    return f


def log(rec: dict, note: str | None):
    rec = dict(time=dt.datetime.now().isoformat(timespec="seconds"), host_git=git_rev(),
               note=note, **rec)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def run_bench(b: Board, kernel: str, sps: int, L: int, reps: int, note: str | None) -> dict:
    r, _ = b.cmd(f"bench {kernel} {sps} {L} {reps}", timeout=120)
    if "error" in r:
        raise RuntimeError(r["error"])
    ref = model_words(sps, L, r["seed"], r["in_words"], periodic=False)
    want = zlib.crc32(m.to_layout(ref, r["layout"]).tobytes())
    r["crc_ok"] = r["crc"] == want and bool(r["tables_ok"])
    if not r["crc_ok"]:
        fail(f"bench {kernel} {sps} {L}: output CRC or table check failed")
    print(f"bench {kernel:<9} sps={sps} L={L:<2} {r['cyc_per_sym']:7.2f} cyc/sym "
          f"(worst block {r['worst_block_cyc_per_sym']:.2f}) -> {r['msym_s_one_core']:6.2f} "
          f"Msym/s/core @ {r['clk_hz'] / 1e6:.0f} MHz  crc {'ok' if r['crc_ok'] else 'FAIL'}")
    return log(r, note)


def run_stream(b: Board, kernel, sps, L, cores, cpw, ms, cap, note, lanes=0, half=6) -> dict:
    r, lines = b.cmd(f"stream {kernel} {sps} {L} {cores} {cpw} {ms} {cap} {lanes} {half}",
                     timeout=ms / 1e3 + 60)
    if "error" in r:
        raise RuntimeError(r["error"])
    info, _ = b.cmd("info")
    if cap:
        r.update(check_capture(r, lines, info["seed"], info["in_words"]))
    busy = " ".join(f"c{c['id']}={c['busy'] * 100:.1f}%/lead{c['min_lead']}/iw{c['input_waits']}"
                    for c in r["core"])
    link = (f" link {r['lanes']}x @ {r['link_mbps']:.2f} Mb/s ovr {r['link_overruns']}"
            if r["lanes"] else "")
    capmsg = ""
    if cap:
        capmsg = (f" cap {r['cap_bus_words']} words: " +
                  (f"{r['cap_mismatches']} mismatches" if r["cap_aligned"]
                   else r.get("cap_error", "NOT ALIGNED")))
    r["faults"] = stream_faults(r, cap)
    if r["faults"]:
        fail(f"stream {kernel} {sps} {L}: {', '.join(r['faults'])}")
    print(f"stream {kernel} sps={sps} L={L} cores={cores} {r['sym_rate'] / 1e6:.3f} Msym/s "
          f"{r['ms']} ms: blocks {r['blocks_out']} underruns {r['underruns']} own_err "
          f"{r['own_errors']} txstall {r['txstalls']} busy {busy}{link}{capmsg}")
    return log(r, note)


def dvbs2_ref_symbols(code: str, pilots: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Reference PLFRAME sign bits (bI, bQ) for the firmware's test BBFRAME."""
    sys.path.insert(0, str(ROOT / "reference" / "dvbs2"))
    import dvbs2 as ref
    frame, rate = code.split()
    short = frame == "short"
    kbch = ref.code(rate, short).kbch
    w = m.xorshift32(seed, (kbch + 31) // 32)
    bits = ((w[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()[:kbch]
    return ref.plframe(ref.fecframe(bits, rate, short), rate, short, pilots=bool(pilots))


def pack_symbols(bI: np.ndarray, bQ: np.ndarray) -> np.ndarray:
    """Sign bits -> shaper input words (I_j at bit j, Q_j at bit 16+j); zero-padded to 16."""
    pad = (-len(bI)) % 16
    bI, bQ = (np.concatenate([b, np.zeros(pad, np.uint8)]).reshape(-1, 16).astype(np.uint32)
              for b in (bI, bQ))
    sh = np.arange(16, dtype=np.uint32)
    return ((bI << sh).sum(1) | ((bQ << sh).sum(1) << 16)).astype(np.uint32)


def dvbs2_ref_crc(code: str, pilots: int, seed: int) -> tuple[int, int]:
    """CRC32 and symbol count of the reference PLFRAME for the firmware's test input."""
    bI, bQ = dvbs2_ref_symbols(code, pilots, seed)
    return zlib.crc32(pack_symbols(bI, bQ).tobytes()), len(bI)


def run_txs2(b: Board, index: int, pilots: int, kernel: str, sps: int, L: int, cpw: int, ms: int,
             cap: int, note: str | None) -> dict:
    """Full transmitter: on-chip DVB-S2 encoder (core 1) -> shaper/DMA/PIO (core 0) -> pin capture,
    checked against reference PLFRAMEs (identical frames) run through the LUT model."""
    r, lines = b.cmd(f"txs2 {index} {pilots} {kernel} {sps} {L} {cpw} {ms} {cap}", timeout=ms / 1e3 + 60)
    if "error" in r:
        raise RuntimeError(r["error"])
    if cap:
        bI, bQ = dvbs2_ref_symbols(r["s2_code"], pilots, 0x9E3779B9 ^ index)
        words = pack_symbols(np.tile(bI, 3), np.tile(bQ, 3))
        lut = m.Lut(gen_coeffs.ALPHA, sps, L, 0.0, gen_coeffs.HEADROOM_DB)
        r.update(check_capture(r, lines, 0, 0, ref=m.generate(words, lut).view(np.uint16)))
    r["faults"] = stream_faults(r, cap)
    if r["faults"]:
        fail(f"txs2 {r['s2_code']}: {', '.join(r['faults'])}")
    c0 = r["core"][0]
    print(f"txs2 {r['s2_code']} pilots={pilots} {kernel} sps={sps} L={L} {r['sym_rate'] / 1e6:.3f} Msym/s "
          f"{r['ms']} ms: frames {r['s2_frames']} encoder core1 {r['s2_busy'] * 100:.1f}% "
          f"(min ahead {r['s2_min_ahead_words']} words), shaper core0 {c0['busy'] * 100:.1f}% "
          f"input waits {c0['input_waits']}, underruns {r['underruns']} txstall {r['txstalls']}" +
          (f", cap {r['cap_bus_words']} words: " + (f"{r['cap_mismatches']} mismatches" if r["cap_aligned"]
                                                  else "NOT ALIGNED") if cap else ""))
    return log(r, note)


def run_dvbs2(b: Board, index: int, pilots: int, reps: int, note: str | None) -> dict:
    r, _ = b.cmd(f"dvbs2 {index} {pilots} {reps}", timeout=120)
    if "error" in r:
        raise RuntimeError(r["error"])
    want, n = dvbs2_ref_crc(r["code"], pilots, 0x9E3779B9 ^ index)
    r["crc_ok"] = r["crc"] == want and r["syms"] == n
    if not r["crc_ok"]:
        fail(f"dvbs2 {r['code']}: PLFRAME CRC or length differs from the reference")
    clk = r["clk_hz"]
    for rs in (1e6, 8e6):                                  # encoder load at 1 and 8 Msym/s
        r[f"load_at_{int(rs / 1e6)}msym"] = r["cyc_frame"] / (clk * r["syms"] / rs)
    print(f"dvbs2 {r['code']:<11} pilots={pilots} frame {r['cyc_frame'] / 1e3:7.1f} k cyc "
          f"(bch {r['cyc_bch'] / 1e3:.1f} k vs serial {r['cyc_bch_serial'] / 1e3:.0f} k, "
          f"ldpc {r['cyc_ldpc'] / 1e3:.1f} k vs serial {r['cyc_ldpc_serial'] / 1e3:.0f} k) "
          f"load {r['load_at_1msym'] * 100:.1f} % @1 / {r['load_at_8msym'] * 100:.1f} % @8 Msym/s "
          f"crc {'ok' if r['crc_ok'] else 'FAIL'}")
    return log(r, note)


KERNELS = ("conv", "lut_shift", "lut_win", "lut_pair", "lut_asm")


def sweep(b: Board, note: str | None):
    for sps, Ls in ((2, (8, 10, 12)), (4, (8, 10, 12)), (8, (8, 10))):
        for L in Ls:
            for k in KERNELS:
                run_bench(b, k, sps, L, 1 if k == "conv" else 4, note)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--note")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    p = sub.add_parser("raw")
    p.add_argument("line")
    p = sub.add_parser("bench")
    for a in ("kernel", "sps", "L"):
        p.add_argument(a, type=int if a != "kernel" else str)
    p.add_argument("--reps", type=int, default=4)
    p = sub.add_parser("stream")
    for a in ("kernel", "sps", "L"):
        p.add_argument(a, type=int if a != "kernel" else str)
    p.add_argument("--cores", default="0")
    p.add_argument("--cpw", type=int, required=True)
    p.add_argument("--ms", type=int, default=2000)
    p.add_argument("--cap", type=int, default=0)
    p.add_argument("--lanes", type=int, default=0, help="input over the PIO link (1, 2, 4)")
    p.add_argument("--half", type=int, default=6, help="link SCK half period, system clocks")
    sub.add_parser("sweep")
    p = sub.add_parser("txs2", help="full on-chip transmitter: DVB-S2 encode + shape + stream")
    p.add_argument("index", type=int)
    for a in ("kernel", "sps", "L"):
        p.add_argument(a, type=int if a != "kernel" else str)
    p.add_argument("--pilots", type=int, default=1)
    p.add_argument("--cpw", type=int, required=True)
    p.add_argument("--ms", type=int, default=3000)
    p.add_argument("--cap", type=int, default=4096)
    p = sub.add_parser("dvbs2", help="DVB-S2 encoder benchmark; code index into DVBS2_CODES")
    p.add_argument("index", type=int, nargs="+")
    p.add_argument("--pilots", type=int, default=1)
    p.add_argument("--reps", type=int, default=4)
    a = ap.parse_args()

    b = Board(a.port)
    if a.cmd == "info":
        print(json.dumps(b.cmd("info")[0], indent=1))
    elif a.cmd == "raw":
        print(json.dumps(b.cmd(a.line)[0]))
    elif a.cmd == "bench":
        run_bench(b, a.kernel, a.sps, a.L, a.reps, a.note)
    elif a.cmd == "stream":
        run_stream(b, a.kernel, a.sps, a.L, a.cores, a.cpw, a.ms, a.cap, a.note, a.lanes, a.half)
    elif a.cmd == "sweep":
        sweep(b, a.note)
    elif a.cmd == "txs2":
        run_txs2(b, a.index, a.pilots, a.kernel, a.sps, a.L, a.cpw, a.ms, a.cap, a.note)
    elif a.cmd == "dvbs2":
        for i in a.index:
            run_dvbs2(b, i, a.pilots, a.reps, a.note)
    if FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()
