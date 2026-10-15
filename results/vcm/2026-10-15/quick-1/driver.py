"""Continuous QPSK/8PSK VCM trial with an independently checked mode boundary.

Build and flash separately. Both modes use normal5/6, pilots, Gold0, one symbol
clock and one pi/8-rotated L9/N2 shaper. Input is the on-chip PV-SPI pattern.
The capture must include the end of one mode and the next90-symbol PLHEADER.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import json
import math
from pathlib import Path
import shutil
import zlib

import numpy as np

import dual_spi
import iq8live_trial as base
import iqbench as iq

N8, NQ = 22194, 33282
PERIOD = N8 + NQ


def frame_start(index: int) -> int:
    pair, odd = divmod(index, 2)
    return pair * PERIOD + odd * N8


def frame_index(symbol: int) -> int:
    pair, phase = divmod(symbol, PERIOD)
    return 2 * pair + int(phase >= N8)


@functools.lru_cache(maxsize=16)
def frame_points(index: int) -> np.ndarray:
    bb = dual_spi.bbframe(index, gcs=True, rate="5/6", ccm=False)
    fec = dual_spi.ref.fecframe(bb, "5/6")
    if index & 1:
        bi, bq = dual_spi.ref.plframe(fec, "5/6", pilots=True)
        result = ((1 - 2 * bi.astype(float)) + 1j * (1 - 2 * bq.astype(float)))
        result *= np.exp(1j * np.pi / 8) / np.sqrt(2)
        assert len(result) == NQ
    else:
        result = base.scalar_frame(fec, "5/6", False, True)
        assert len(result) == N8
    return result


def prefill_reference() -> dict:
    points = np.concatenate([frame_points(0), frame_points(1)])
    groups = base.packed_groups(points).astype("<u4")
    return dict(prefill_frames=2, prefill_groups=len(groups),
                prefill_fill=len(points) % 16, prefill_crc=zlib.crc32(groups.tobytes()))


def reference_window(cursor: int, search: int = base.SEARCH_SYMBOLS):
    if cursor <= 0 or search != base.SEARCH_SYMBOLS:
        raise ValueError("invalid VCM capture-search metadata")
    lower = max(0, cursor - search)
    first = max(0, frame_index(lower) - 1)
    last = frame_index(cursor + 2 * base.CAP_WORDS // base.BUS_PER_SYMBOL + 1) + 1
    points = np.concatenate([frame_points(k) for k in range(first, last)])
    taps, coef, _ = base.shaper_profile()
    _, axes, _ = base.direct_reference(base.sign_planes(points), taps, coef)
    i, q = (np.asarray(axis, np.int64) & 0x3fff for axis in axes)
    bus = np.stack([i | 0x4000, q], axis=1).ravel().astype("<u2")
    origin = frame_start(first) * base.BUS_PER_SYMBOL
    lo, hi = lower * base.BUS_PER_SYMBOL - origin, cursor * base.BUS_PER_SYMBOL - origin
    assert 0 <= lo < hi and hi + 2 * base.CAP_WORDS <= len(bus)
    assert first == 0 or lo >= 9 * base.BUS_PER_SYMBOL
    return first, bus, lo, hi


def validate_capture(result: dict, lines: list[str]) -> dict:
    checked = base.validate_capture(result, lines, window=reference_window)
    if checked.get("cap_aligned"):
        # The common capture helper uses fixed-frame arithmetic for this label only.
        first = checked["cap_reference_first_frame"]
        checked["cap_global_bus_offset"] = frame_start(first) * 4 + checked["cap_offset"]
        start = checked["cap_global_bus_offset"] / 4
        end = start + 2 * base.CAP_WORDS / 4
        boundary = int(result.get("cap_mode_boundary", 0))
        checked["cap_transition_observed"] = (
            start < boundary and end >= boundary + 90 and
            boundary == frame_start(frame_index(math.floor(start)) + 1))
    return checked


def faults(result: dict, clock: int, ms: int) -> list[str]:
    r = result
    expected = dict(cmd="benchvcm", waveform_plan="8psk_qpsk_alternating", bb_matype1=0x62,
                    clk_hz=clock * 1000, ms=ms, code_index=8, s2_code="normal 5/6",
                    modcod=0, kbch=53840, pilots=True, gold=0, rotation_pi=.125,
                    L=9, sps=2, row_symbols=4, cpw=4, frame_symbols=0,
                    frame_symbols_qpsk=NQ, frame_symbols_8psk=N8,
                    frame_groups_max=2081, input_groups=4096, cap_words=4096,
                    cap_stride=1, cap_search_symbols=base.SEARCH_SYMBOLS, bch_kernel=2)
    expected.update(prefill_reference())
    _, _, crcs = base.shaper_profile()
    expected.update(table_crc_a=crcs[0], table_crc_b=crcs[1])
    errors = [f"{k}: got{r.get(k)!r}, expected{v!r}" for k, v in expected.items() if r.get(k) != v]
    if r.get("error"):
        return errors + [r["error"]]
    for name in ("errors", "underruns", "own_errors", "idle_sent", "txstalls"):
        if r.get(name, -1):
            errors.append(name)
    frames = int(r.get("encoder_frames", 0))
    qframes, eframes = frames // 2, (frames + 1) // 2
    if (frames <= 2 or r.get("encoder_runtime_frames") != frames - 2 or
            r.get("qpsk_frames") != qframes or r.get("psk8_frames") != eframes):
        errors.append("alternating frame accounting")
    published = (qframes * NQ + eframes * N8) // 16
    consumed = (int(r.get("blocks", 0)) + 4) * 64 - 1
    if (r.get("input_published_groups") != published or r.get("input_consumed_groups") != consumed or
            not 0 < published - consumed <= 4096):
        errors.append("publication/history accounting")
    elapsed = r.get("elapsed_us", 0)
    rs = clock * 1000 / 16
    if not ms * 1000 <= elapsed <= ms * 1000 + 50000:
        errors.append("duration")
    if abs(r.get("blocks", 0) - rs * elapsed / 1e6 / 1024) > 8:
        errors.append("output clock/block accounting")
    for key, want in (("sym_rate", rs), ("bb_payload_bps", rs * 2 * 53760 / PERIOD)):
        if not math.isclose(r.get(key, -1), want, rel_tol=1e-6, abs_tol=.11):
            errors.append(key)
    if r.get("pv_spi", {}).get("sck_hz") != (32000000 if clock == 128000 else 25000000):
        errors.append("SPI clock")
    errors.extend(dual_spi.packet_faults(dict(r, source=6), ccm=False))
    if not (r.get("cap_transport_ok") and r.get("cap_aligned") and
            r.get("cap_mismatches") == 0 and r.get("cap_checked_bus_words") == 8192):
        errors.append(r.get("cap_error") or "capture mismatch")
    if not r.get("cap_transition_observed"):
        errors.append("capture did not span a mode change and its full PLHEADER")
    return list(dict.fromkeys(errors))


def self_test():
    assert prefill_reference() == dict(prefill_frames=2, prefill_groups=3467,
                                       prefill_fill=4, prefill_crc=215158474)
    assert dual_spi.first_crcs(gcs=True, rate="5/6", ccm=False) == [
        2430356946, 1153874154, 1617543358, 3202956426]
    for index in (1, 2, 17, 200):
        boundary = frame_start(index)
        cursor = ((boundary + 2048) // 1024) * 1024
        first, reference, lo, hi = reference_window(cursor)
        start = (boundary - 1200 - frame_start(first)) * 4
        assert lo <= start < hi
        bus = reference[start:start + 8192]
        r, lines = base.capture_lines(bus)
        r.update(cap_symbol_cursor=cursor, cap_search_symbols=base.SEARCH_SYMBOLS,
                 cap_mode_boundary=boundary, cap_stride=1)
        good = validate_capture(r, lines)
        assert good["cap_transition_observed"] and good["cap_mismatches"] == 0, good
        bad = bus.copy(); bad[1000] ^= 1
        br, bl = base.capture_lines(bad)
        checked = validate_capture(dict(r, **br), bl)
        assert not checked.get("cap_aligned") or checked.get("cap_mismatches")
        assert not validate_capture(dict(r, cap_mode_boundary=boundary + 1), lines).get("cap_transition_observed")
    print("VCM reference: both boundary directions, unflushed prefill, VCM BBHEADER, corruption and false-boundary controls PASS")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path)
    p.add_argument("--ms", type=int, choices=(3000, 60000), default=3000)
    p.add_argument("--clocks", type=int, nargs="+", choices=(128000, 150000), default=[128000, 150000])
    p.add_argument("--build", type=Path, default=iq.ROOT / "firmware/build-limits")
    p.add_argument("--source", type=Path, default=iq.ROOT / "firmware")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        self_test(); return
    if not a.out:
        p.error("--out is required")
    a.out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, a.out / "driver.py")
    hashes = {}
    for name in ("iqbench.uf2", "iqbench.elf"):
        shutil.copy2(a.build / name, a.out / name)
        hashes[name] = hashlib.sha256((a.out / name).read_bytes()).hexdigest()
    for f in (a.source / "src").glob("*"):
        if f.is_file():
            hashes["firmware/src/" + f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    (a.out / "manifest.json").write_text(json.dumps(dict(started=dt.datetime.now().isoformat(),
        source=str(a.source), build=str(a.build), sha256=hashes,
        scope="On-chip SPI and continuous alternating DVB-S2 VCM digital waveform; no AFE/RF/receiver qualification"), indent=2) + "\n")
    iq.LOGGING = False
    board = iq.Board()
    failed = False
    try:
        for command in ("bchkernel 2", "mapkernel 1"):
            board.cmd(command)
        for clock in a.clocks:
            board.cmd(f"clock {clock}")
            r, lines = board.cmd(f"benchvcm {a.ms}", timeout=a.ms / 1000 + 30)
            (a.out / f"capture-{clock}.txt").write_text("\n".join(lines) + "\n")
            if "error" not in r:
                r.update(validate_capture(r, lines))
            r["faults"] = faults(r, clock, a.ms)
            r["classification"] = "failed" if r["faults"] else "clean"
            with (a.out / "runs.jsonl").open("a") as f:
                f.write(json.dumps(r) + "\n")
            print(json.dumps(r), flush=True)
            failed |= bool(r["faults"])
    finally:
        try:
            for command in ("bchkernel 0", "mapkernel 0", "clock 128000"):
                board.cmd(command)
        finally:
            board.s.close()
    (a.out / "summary.json").write_text(json.dumps(dict(passed=not failed), indent=2) + "\n")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
