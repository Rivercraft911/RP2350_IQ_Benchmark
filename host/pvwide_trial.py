#!/usr/bin/env python3
"""Coordinator runner for the opt-in source-clocked PV ingress benchmark.

Build and flash separately. A device run requires an explicit --port and a fresh
--out directory. --self-test is offline and imports no serial/device modules.
The reported rate measures accepted PV payload, including admission and cleanup.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zlib

from cm5.pv_spi_tx import MSG_BYTES, PAYLOAD, message, pattern_payload

ROOT = Path(__file__).resolve().parents[1]
SYS_HZ, PERIOD = 128000000, 16
COUNTERS = ("closed", "consumed", "started", "ok", "bad_crc", "bad_hdr", "bad_sync", "lost",
            "short_msgs", "long_msgs", "overflows", "errors", "crc_chain", "payload_crc",
            "pattern_mismatches", "close_cycles", "consume_cycles", "drain_timeouts", "late_closes",
            "validated_bytes")


def _uint(value, bits=32):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**bits


def firmware_identity(value):
    """Match the firmware's ten-digit identity without dropping a dirty suffix."""
    return value[:10].lower() if re.fullmatch(r"[0-9a-fA-F]{40}", value) else value


@lru_cache(maxsize=2)
def _period(fault):
    frames = tuple(message(index, pattern_payload(index)) for index in range(PERIOD))
    accepted = tuple(frame for index, frame in enumerate(frames) if not fault or index != 0)
    return frames, b"".join(frame[-4:] for frame in accepted), b"".join(frame[12:-4] for frame in accepted)


def reference(closed, fault):
    """Independent existing CM5 sender + zlib reference for exact closed slots.

    Fault 1 flips message zero's payload byte 20 after calculating its CRC and
    repeats once per 16 messages. The initial rejection is not sequence loss;
    each subsequent rejected zero counts as lost only when message one arrives.
    Fault 2 closes short transfers before message validation.
    """
    if not _uint(closed) or fault not in (0, 1, 2):
        raise ValueError("invalid closed count or fault mode")
    if fault == 2:
        if closed:
            raise ValueError("short control cannot close a full message")
        return dict(ok=0, bad_crc=0, lost=0, crc_chain=0, payload_crc=0)
    frames, crc_period, payload_period = _period(fault)
    crc = payload_crc = 0
    periods, tail = divmod(closed, PERIOD)
    for _ in range(periods):
        crc = zlib.crc32(crc_period, crc)
        payload_crc = zlib.crc32(payload_period, payload_crc)
    for index, frame in enumerate(frames[:tail]):
        if fault and index == 0:
            continue
        crc = zlib.crc32(frame[-4:], crc)
        payload_crc = zlib.crc32(frame[12:-4], payload_crc)
    rejected = (closed + PERIOD - 1) // PERIOD if fault else 0
    return dict(ok=closed - rejected, bad_crc=rejected,
                lost=max(0, (closed - 2) // PERIOD) if fault else 0,
                crc_chain=crc, payload_crc=payload_crc)


def validate_run(result, ms, width, half, fault, expected_git=None, min_mbps=None):
    """Fail closed on metadata, framing, order, content, progress and cleanup."""
    r, faults = result, []
    if not isinstance(r, dict):
        return dict(faults=["response is not an object"], classification="failed")
    expected = dict(cmd="pvwide", sys_hz=SYS_HZ, ms=ms, width=width, half=half, fault=fault,
                    bad_hdr=0, bad_sync=0, long_msgs=0, overflows=0,
                    pattern_mismatches=0, errors=1 if fault == 2 else 0, late_closes=0,
                    first_seq=0, pio=2, sms=3, dma_channels=3, pio_instructions=25,
                    emulate=1, seq_mod=16, period_msgs=16, source="on_chip_pv_pattern")
    for key, value in expected.items():
        if r.get(key) != value or (isinstance(value, int) and not _uint(r.get(key))):
            faults.append(f"{key} differs from requested benchmark")
    if not isinstance(r.get("git"), str) or not r["git"]:
        faults.append("missing firmware identity")
    elif expected_git is not None and r["git"] != firmware_identity(expected_git):
        faults.append("firmware identity differs from archived image")
    for key in ("released", "guard_ok", "source_unchanged"):
        if r.get(key) is not True:
            faults.append(f"{key} is not true")
    if r.get("shutdown_inflight") is not False:
        faults.append("shutdown_inflight is not false")
    if r.get("error"):
        faults.append(str(r["error"]))
    numeric_bad = [key for key in COUNTERS
                   if not _uint(r.get(key), 64 if key.endswith(("_cycles", "_bytes")) else 32)]
    if numeric_bad:
        faults.append("invalid counters: " + ", ".join(numeric_bad))
    programs = []
    for key, length in (("rx_off", 8), ("gate_off", 5), ("tx_off", 12)):
        offset = r.get(key)
        if not _uint(offset) or offset + length > 32:
            faults.append(f"invalid {key}")
        else:
            occupied = set(range(offset, offset + length))
            if any(occupied & other for other in programs):
                faults.append("PIO program ranges overlap")
            programs.append(occupied)
    memory = []
    for key, size in (("slots_addr", 24 * MSG_BYTES),
                      ("sender_addr", PERIOD * (MSG_BYTES - (4 if fault == 2 else 0))),
                      ("state_addr", 4)):
        address = r.get(key)
        if not _uint(address) or address % 4 or not 0x20000000 <= address <= 0x20082000 - size:
            faults.append(f"invalid {key}")
        else:
            end = address + size
            if any(address < other_end and other_start < end for other_start, other_end in memory):
                faults.append("borrowed memory ranges overlap")
            memory.append((address, end))
    elapsed = r.get("elapsed_us")
    if not _uint(elapsed) or not elapsed:
        faults.append("invalid elapsed duration")
    wire_hz = r.get("wire_hz")
    if (isinstance(wire_hz, bool) or not isinstance(wire_hz, (int, float)) or
            not math.isfinite(wire_hz) or abs(wire_hz - SYS_HZ / (2 * half)) >= 1):
        faults.append("sender clock differs from divider")
    rates = {}
    if _uint(elapsed) and elapsed:
        # The benchmark stops admission then drains one maximum-length transfer.
        drain_us = math.ceil(MSG_BYTES * 8 * 2 * half * 1e6 / (SYS_HZ * width)) + 10000
        if not ms * 1000 <= elapsed <= ms * 1000 + drain_us:
            faults.append("elapsed duration differs from request and bounded drain")
        if not numeric_bad:
            if r["started"] != r["closed"] + r["short_msgs"] + r["long_msgs"]:
                faults.append("closed and rejected transfers differ from started count")
            if r["consumed"] != r["closed"]:
                faults.append("not every closed message was consumed")
            if r["validated_bytes"] != r["ok"] * PAYLOAD:
                faults.append("validated bytes differ from accepted payload")
            if not r["started"]:
                faults.append("zero progress")
            if fault == 2:
                if not r["short_msgs"] or r["closed"]:
                    faults.append("short-message negative control was not detected")
                if r["drain_timeouts"] != r["short_msgs"]:
                    faults.append("short transfer drain timeouts differ from rejected transfers")
            elif r["short_msgs"]:
                faults.append("unexpected short message")
            elif r["drain_timeouts"]:
                faults.append("unexpected receive drain timeout")
            if fault == 1 and not r["bad_crc"]:
                faults.append("corrupt-message negative control was not detected")
            if fault != 2 and not r["ok"]:
                faults.append("zero accepted payload progress")
            # Bound the reference work and reject impossible counter values before
            # hashing. Omitting all CS gaps makes this a generous physical bound.
            max_closed = math.ceil(elapsed * SYS_HZ * width / (2 * half * MSG_BYTES * 8 * 1e6)) + 2
            wire_bytes = MSG_BYTES - (4 if fault == 2 else 0)
            max_started = math.ceil(elapsed * SYS_HZ * width / (2 * half * wire_bytes * 8 * 1e6)) + 2
            if r["started"] > max_started:
                faults.append("started count exceeds physical wire capacity")
            if r["closed"] > max_closed:
                faults.append("closed count exceeds physical wire capacity")
            elif fault != 2 or r["closed"] == 0:
                for key, value in reference(r["closed"], fault).items():
                    if r[key] != value:
                        faults.append(f"{key} differs from independent PV reference")
            last = r["closed"] - 1
            if fault == 1 and last % PERIOD == 0:
                last -= 1
            expected_last = last % PERIOD if r["ok"] and fault != 2 else 0
            if not _uint(r.get("last_seq")) or r["last_seq"] != expected_last:
                faults.append("last_seq differs from last accepted message")
            total_cycles = elapsed * SYS_HZ / 1e6
            charged = r["close_cycles"] + r["consume_cycles"]
            if charged > total_cycles:
                faults.append("charged CPU cycles exceed elapsed core cycles")
            if r["started"] and not r["close_cycles"]:
                faults.append("missing CS-close cycle accounting")
            if r["consumed"] and not r["consume_cycles"]:
                faults.append("missing consume cycle accounting")
            payload_bits = r["ok"] * PAYLOAD * 8
            rates = dict(payload_mbps=payload_bits / elapsed,
                         close_busy=r["close_cycles"] / total_cycles,
                         consume_busy=r["consume_cycles"] / total_cycles,
                         close_cycles_per_message=r["close_cycles"] / r["started"] if r["started"] else 0,
                         consume_cycles_per_message=r["consume_cycles"] / r["consumed"] if r["consumed"] else 0,
                         cycles_per_payload_bit=charged / payload_bits if payload_bits else None)
            if fault == 0 and min_mbps is not None and rates["payload_mbps"] < min_mbps:
                faults.append(f"accepted payload rate below {min_mbps:g} Mb/s")
    return dict(rates, faults=list(dict.fromkeys(faults)),
                classification="failed" if faults else
                "short_control_detected" if fault == 2 else
                "crc_control_detected" if fault else "digital_ingress_clean")


def _fixture(width=4, half=3, fault=0, closed=256):
    r = dict(cmd="pvwide", git="self-test", sys_hz=SYS_HZ, ms=100, width=width,
             half=half, fault=fault, elapsed_us=101000, wire_hz=SYS_HZ // (2 * half),
             closed=0 if fault == 2 else closed, consumed=0 if fault == 2 else closed,
             started=closed, short_msgs=closed if fault == 2 else 0, long_msgs=0,
             bad_hdr=0, bad_sync=0, overflows=0, errors=1 if fault == 2 else 0,
             pattern_mismatches=0, close_cycles=120 * closed,
             consume_cycles=0 if fault == 2 else 1200 * closed, released=True,
             guard_ok=True, source_unchanged=True, drain_timeouts=closed if fault == 2 else 0,
             late_closes=0, first_seq=0, last_seq=(closed - 1) % PERIOD if closed and fault != 2 else 0,
             pio=2, sms=3, dma_channels=3, pio_instructions=25,
             emulate=1, seq_mod=16, period_msgs=16, source="on_chip_pv_pattern",
             shutdown_inflight=False,
             slots_addr=0x20040000, sender_addr=0x20048000, state_addr=0x2007f000,
             rx_off=0, gate_off=8, tx_off=13)
    r.update(reference(r["closed"], fault))
    r["validated_bytes"] = r["ok"] * PAYLOAD
    last = r["closed"] - 1
    if fault == 1 and last % PERIOD == 0:
        last -= 1
    r["last_seq"] = last % PERIOD if r["ok"] else 0
    return r


def self_test():
    """Offline: independent vectors and strict mutations; never import iqbench."""
    vector = message(1, bytes([0x47, 0x01, 0x00, 0x10]) + bytes(range(184)))
    assert vector[:16] == bytes.fromhex("50 56 01 01 01 00 bc 00 00 00 00 00 47 01 00 10")
    assert struct.unpack("<I", vector[-4:])[0] == 0x51be328d
    assert zlib.crc32(b"123456789") == 0xcbf43926
    for fault in (0, 1, 2):
        base = _fixture(fault=fault)
        assert not validate_run(base, 100, 4, 3, fault, "self-test")["faults"]
        for key in tuple(base):
            bad = copy.deepcopy(base)
            del bad[key]
            assert validate_run(bad, 100, 4, 3, fault, "self-test")["faults"], key
        for key in COUNTERS:
            if key.endswith("_cycles"):
                continue
            bad = copy.deepcopy(base)
            bad[key] += 1
            assert validate_run(bad, 100, 4, 3, fault, "self-test")["faults"], key
        for key in COUNTERS + ("elapsed_us",):
            for value in (None, True, -1, "1", float("nan"), 2**64):
                bad = copy.deepcopy(base)
                bad[key] = value
                assert validate_run(bad, 100, 4, 3, fault, "self-test")["faults"], (key, value)
        for changes in (dict(git="other"), dict(width=1), dict(half=4), dict(fault=4),
                        dict(sys_hz=150000000), dict(released=False), dict(released=1),
                        dict(guard_ok=False), dict(source_unchanged=False), dict(first_seq=1),
                        dict(last_seq=4), dict(pio=1), dict(sms=2), dict(dma_channels=2),
                        dict(emulate=0), dict(source="external"), dict(seq_mod=65536),
                        dict(period_msgs=0), dict(shutdown_inflight=True), dict(shutdown_inflight=0),
                        dict(pio_instructions=24), dict(rx_off=25), dict(gate_off=0),
                        dict(tx_off=-1), dict(slots_addr=0x20040001),
                        dict(sender_addr=0x20040000), dict(state_addr=0x20090000),
                        dict(wire_hz=float("nan")), dict(wire_hz=True), dict(elapsed_us=1),
                        dict(close_cycles=SYS_HZ), dict(error="setup failed")):
            bad = copy.deepcopy(base)
            bad.update(changes)
            assert validate_run(bad, 100, 4, 3, fault, "self-test")["faults"], changes
    for fault in (0, 1):
        for count in (0, 1, 2, 15, 16, 17, 18, 31, 32, 33, 34, 1000):
            crc = payload_crc = ok = lost = 0
            previous = None
            for index in range(count):
                if fault and index % PERIOD == 0:
                    continue
                seq = index % PERIOD
                frame = message(seq, pattern_payload(seq))
                crc = zlib.crc32(frame[-4:], crc)
                payload_crc = zlib.crc32(frame[12:-4], payload_crc)
                lost += (seq - previous - 1) % PERIOD if previous is not None else 0
                previous = seq
                ok += 1
            assert reference(count, fault) == dict(ok=ok, bad_crc=count-ok, lost=lost,
                                                   crc_chain=crc, payload_crc=payload_crc)
    empty = _fixture(closed=0)
    assert "zero progress" in validate_run(empty, 100, 4, 3, 0)["faults"]
    assert validate_run(_fixture(), 100, 4, 3, 0, min_mbps=47)["faults"]
    assert "iqbench" not in sys.modules
    print("pvwide trial self-test: PASS (offline vectors, counters, CRCs, controls, cleanup)")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive(args, identity):
    args.out.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in ("iqbench.uf2", "iqbench.elf", "git_rev.h", "CMakeCache.txt", "memmap_iq.ld"):
        source = args.build / name
        shutil.copy2(source, args.out / name)
        hashes[name] = _sha(source)
    shutil.copy2(__file__, args.out / "driver.py")
    hashes["driver.py"] = _sha(Path(__file__))
    for directory in (ROOT / "firmware/src", ROOT / "firmware/pio"):
        for source in sorted(directory.rglob("*")):
            if source.is_file() and source.suffix in (".c", ".h", ".S", ".pio"):
                hashes[str(source.relative_to(ROOT))] = _sha(source)
    for relative in ("firmware/CMakeLists.txt", "host/pvwide_trial.py", "host/cm5/pv_spi_tx.py", "host/iqbench.py"):
        hashes[relative] = _sha(ROOT / relative)
    manifest = dict(started=dt.datetime.now(dt.timezone.utc).isoformat(),
                    expected_firmware_git=identity, build_directory=str(args.build),
                    ms=args.ms, widths=args.widths, halves=args.halves, faults=args.faults,
                    reps=args.reps, min_mbps=args.min_mbps, port=args.port,
                    setup="On-chip source-clocked sender and receiver, unconnected external GPIOs",
                    pins=dict(data=[17, 18, 19, 20], sck=21, cs_n=26, ready=22),
                    scope="Digital ingress only; payload rate includes startup and bounded shutdown drain",
                    head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    sha256=hashes)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--port", help="Explicit coordinator-owned CDC port; no automatic discovery")
    p.add_argument("--out", type=Path, help="Fresh evidence directory")
    p.add_argument("--build", type=Path, default=ROOT / "firmware/build-exp")
    p.add_argument("--expected-git", help="Runtime identity (default: archived build's git_rev.h)")
    p.add_argument("--widths", nargs="+", type=int, choices=(1, 4), default=[1, 4])
    p.add_argument("--halves", nargs="+", type=int, choices=range(3, 17), default=[3, 4, 5, 6, 8])
    p.add_argument("--faults", nargs="+", type=int, choices=(0, 1, 2), default=[0, 1, 2])
    p.add_argument("--ms", type=int, default=1000)
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--min-mbps", type=float, help="Required accepted payload rate for fault=0 runs")
    args = p.parse_args(argv)
    if args.self_test:
        self_test()
        return
    if not args.port or args.out is None:
        p.error("device runs require explicit --port and fresh --out")
    if not 100 <= args.ms <= 60000 or args.reps < 1:
        p.error("--ms must be 100..60000 and --reps must be positive")
    if args.min_mbps is not None and (not math.isfinite(args.min_mbps) or args.min_mbps <= 0):
        p.error("--min-mbps must be finite and positive")
    args.build = args.build.resolve()
    if args.out.exists():
        p.error("--out must not already exist; previous evidence is immutable")
    for name in ("iqbench.uf2", "iqbench.elf", "git_rev.h", "CMakeCache.txt", "memmap_iq.ld"):
        if not (args.build / name).is_file():
            p.error(f"missing build artifact: {args.build / name}")
    if "IQ4HIST_EXPERIMENT:BOOL=ON" not in (args.build / "CMakeCache.txt").read_text():
        p.error("--build must contain IQ4HIST_EXPERIMENT=ON firmware")
    matches = re.findall(r'^#define\s+GIT_REV\s+"([^"]+)"', (args.build / "git_rev.h").read_text(), re.M)
    if len(matches) != 1:
        p.error("cannot read exact firmware identity from git_rev.h")
    identity = matches[0]
    if args.expected_git is not None and firmware_identity(args.expected_git) != identity:
        p.error("--expected-git differs from archived build identity")
    _archive(args, identity)
    # Importing this module and all offline validation above needs no pyserial.
    import iqbench as iq
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        info, _ = board.cmd("info")
        (args.out / "info.json").write_text(json.dumps(info, indent=2) + "\n")
        if info.get("cmd") != "info" or info.get("clk_hz") != SYS_HZ or info.get("git") != identity or info.get("error"):
            raise ValueError("device identity/SYS128 differs from archived build")
        for width in args.widths:
            for half in args.halves:
                for fault in args.faults:
                    for rep in range(args.reps):
                        command = f"pvwide {args.ms} {width} {half} {fault}"
                        result, lines = board.cmd(command, timeout=args.ms / 1000 + 20)
                        (args.out / f"reply-w{width}-h{half}-f{fault}-r{rep}.json").write_text(
                            json.dumps(dict(command=command, response=result, lines=lines), indent=2) + "\n")
                        checked = dict(result)
                        checked.update(validate_run(result, args.ms, width, half, fault, identity, args.min_mbps))
                        checked["rep"] = rep
                        with (args.out / "runs.jsonl").open("a") as stream:
                            stream.write(json.dumps(checked) + "\n")
                        print(json.dumps(checked), flush=True)
                        failed |= bool(checked["faults"])
    finally:
        board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
