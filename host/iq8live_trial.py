"""Full 8PSK/GCS digital trial: finite independent FEC/PL/FIR capture verification.

Build and flash separately. This runner never builds or flashes firmware. The Pico
receives its own on-chip PV-SPI pattern; the host reconstructs only a small window
for validation. No host radio processing, external-CM5, AFE, or RF claim is made.

  python3 host/iq8live_trial.py --self-test
  python3 host/iq8live_trial.py --out results/8psk-live/quick --ms 3000
  python3 host/iq8live_trial.py --out results/8psk-live/soak --ms 60000
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

import iqbench as iq
import dual_spi
from dual_trial import parse_capture
from test_dvbs2_8psk import packed_groups, scalar_frame, sign_planes
from test_iq8quad import AQ30, BQ30, coefficients, direct_reference

RATE = "5/6"
FRAME_SYMBOLS = 22194
BB_BITS = 53840
DATA_BYTES = (BB_BITS - 80) // 8
CAP_WORDS = 4096
BUS_PER_SYMBOL = 4  # two samples/symbol, each I then Q
BLOCK_SYMBOLS = 1024
PREFILL_FRAMES = 2
OUT_BLOCKS = 4
INPUT_GROUPS = 4096
SEARCH_SYMBOLS = OUT_BLOCKS * BLOCK_SYMBOLS + 32


@functools.lru_cache(maxsize=16)
def frame_points(index: int) -> np.ndarray:
    """Random-access real GCS BBFRAME -> scalar BCH/LDPC -> complete rotated PLFRAME."""
    bits = dual_spi.bbframe(index, gcs=True, rate=RATE)
    fec = dual_spi.ref.fecframe(bits, RATE, False)
    result = scalar_frame(fec, RATE, False, True, 0)
    assert len(result) == FRAME_SYMBOLS
    result.flags.writeable = False
    return result


@functools.lru_cache(maxsize=1)
def shaper_profile():
    """Recreate the fixed L9/N2 preEQ pulse and both complete signed plane tables."""
    taps, coef = coefficients()
    key = np.arange(4096, dtype=np.int64)[:, None]
    crcs = []
    for factor in (AQ30, BQ30):
        cp = (coef * factor + (1 << 29)) >> 30
        rows = []
        for symbol in range(4):
            signs = 1 - 2 * ((key >> (8 + symbol - np.arange(9))) & 1)
            values = ((signs @ cp.reshape(9, 2) + 128) >> 8).astype(np.uint16).astype(np.uint32)
            rows.append(values[:, 0] | values[:, 1] << 16)
        crcs.append(zlib.crc32(np.stack(rows, axis=1).astype('<u4').tobytes()))
    return taps, coef, crcs


@functools.lru_cache(maxsize=1)
def prefill_reference() -> dict:
    points = np.concatenate([frame_points(k) for k in range(PREFILL_FRAMES)])
    groups = packed_groups(points).astype('<u4')  # no per-frame or end-of-stream padding
    return dict(prefill_frames=PREFILL_FRAMES, prefill_groups=len(groups),
                prefill_fill=len(points) % 16, prefill_crc=zlib.crc32(groups.tobytes()))


def reference_window(cursor: int, search: int = SEARCH_SYMBOLS) -> tuple[int, np.ndarray, int, int]:
    """A finite source window with real previous-frame FIR history, never a cyclic model.

    Candidate capture starts are strictly before the generated-symbol cursor, in
    the preceding search-symbol interval. Extra future symbols only complete each
    candidate's contiguous capture; they do not enlarge the allowed start range.
    """
    if cursor <= 0 or search != SEARCH_SYMBOLS:
        raise ValueError("invalid finite capture-search metadata")
    lower = max(0, cursor - search)
    first = max(0, lower // FRAME_SYMBOLS - 1)
    end = cursor + math.ceil(2 * CAP_WORDS / BUS_PER_SYMBOL) + 1
    last = (end + FRAME_SYMBOLS - 1) // FRAME_SYMBOLS
    points = np.concatenate([frame_points(k) for k in range(first, last)])
    taps, coef, _ = shaper_profile()
    _, axes, _ = direct_reference(sign_planes(points), taps, coef)
    i, q = (np.asarray(axis, np.int64) & 0x3fff for axis in axes)
    bus = np.stack([i | 0x4000, q], axis=1).ravel().astype('<u2')
    base = first * FRAME_SYMBOLS * BUS_PER_SYMBOL
    lo, hi = lower * BUS_PER_SYMBOL - base, cursor * BUS_PER_SYMBOL - base
    assert 0 <= lo < hi and hi + 2 * CAP_WORDS <= len(bus)
    assert first == 0 or lo >= 9 * BUS_PER_SYMBOL  # artificial local prehistory is never searched
    return first, bus, lo, hi


def validate_capture(result: dict, lines: list[str], *, window=reference_window) -> dict:
    """Match ALL8192 halfwords at one offset in the bounded finite source window."""
    try:
        if result.get('cap_error'):
            raise ValueError(str(result['cap_error']))
        got = parse_capture(lines)
        if len(got) != CAP_WORDS or result.get('cap_words') != CAP_WORDS:
            raise ValueError(f"incomplete capture: got{len(got)}, reported{result.get('cap_words')}")
        if zlib.crc32(got.tobytes()) != result.get('cap_crc'):
            raise ValueError('capture CRC mismatch (serial transfer)')
        if result.get('cap_stride') != 1:
            raise ValueError('capture must contain contiguous bus halfwords')
    except (ValueError, TypeError) as exc:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(exc))
    bus = got.view('<u2')
    cursor, search = int(result['cap_symbol_cursor']), int(result['cap_search_symbols'])
    first, reference, lo, hi = window(cursor, search)
    # Both ends are finite: no wrap, modulo, padding, or independent per-sample phase choices.
    hi = min(hi, len(reference) - len(bus) + 1)
    candidates = np.flatnonzero(reference[lo:hi] == bus[0]) + lo
    for j in range(1, min(32, len(bus))):
        candidates = candidates[reference[candidates + j] == bus[j]]
        if not len(candidates):
            break
    best = None
    for offset in candidates:
        bad = np.flatnonzero(reference[int(offset):int(offset) + len(bus)] != bus)
        if best is None or len(bad) < len(best[1]):
            best = int(offset), bad
        if not len(bad):
            break
    axes = ((bus >> 14) & 1).astype(np.uint8)
    alternating = np.array_equal(axes, (int(axes[0]) ^ (np.arange(len(bus)) & 1)))
    physical_ok = alternating and not bool(np.any(bus & 0x8000))
    if best is None:
        return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_reference_first_frame=first,
                    cap_start_search_bus_words=hi-lo,
                    cap_error='no bounded finite offset matches the first32 bus observations')
    offset, bad = best
    first_bad = int(bad[0]) if len(bad) else None
    return dict(cap_transport_ok=True, cap_aligned=physical_ok, cap_offset=offset,
                cap_global_bus_offset=first * FRAME_SYMBOLS * BUS_PER_SYMBOL + offset,
                cap_reference_first_frame=first, cap_start_search_bus_words=hi-lo,
                cap_axis_counts=np.bincount(axes, minlength=2).tolist(),
                cap_physical_iq_ok=physical_ok, cap_mismatches=int(len(bad)),
                cap_checked_bus_words=len(bus), cap_first_bad=first_bad,
                cap_got_first_bad=int(bus[first_bad]) if first_bad is not None else None,
                cap_want_first_bad=int(reference[offset+first_bad]) if first_bad is not None else None)


def runtime_faults(result: dict, clock_khz: int, ms: int, expected_git: str | None = None,
                   *, ccm: bool = True, capture_target_symbols: float | None = None) -> list[str]:
    """Common digital-trial accounting; frame-specific publication stays with the caller."""
    r = result
    faults = []
    if not isinstance(r.get('git'), str) or not r['git'] or (expected_git and r['git'] != expected_git):
        faults.append('firmware git identity missing or differs from request')
    for name in ('errors', 'underruns', 'own_errors', 'idle_sent', 'txstalls'):
        if r.get(name, -1) != 0:
            faults.append(name)
    # These counters feed arithmetic below. Reject missing/noninteger/nonfinite values
    # before converting; malformed metadata must produce a fault rather than false-pass.
    counters = ('elapsed_us', 'blocks', 'encoder_frames', 'encoder_runtime_frames',
                'input_published_groups', 'input_consumed_groups', 'cap_symbol_cursor',
                'cap_done_blocks', 'cap_input_groups', 'min_input_groups', 'encoder_elapsed_us')
    invalid = [name for name in counters if type(r.get(name)) is not int or r[name] < 0]
    if invalid:
        return faults + ['invalid runtime counters: ' + ', '.join(invalid)]
    hz, rs = clock_khz * 1000, clock_khz * 1000 / 16
    elapsed, blocks, frames = r['elapsed_us'], r['blocks'], r['encoder_frames']
    if elapsed < ms * 1000 or elapsed > ms * 1000 + 50000:
        faults.append('elapsed duration differs from request')
    if blocks <= 0 or abs(blocks - rs * elapsed / 1e6 / BLOCK_SYMBOLS) > 8:
        faults.append('output block count differs from continuous requested sample clock')
    runtime = frames - PREFILL_FRAMES
    if frames <= PREFILL_FRAMES or r['encoder_runtime_frames'] != runtime:
        faults.append('no coherent runtime encoder frame count')
    consumed = (blocks + OUT_BLOCKS) * (BLOCK_SYMBOLS // 16) - 1
    if r['input_consumed_groups'] != consumed:
        faults.append('mapped input retained-history count mismatch')
    if not 0 < r['input_published_groups'] - consumed <= INPUT_GROUPS:
        faults.append('mapped input ring lead outside retained-history capacity')
    cursor = r['cap_symbol_cursor']
    if cursor % BLOCK_SYMBOLS or not OUT_BLOCKS * BLOCK_SYMBOLS <= cursor <= (blocks + OUT_BLOCKS) * BLOCK_SYMBOLS:
        faults.append('capture cursor differs from generated block boundary')
    target = rs * ms / 2000 if capture_target_symbols is None else capture_target_symbols
    if not math.isfinite(target) or abs(cursor - target) > 6 * BLOCK_SYMBOLS:
        faults.append('capture cursor is outside the planned output window')
    if not 0 <= cursor // BLOCK_SYMBOLS - r['cap_done_blocks'] <= OUT_BLOCKS:
        faults.append('capture DMA completion/cursor lead mismatch')
    if not cursor // 16 <= r['cap_input_groups'] <= cursor // 16 + INPUT_GROUPS:
        faults.append('capture input publication outside ring capacity')
    if not 0 <= r['min_input_groups'] <= INPUT_GROUPS:
        faults.append('input lead diagnostic outside ring capacity')
    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)
    for key in ('gen_busy', 'encoder_busy'):
        if not finite(r.get(key)) or not 0 < r[key] <= 1.001:
            faults.append(f'invalid {key}')
    stage, stage_max = r.get('encoder_stage_cycles'), r.get('encoder_stage_max_cyc')
    keys = {'bb', 'bb_bch', 'ldpc', 'map'}
    valid_stage = isinstance(stage, dict) and set(stage) == keys and all(type(v) is int and v > 0 for v in stage.values())
    valid_max = isinstance(stage_max, dict) and set(stage_max) == keys and all(type(v) is int and v > 0 for v in stage_max.values())
    if not valid_stage:
        faults.append('encoder stage cycle accounting missing or invalid')
    if not valid_max:
        faults.append('encoder stage maxima missing or invalid')
    if valid_stage and valid_max and runtime > 0 and any(stage[k] > stage_max[k] * runtime for k in keys):
        faults.append('encoder stage total exceeds reported per-frame maximum')
    enc_us = r['encoder_elapsed_us']
    if (enc_us <= 0 or not valid_stage or not finite(r.get('encoder_busy')) or
            not math.isclose(sum(stage.values()) / (enc_us * hz / 1e6), r['encoder_busy'], abs_tol=.00015)):
        faults.append('encoder work/budget fraction mismatch')
    if (elapsed <= 0 or not finite(r.get('gen_cyc_per_sym')) or r['gen_cyc_per_sym'] <= 0 or
            not finite(r.get('gen_busy')) or not math.isclose(
                r['gen_cyc_per_sym'] * blocks * BLOCK_SYMBOLS / (elapsed * hz / 1e6), r['gen_busy'], abs_tol=.00015)):
        faults.append('shaper work/budget fraction mismatch')
    spi = r.get('pv_spi', {})
    if not isinstance(spi, dict):
        return faults + ['invalid PV-SPI statistics']
    if spi.get('sck_hz') != (32000000 if clock_khz == 128000 else 25000000):
        faults.append('on-chip SPI clock differs from requested profile')
    if spi.get('bbframes') != frames or spi.get('runtime_bbframes') != runtime:
        faults.append('GCS/encoder BBFRAME count mismatch')
    try:
        faults.extend(dual_spi.packet_faults(dict(r, source=6), ccm=ccm))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        faults.append(f'invalid packet accounting: {type(exc).__name__}')
    if not (r.get('cap_transport_ok') and r.get('cap_aligned') and r.get('cap_mismatches') == 0 and
            r.get('cap_checked_bus_words') == 2 * CAP_WORDS):
        faults.append(r.get('cap_error') or 'complete finite capture mismatch')
    return list(dict.fromkeys(faults))


def stream_faults(result: dict, clock_khz: int, ms: int, expected_git: str | None = None) -> list[str]:
    """Fail closed on fixed-8PSK profile and common runtime discrepancies."""
    r = result
    if r.get('error'):
        return [f"firmware error: {r['error']}"]
    expected = dict(cmd='bench8live', clk_hz=clock_khz*1000, ms=ms, code_index=8,
                    s2_code='normal 5/6', modcod=15, kbch=BB_BITS, pilots=True,
                    gold=0, rotation_pi=.125, L=9, sps=2, row_symbols=4, cpw=4,
                    frame_symbols=FRAME_SYMBOLS, frame_groups_max=1388,
                    input_groups=INPUT_GROUPS, cap_words=CAP_WORDS, cap_stride=1,
                    cap_search_symbols=SEARCH_SYMBOLS, bch_kernel=2)
    expected.update(prefill_reference())
    _, _, crcs = shaper_profile()
    expected.update(table_crc_a=crcs[0], table_crc_b=crcs[1])
    faults = [f"{key}={r.get(key)!r}, expected{value!r}" for key, value in expected.items() if r.get(key) != value]
    hz, rs = clock_khz*1000, clock_khz*1000/16
    for key, want in (('sym_rate', rs), ('bb_payload_bps', rs*(BB_BITS-80)/FRAME_SYMBOLS)):
        if type(r.get(key)) not in (int, float) or not math.isclose(r[key], want, rel_tol=1e-6, abs_tol=.11):
            faults.append(f'{key} differs from requested mode')
    frames = r.get('encoder_frames')
    if type(frames) is not int or r.get('input_published_groups') != frames*FRAME_SYMBOLS//16:
        faults.append('mapped input publication count mismatch')
    faults.extend(runtime_faults(r, clock_khz, ms, expected_git))
    return list(dict.fromkeys(faults))


def capture_lines(bus: np.ndarray) -> tuple[dict, list[str]]:
    words = np.asarray(bus, dtype='<u2').view('<u4')
    lines = ['@cap '+str(k)+' '+' '.join(f'{int(w):08x}' for w in words[k:k+16]) for k in range(0,len(words),16)]
    return dict(cap_words=len(words), cap_crc=zlib.crc32(words.tobytes()), cap_stride=1), lines


def self_test():
    """Entirely offline positive/negative controls, including real cross-frame captures."""
    cursor = 44*BLOCK_SYMBOLS
    first, bus, lo, hi = reference_window(cursor)
    boundary = 2*FRAME_SYMBOLS*BUS_PER_SYMBOL-first*FRAME_SYMBOLS*BUS_PER_SYMBOL
    for phase in (0, 1):
        offset = boundary-1000*BUS_PER_SYMBOL+phase
        assert lo <= offset < hi
        r, lines = capture_lines(bus[offset:offset+2*CAP_WORDS])
        r.update(cap_symbol_cursor=cursor, cap_search_symbols=SEARCH_SYMBOLS)
        check = validate_capture(r, lines)
        assert check['cap_aligned'] and check['cap_mismatches'] == 0 and check['cap_checked_bus_words'] == 8192
        assert check['cap_global_bus_offset'] == first*FRAME_SYMBOLS*BUS_PER_SYMBOL+offset
        bad = bus[offset:offset+2*CAP_WORDS].copy();bad[5000] ^= 1
        br, bl = capture_lines(bad);br.update(cap_symbol_cursor=cursor, cap_search_symbols=SEARCH_SYMBOLS)
        bc = validate_capture(br, bl);assert bc['cap_mismatches'] == 1 and bc['cap_first_bad'] == 5000
        for malformed in (lines[1:], [lines[0],lines[0],*lines[1:]], lines[:-1]):
            assert not validate_capture(r,malformed)['cap_transport_ok']
        serial_bad = dict(r,cap_crc=r['cap_crc']^1)
        assert not validate_capture(serial_bad,lines)['cap_transport_ok']
        outside = max(0,lo-400)
        br, bl = capture_lines(bus[outside:outside+8192]);br.update(cap_symbol_cursor=cursor,cap_search_symbols=SEARCH_SYMBOLS)
        assert not validate_capture(br,bl)['cap_aligned']
        # A cyclic model could wrongly accept this; no complete finite slice exists.
        wrapped = np.concatenate([bus[-128:],bus[:8192-128]])
        br, bl = capture_lines(wrapped);br.update(cap_symbol_cursor=cursor,cap_search_symbols=SEARCH_SYMBOLS)
        assert not validate_capture(br,bl)['cap_aligned']
    assert prefill_reference() == dict(prefill_frames=2,prefill_groups=2774,prefill_fill=4,prefill_crc=1685928093)
    assert dual_spi.first_crcs(gcs=True,rate=RATE) == [1325498631,2602267711,3216247915,1630859359]
    # Build fully coherent metadata at both hardware clock/duration profiles.
    for clock,ms in ((128000,3000),(150000,60000)):
        hz=clock*1000;rs=hz/16
        cursor=math.ceil((rs*ms/2000+2*BLOCK_SYMBOLS)/BLOCK_SYMBOLS)*BLOCK_SYMBOLS
        _,ref,lo,_=reference_window(cursor)
        record,lines=capture_lines(ref[lo+128:lo+128+8192])
        record.update(cap_symbol_cursor=cursor,cap_search_symbols=SEARCH_SYMBOLS)
        record.update(validate_capture(record,lines))
        blocks=round(rs*ms/1000/BLOCK_SYMBOLS)
        frames=math.ceil((blocks+OUT_BLOCKS)*BLOCK_SYMBOLS/FRAME_SYMBOLS)+1
        runtime=frames-PREFILL_FRAMES
        stage={k:runtime*cycle for k,cycle in dict(bb=10000,bb_bch=115000,ldpc=126000,map=50000).items()}
        busy=sum(stage.values())/(ms*1000*hz/1e6)
        record.update(cmd='bench8live',git='offline-self-test',clk_hz=hz,ms=ms,code_index=8,
                      s2_code='normal 5/6',modcod=15,kbch=BB_BITS,pilots=True,gold=0,rotation_pi=.125,
                      L=9,sps=2,row_symbols=4,cpw=4,frame_symbols=FRAME_SYMBOLS,frame_groups_max=1388,
                      input_groups=INPUT_GROUPS,bch_kernel=2,elapsed_us=ms*1000,blocks=blocks,
                      encoder_frames=frames,encoder_runtime_frames=runtime,errors=0,underruns=0,
                      own_errors=0,idle_sent=0,txstalls=0,sym_rate=rs,bb_payload_bps=rs*(BB_BITS-80)/FRAME_SYMBOLS,
                      input_published_groups=frames*FRAME_SYMBOLS//16,
                      input_consumed_groups=(blocks+OUT_BLOCKS)*64-1,min_input_groups=100,
                      cap_done_blocks=cursor//BLOCK_SYMBOLS-3,cap_input_groups=cursor//16+1000,
                      gen_cyc_per_sym=11.84,gen_busy=11.84*blocks*BLOCK_SYMBOLS/(ms*1000*hz/1e6),
                      encoder_busy=busy,encoder_elapsed_us=ms*1000,encoder_stage_cycles=stage,
                      encoder_stage_max_cyc={k:v//runtime+100 for k,v in stage.items()})
        record.update(prefill_reference())
        record.update(table_crc_a=shaper_profile()[2][0],table_crc_b=shaper_profile()[2][1])
        byte_count=frames*DATA_BYTES;containers=(byte_count+187)//188;accepted=(containers+6)//7
        crc_fields=b''.join(dual_spi.pv.message(k,dual_spi.pv.pattern_payload(k))[-4:] for k in range(16))
        chain=crc_fields*(accepted//16)+crc_fields[:4*(accepted%16)]
        record['pv_spi']=dict(bb_mode='gcs',sck_hz=32000000 if clock==128000 else 25000000,
            bbframes=frames,runtime_bbframes=runtime,runtime_messages=accepted-10,ok=accepted,
            gcs_bytes=byte_count,gcs_packets=containers,ts_packets=0,null_packets=0,
            crc_chain=zlib.crc32(chain),bb_crc=dual_spi.first_crcs(gcs=True,rate=RATE),
            **{k:0 for k in ('lost','bad_hdr','bad_crc','bad_sync','short_msgs','long_msgs',
                             'overflows','drain_timeouts','late_closes','gcs_underflows')})
        assert not stream_faults(record,clock,ms,'offline-self-test'),stream_faults(record,clock,ms,'offline-self-test')
        for mutation in ('clock','bbcrc','packet_loss','byte_count','stage_cycles','published','capture_time'):
            bad=copy.deepcopy(record)
            if mutation=='clock':bad['clk_hz']+=1000000
            elif mutation=='bbcrc':bad['pv_spi']['bb_crc'][3]^=1
            elif mutation=='packet_loss':bad['pv_spi']['lost']=1
            elif mutation=='byte_count':bad['pv_spi']['gcs_bytes']-=1
            elif mutation=='stage_cycles':bad['encoder_stage_cycles']['map']+=hz
            elif mutation=='published':bad['input_published_groups']+=1
            else:bad['cap_symbol_cursor']=4096
            assert stream_faults(bad,clock,ms,'offline-self-test'),mutation
    print(json.dumps(dict(self_test='passed',capture_halfwords=8192,finite_search_symbols=SEARCH_SYMBOLS,
                          prefill=prefill_reference(),bb_crc=dual_spi.first_crcs(gcs=True,rate=RATE),
                          table_crc=shaper_profile()[2],metadata_profiles=2,
                          negative_controls=['sample_corruption','missing_line','duplicate_line','truncated_capture',
                              'serial_crc','outside_start_window','cyclic_wrap','clock','BB_crc','packet_loss',
                              'byte_count','stage_accounting','publication','capture_time'])))


def main():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out',type=Path)
    p.add_argument('--ms',type=int,choices=(3000,60000),default=3000)
    p.add_argument('--clocks',type=int,nargs='+',choices=(128000,150000),default=[128000,150000])
    p.add_argument('--port');p.add_argument('--expected-git',help='require the exact firmware git string')
    p.add_argument('--build',type=Path,default=iq.ROOT/'firmware/build-limits',
                   help='directory containing the already-flashed UF2/ELF snapshot')
    p.add_argument('--source',type=Path,default=iq.ROOT/'firmware',
                   help='firmware source snapshot directory, containing src/ and CMakeLists.txt')
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args()
    if a.self_test:self_test();return
    if a.out is None:p.error('--out is required for hardware trials')
    a.out.mkdir(parents=True,exist_ok=False)
    source=a.source.resolve();build=a.build.resolve()
    firmware_files=[f for f in (source/'src').rglob('*') if f.suffix in ('.c','.h','.S')]
    firmware_files += [source/'CMakeLists.txt',source/'git_rev.cmake',source/'pico_sdk_import.cmake']
    files = [Path(__file__),iq.ROOT/'host/dual_spi.py',
              iq.ROOT/'host/iqbench.py',iq.ROOT/'host/dual_trial.py',iq.ROOT/'host/test_dvbs2_8psk.py',
              iq.ROOT/'host/test_iq8quad.py',iq.ROOT/'host/cm5/pv_spi_tx.py']
    files += list((iq.ROOT/'reference').glob('*.py'))+list((iq.ROOT/'reference/dvbs2').glob('*.py'))
    hashes={str(f.relative_to(iq.ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    hashes.update({'firmware/'+str(f.relative_to(source)):hashlib.sha256(f.read_bytes()).hexdigest()
                   for f in firmware_files})
    for name in ('iqbench.uf2','iqbench.elf'):
        binary=build/name;shutil.copy2(binary,a.out/name)
        hashes[name]=hashlib.sha256(binary.read_bytes()).hexdigest()
    def git(*args):return subprocess.check_output(['git',*args],cwd=iq.ROOT,text=True).strip()
    manifest=dict(started=dt.datetime.now().isoformat(),ms=a.ms,clocks_khz=a.clocks,
                  source_firmware_directory=str(source),build_directory=str(build),
                  expected_firmware_git=a.expected_git,head=git('rev-parse','HEAD'),branch=git('branch','--show-current'),
                  status=git('status','--short'),sha256=hashes,
                  setup='USB only; disconnected external GPIOs; stock voltage; on-chip PV-SPI source',
                  scope='Complete standalone normal5/6 8PSK+pilots, GCS/FEC/mapper/shaper/DMA/PIO digital trial; no AFE/RF/external-CM5 qualification',
                  provenance='Copies local source/model/build snapshots; source-to-flash identity requires explicit external flashing of this UF2',
                  capture='One bounded finite offset, all8192 contiguous bus halfwords, no cyclic wrap or per-frame padding')
    (a.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    # Compute stable mathematical expectations before opening the serial session.
    prefill_reference();shaper_profile();dual_spi.first_crcs(gcs=True,rate=RATE)
    iq.LOGGING=False;board=None;failures=[];fatal=None
    def record(label,result):
        row=dict(label=label,recorded_at=dt.datetime.now().isoformat(),**result)
        with (a.out/'runs.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
        print(json.dumps(row),flush=True)
    try:
        board=iq.Board(a.port)
        info,_=board.cmd('info');record('info',info)
        configured,_=board.cmd('bchkernel 2');record('bchkernel',configured)
        if configured.get('error') or configured.get('kernel')!=2:raise RuntimeError('BCH kernel selection failed')
        for clock in a.clocks:
            changed,_=board.cmd(f'clock {clock}');record(f'clock-{clock}',changed)
            if changed.get('clk_hz')!=clock*1000:raise RuntimeError('clock configuration failed')
            result,lines=board.cmd(f'bench8live {a.ms}',timeout=a.ms/1000+60)
            capture=a.out/f'capture-{clock}.txt';capture.write_text('\n'.join(lines)+'\n')
            if not result.get('error'):
                result.update(validate_capture(result,lines))
            faults=stream_faults(result,clock,a.ms,a.expected_git)
            result.update(faults=faults,classification='failed' if faults else 'clean',
                          capture_file=capture.name,host_crc_ok=not faults,
                          measured_scope='standalone digital GCS pipeline; instrumented work fractions, not OS idle')
            record(f'8psk-live-{clock}',result)
            if faults:failures.append(dict(clock_khz=clock,faults=faults))
    except Exception as exc:
        fatal=dict(exception=type(exc).__name__,detail=str(exc));record('host-exception',dict(fatal,faults=['host exception']))
    finally:
        if board is not None:
            try:board.cmd('clock 128000')
            except Exception as exc:
                failures.append(dict(cleanup='clock reset failed',detail=str(exc)))
            board.s.close()
        summary=dict(passed=not failures and fatal is None,failures=failures,fatal=fatal)
        (a.out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    if not summary['passed']:raise SystemExit(1)


if __name__=='__main__':main()
