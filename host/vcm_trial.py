"""Continuous QPSK/8PSK VCM trial with an independently checked mode boundary.

Build and flash separately. Both modes use normal5/6, pilots, Gold0, one symbol
clock and one pi/8-rotated L9/N2 shaper. Input is the on-chip PV-SPI pattern.
The capture must include the end of one mode and the next90-symbol PLHEADER.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import functools
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
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


def planned_boundary(clock_khz: int, ms: int) -> int:
    """Exact firmware boundary schedule, with integer midpoint and strict next-frame rule."""
    boundary = frame_start(frame_index(clock_khz * 1000 * ms // 32000) + 1)
    while boundary % base.BLOCK_SYMBOLS > 768:
        boundary = frame_start(frame_index(boundary) + 1)
    return boundary


def faults(result: dict, clock: int, ms: int, expected_git: str | None = None) -> list[str]:
    r = result
    expected = dict(cmd="benchvcm", waveform_plan="8psk_qpsk_alternating", bb_matype1=0x62,
                    clk_hz=clock * 1000, ms=ms, code_index=8, s2_code="normal 5/6",
                    modcod=0, kbch=53840, pilots=True, gold=0, rotation_pi=.125,
                    L=9, sps=2, row_symbols=4, cpw=4, frame_symbols=0,
                    frame_symbols_qpsk=NQ, frame_symbols_8psk=N8,
                    frame_groups_max=2081, input_groups=4096, cap_words=4096,
                    cap_stride=1, cap_search_symbols=base.SEARCH_SYMBOLS, bch_kernel=2,
                    cap_mode_boundary=planned_boundary(clock, ms))
    expected.update(prefill_reference())
    _, _, crcs = base.shaper_profile()
    expected.update(table_crc_a=crcs[0], table_crc_b=crcs[1])
    errors = [f"{k}: got{r.get(k)!r}, expected{v!r}" for k, v in expected.items() if r.get(k) != v]
    if r.get("error"):
        return errors + [r["error"]]
    frames = r.get("encoder_frames")
    if type(frames) is not int or frames <= 2:
        errors.append("alternating frame accounting")
    else:
        qframes, eframes = frames // 2, (frames + 1) // 2
        if r.get("qpsk_frames") != qframes or r.get("psk8_frames") != eframes:
            errors.append("alternating frame accounting")
        if r.get("input_published_groups") != (qframes * NQ + eframes * N8) // 16:
            errors.append("alternating-frame publication accounting")
    rs = clock * 1000 / 16
    for key, want in (("sym_rate", rs), ("bb_payload_bps", rs * 2 * 53760 / PERIOD)):
        if type(r.get(key)) not in (int, float) or not math.isclose(r[key], want, rel_tol=1e-6, abs_tol=.11):
            errors.append(key)
    errors.extend(base.runtime_faults(r, clock, ms, expected_git, ccm=False,
                                     capture_target_symbols=expected["cap_mode_boundary"]))
    if r.get("cap_transition_observed") is not True:
        errors.append("capture did not span a mode change and its full PLHEADER")
    return list(dict.fromkeys(errors))


def require_reply(label: str, result: dict, **expected):
    """Selectors and clock changes must succeed before a trial may begin."""
    if not isinstance(result, dict) or result.get("error") or any(result.get(k) != v for k, v in expected.items()):
        raise RuntimeError(f"{label} failed: {result!r}")


def validator_hashes() -> dict:
    # Include transitive host/reference helpers (including pulse_profiles.py), not
    # just this driver. Stored sources and frozen image identities are separate.
    files = sorted(set((iq.ROOT / "host").rglob("*.py")) | set((iq.ROOT / "reference").rglob("*.py")))
    return {str(f.relative_to(iq.ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def firmware_hashes(source: Path) -> dict:
    files = [f for f in (source / "src").rglob("*") if f.suffix in (".c", ".h", ".S")]
    files += [source / "CMakeLists.txt", source / "git_rev.cmake", source / "pico_sdk_import.cmake"]
    return {"firmware/" + str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def revalidate(directory: Path, expected_git: str, clocks: list[int]) -> dict:
    """Add a validator report without changing raw captures, manifests or run records."""
    destination = directory / "revalidation.json"
    if destination.exists():
        raise FileExistsError(f"refusing to replace additive report: {destination}")
    manifest = json.loads((directory / "manifest.json").read_text())
    # A partially written soak must not be qualified as a finished two-clock trial.
    original_summary = json.loads((directory / "summary.json").read_text())
    rows = [json.loads(line) for line in (directory / "runs.jsonl").read_text().splitlines()]
    rows = [row for row in rows if row.get("cmd") == "benchvcm"]
    raw_paths = [directory / "manifest.json", directory / "summary.json", directory / "runs.jsonl"]
    original_binary_sha256 = {}
    provenance_faults = []
    for name in ("iqbench.uf2", "iqbench.elf"):
        raw_paths.append(directory / name)
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        original_binary_sha256[name] = digest
        if manifest.get("sha256", {}).get(name) != digest:
            provenance_faults.append(f"original {name} differs from saved manifest")
    if sorted(row.get("clk_hz", 0) // 1000 for row in rows) != sorted(clocks):
        provenance_faults.append("saved run clocks do not match requested complete trial")
    if len({row.get("ms") for row in rows}) != 1:
        provenance_faults.append("saved run durations are inconsistent")
    checked = []
    for row in rows:
        clock, ms = row["clk_hz"] // 1000, row["ms"]
        capture_path = directory / f"capture-{clock}.txt"
        raw_paths.append(capture_path)
        record = copy.deepcopy(row)
        capture = validate_capture(record, capture_path.read_text().splitlines())
        record.update(capture)
        errors = faults(record, clock, ms, expected_git)
        checked.append(dict(clock_khz=clock, ms=ms, passed=not errors, faults=errors, capture=capture,
                            firmware_git=record.get("git"), encoder_frames=record.get("encoder_frames"),
                            gen_busy=record.get("gen_busy"), encoder_busy=record.get("encoder_busy")))
    source = Path(manifest.get("source_firmware_directory", manifest.get("source", "")))
    frozen_source_hashes = firmware_hashes(source) if source.is_dir() else {}
    for name, digest in manifest.get("sha256", {}).items():
        if name.startswith("firmware/") and frozen_source_hashes.get(name) != digest:
            provenance_faults.append(f"frozen source differs from saved manifest: {name}")
    report = dict(schema="yapogee-vcm-revalidation-v1", revalidated_at=dt.datetime.now().isoformat(),
                  passed=bool(checked) and not provenance_faults and all(row["passed"] for row in checked),
                  expected_firmware_git=expected_git, original_summary=original_summary,
                  original_binary_sha256=original_binary_sha256, provenance_faults=provenance_faults,
                  raw_sha256={f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in raw_paths},
                  validator_sha256=validator_hashes(), frozen_source_sha256=frozen_source_hashes,
                  runs=checked, scope="Offline stricter revalidation of unchanged saved digital records; no rerun, flash or RF qualification")
    destination.write_text(json.dumps(report, indent=2) + "\n")
    return report


def coherent_record(clock: int, ms: int) -> dict:
    """Synthetic positive metadata with a real independently calculated boundary capture."""
    boundary = planned_boundary(clock, ms)
    cursor = ((boundary + 2048) // 1024) * 1024
    first, reference, lo, hi = reference_window(cursor)
    start = (boundary - 1200 - frame_start(first)) * 4
    assert lo <= start < hi
    record, lines = base.capture_lines(reference[start:start + 8192])
    record.update(cap_symbol_cursor=cursor, cap_search_symbols=base.SEARCH_SYMBOLS,
                  cap_mode_boundary=boundary, cap_stride=1)
    record.update(validate_capture(record, lines))
    hz, rs = clock * 1000, clock * 1000 / 16
    blocks = round(rs * ms / 1000 / 1024)
    frames = frame_index((blocks + 4) * 1024) + 1
    runtime = frames - 2
    stage = {k: runtime * v for k, v in dict(bb=27000, bb_bch=121500, ldpc=128550, map=85000).items()}
    record.update(cmd="benchvcm", git="offline-self-test", clk_hz=hz, ms=ms,
                  waveform_plan="8psk_qpsk_alternating", bb_matype1=0x62, code_index=8,
                  s2_code="normal 5/6", modcod=0, kbch=53840, pilots=True, gold=0,
                  rotation_pi=.125, L=9, sps=2, row_symbols=4, cpw=4, frame_symbols=0,
                  frame_symbols_qpsk=NQ, frame_symbols_8psk=N8, frame_groups_max=2081,
                  input_groups=4096, bch_kernel=2, errors=0, underruns=0, own_errors=0,
                  idle_sent=0, txstalls=0, elapsed_us=ms*1000, blocks=blocks,
                  sym_rate=rs, bb_payload_bps=rs*2*53760/PERIOD,
                  encoder_frames=frames, encoder_runtime_frames=runtime,
                  qpsk_frames=frames//2, psk8_frames=(frames+1)//2,
                  input_published_groups=frame_start(frames)//16,
                  input_consumed_groups=(blocks+4)*64-1, min_input_groups=100,
                  cap_done_blocks=cursor//1024-3, cap_input_groups=cursor//16+1000,
                  gen_cyc_per_sym=12.064, gen_busy=12.064*blocks*1024/(ms*1000*hz/1e6),
                  encoder_busy=sum(stage.values())/(ms*1000*hz/1e6), encoder_elapsed_us=ms*1000,
                  encoder_stage_cycles=stage, encoder_stage_max_cyc={k:v//runtime+100 for k,v in stage.items()})
    record.update(prefill_reference())
    record.update(table_crc_a=base.shaper_profile()[2][0], table_crc_b=base.shaper_profile()[2][1])
    byte_count = frames * 6720
    containers, accepted = (byte_count+187)//188, ((byte_count+187)//188+6)//7
    crc_fields = b"".join(dual_spi.pv.message(k, dual_spi.pv.pattern_payload(k))[-4:] for k in range(16))
    chain = crc_fields*(accepted//16)+crc_fields[:4*(accepted%16)]
    record["pv_spi"] = dict(bb_mode="gcs", sck_hz=32000000 if clock==128000 else 25000000,
        bbframes=frames, runtime_bbframes=runtime, runtime_messages=accepted-10, ok=accepted,
        gcs_bytes=byte_count, gcs_packets=containers, ts_packets=0, null_packets=0,
        crc_chain=zlib.crc32(chain), bb_crc=dual_spi.first_crcs(gcs=True,rate="5/6",ccm=False),
        **{k:0 for k in ("lost","bad_hdr","bad_crc","bad_sync","short_msgs","long_msgs",
                        "overflows","drain_timeouts","late_closes","gcs_underflows")})
    return record


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
    mutations = ("git", "missing_git", "nan_encoder", "infinite_shaper", "missing_stage",
                 "stage_total", "stage_max", "cap_progress", "cap_publication", "cap_time",
                 "BB_count", "clock", "publication", "BB_crc", "CCM_crc", "boundary", "frame_modes")
    for clock in (128000, 150000):
        for ms in (3000, 60000):
            record = coherent_record(clock, ms)
            assert not faults(record,clock,ms,"offline-self-test"),faults(record,clock,ms,"offline-self-test")
            for mutation in mutations:
                bad = copy.deepcopy(record)
                if mutation == "git": bad["git"] = "unrelated-image"
                elif mutation == "missing_git": bad.pop("git")
                elif mutation == "nan_encoder": bad["encoder_busy"] = float("nan")
                elif mutation == "infinite_shaper": bad["gen_busy"] = float("inf")
                elif mutation == "missing_stage": bad["encoder_stage_cycles"] = {}; bad["encoder_stage_max_cyc"] = {}
                elif mutation == "stage_total": bad["encoder_stage_cycles"]["map"] += clock*1000
                elif mutation == "stage_max": bad["encoder_stage_max_cyc"]["map"] = 1
                elif mutation == "cap_progress": bad["cap_done_blocks"] = 0
                elif mutation == "cap_publication": bad["cap_input_groups"] = 0
                elif mutation == "cap_time": bad["cap_symbol_cursor"] = 4096
                elif mutation == "BB_count":
                    stats=bad["pv_spi"];stats["bbframes"]+=1;stats["gcs_bytes"]+=6720
                    stats["gcs_packets"]=(stats["gcs_bytes"]+187)//188
                elif mutation == "clock": bad["clk_hz"] += 1000000
                elif mutation == "publication": bad["input_published_groups"] += 1
                elif mutation == "BB_crc": bad["pv_spi"]["bb_crc"][3] ^= 1
                elif mutation == "CCM_crc": bad["pv_spi"]["bb_crc"] = dual_spi.first_crcs(gcs=True,rate="5/6")
                elif mutation == "boundary": bad["cap_mode_boundary"] += 1
                elif mutation == "frame_modes": bad["qpsk_frames"] -= 1
                assert faults(bad,clock,ms,"offline-self-test"),mutation
    for label, expected in (("BCH",dict(kernel=2)),("map",dict(kernel=1)),("clock",dict(clk_hz=128000000))):
        require_reply(label, expected, **expected)
        for bad in ({},dict(expected,error="failure"),{k:v+1 for k,v in expected.items()}):
            try: require_reply(label,bad,**expected)
            except RuntimeError: pass
            else: raise AssertionError("selector failure accepted")
    print(json.dumps(dict(self_test="passed",metadata_profiles=4,negative_metadata_controls=list(mutations),
        capture="both boundary directions, all8192 samples, corruption and false-boundary controls",selectors="BCH/map/clock failures rejected")))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--out", type=Path)
    mode.add_argument("--revalidate", type=Path, help="offline additive report for an existing complete trial directory")
    p.add_argument("--ms", type=int, choices=(3000, 60000), default=3000)
    p.add_argument("--clocks", type=int, nargs="+", choices=(128000, 150000), default=[128000, 150000])
    p.add_argument("--build", type=Path, default=iq.ROOT / "firmware/build-limits")
    p.add_argument("--source", type=Path, default=iq.ROOT / "firmware")
    p.add_argument("--port")
    p.add_argument("--expected-git", help="require the exact firmware git string")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        self_test(); return
    if a.revalidate:
        if not a.expected_git:
            p.error("--revalidate requires --expected-git")
        report = revalidate(a.revalidate, a.expected_git, a.clocks)
        print(json.dumps(dict(passed=report["passed"],report=str(a.revalidate/"revalidation.json")),indent=2))
        raise SystemExit(0 if report["passed"] else 1)
    if not a.out:
        p.error("--out is required")
    a.out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, a.out / "driver.py")
    source, build = a.source.resolve(), a.build.resolve()
    hashes = validator_hashes()
    hashes.update(firmware_hashes(source))
    for name in ("iqbench.uf2", "iqbench.elf"):
        shutil.copy2(build / name, a.out / name)
        hashes[name] = hashlib.sha256((a.out / name).read_bytes()).hexdigest()
    def git(*args): return subprocess.check_output(["git",*args],cwd=iq.ROOT,text=True).strip()
    (a.out / "manifest.json").write_text(json.dumps(dict(started=dt.datetime.now().isoformat(),
        ms=a.ms,clocks_khz=a.clocks,source_firmware_directory=str(source),build_directory=str(build),
        expected_firmware_git=a.expected_git,head=git("rev-parse","HEAD"),branch=git("branch","--show-current"),
        status=git("status","--short"),sha256=hashes,
        scope="On-chip SPI and continuous alternating DVB-S2 VCM digital waveform; no AFE/RF/receiver qualification",
        provenance="Frozen source/model/binary hashes; source-to-flash identity requires explicit external flashing of this UF2",
        capture="One bounded finite offset, all8192 contiguous halfwords, spanning a mode boundary and full next PLHEADER"), indent=2)+"\n")
    prefill_reference(); base.shaper_profile(); dual_spi.first_crcs(gcs=True,rate="5/6",ccm=False)
    iq.LOGGING = False
    board, failures, fatal = None, [], None
    def record(label, result):
        row=dict(label=label,recorded_at=dt.datetime.now().isoformat(),**result)
        with (a.out / "runs.jsonl").open("a") as f: f.write(json.dumps(row)+"\n")
        print(json.dumps(row),flush=True)
    try:
        board = iq.Board(a.port)
        info,_ = board.cmd("info"); record("info",info)
        require_reply("firmware info",info,**({"git":a.expected_git} if a.expected_git else {}))
        if not info.get("git"): raise RuntimeError("firmware info missing git identity")
        for command,kernel in (("bchkernel 2",2),("mapkernel 1",1)):
            result,_=board.cmd(command); record(command,result); require_reply(command,result,kernel=kernel)
        for clock in a.clocks:
            changed,_=board.cmd(f"clock {clock}"); record(f"clock-{clock}",changed)
            require_reply("clock",changed,clk_hz=clock*1000)
            r, lines = board.cmd(f"benchvcm {a.ms}", timeout=a.ms / 1000 + 60)
            capture=a.out/f"capture-{clock}.txt"; capture.write_text("\n".join(lines)+"\n")
            if not r.get("error"): r.update(validate_capture(r, lines))
            errors=faults(r,clock,a.ms,a.expected_git)
            r.update(faults=errors,classification="failed" if errors else "clean",capture_file=capture.name,
                     host_crc_ok=not errors,measured_scope="standalone continuous VCM digital GCS pipeline; instrumented work fractions, not OS idle")
            record(f"vcm-{clock}",r)
            if errors: failures.append(dict(clock_khz=clock,faults=errors))
    except Exception as exc:
        fatal=dict(exception=type(exc).__name__,detail=str(exc)); record("host-exception",dict(fatal,faults=["host exception"]))
    finally:
        if board is not None:
            for command,expected in (("bchkernel 0",dict(kernel=0)),("mapkernel 0",dict(kernel=0)),("clock 128000",dict(clk_hz=128000000))):
                try:
                    reply,_=board.cmd(command); require_reply("cleanup "+command,reply,**expected)
                except Exception as exc: failures.append(dict(cleanup=command,detail=str(exc)))
            board.s.close()
        summary=dict(passed=not failures and fatal is None,failures=failures,fatal=fatal)
        (a.out / "summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
