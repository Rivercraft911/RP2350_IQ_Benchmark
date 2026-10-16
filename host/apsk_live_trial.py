"""Runtime16/32APSK mapper/shaper with one-core and two-core schedules.

Build/flash separately. No FEC, SPI, AFE or RF qualification. Instrumented work
includes mapping, filter history per block, shaping and publication; polling
waits are excluded. Capture validation uses an independent native333 FIR.
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
import zlib

import numpy as np

import iqbench as iq
import iq32out_trial as reference
import iq16out_trial as ref16

SOURCE_SYMBOLS = 8192
BLOCK_SYMBOLS, OUT_BLOCKS, CAP_WORDS = 1024, 4, 4096
SCHEDULES = ('core0_map_shape', 'core1_map_core0_shape', 'alternate_map_shape')
LABEL_CRCS = {mod: zlib.crc32((iq.m.xorshift32(reference.SEED, SOURCE_SYMBOLS) & (mod-1)).astype('u1').tobytes())
              for mod in (16, 32)}
LABEL_CRC = LABEL_CRCS[32]


def selected_profiles(modulations, mappers, placements):
    return [(modulation, mapper, placement)
            for modulation in modulations for mapper in mappers for placement in placements
            if (modulation != 16 or mapper == 0)
            and (not placement or (modulation == 32 and mapper == 3))]


@functools.lru_cache(maxsize=2)
def reference_period(modulation):
    if modulation == 32:
        return reference.reference_period()
    words = ref16.source_words()[:SOURCE_SYMBOLS//4]
    q30 = ref16.levels(ref16.GAMMA)[1]
    scaled = (q30[:,None]*ref16.COEFFICIENTS[None,:]+(1<<29))>>30
    axes, _ = ref16.direct_output(words, words[-2:], scaled)
    i, q = (np.asarray(axis, np.int64)&0x3fff for axis in axes)
    bus = np.stack([i|0x4000,q],axis=1).ravel().astype('<u2')
    bus.flags.writeable = False
    return bus


def reference_window(cursor, search, modulation):
    if type(cursor) is not int or cursor <= 0 or search != reference.SEARCH_SYMBOLS:
        raise ValueError('invalid bounded capture cursor/search')
    lower, upper = max(0,cursor-search)*4, cursor*4
    period = reference_period(modulation)
    return lower, upper, period[np.arange(lower,upper+2*CAP_WORDS,dtype=np.int64)%len(period)]


def validate_capture(result, lines):
    try:
        if result.get('cap_error'): raise ValueError(str(result['cap_error']))
        got = reference.parse_capture(lines)
        if len(got) != CAP_WORDS or result.get('cap_words') != CAP_WORDS:
            raise ValueError('capture is incomplete')
        if zlib.crc32(got.tobytes()) != result.get('cap_crc') or result.get('cap_stride') != 1:
            raise ValueError('capture transport CRC/stride mismatch')
        modulation = int(result['modulation'].removesuffix('APSK'))
        if modulation not in (16,32): raise ValueError('invalid modulation')
        lower, upper, expected = reference_window(result['cap_symbol_cursor'], result['cap_search_symbols'], modulation)
    except (KeyError, ValueError, TypeError) as exc:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(exc))
    bus = got.view('<u2')
    candidates = np.flatnonzero(expected[:upper-lower] == bus[0])
    for j in range(1,32):
        candidates = candidates[expected[candidates+j] == bus[j]]
        if not len(candidates): break
    if not len(candidates):
        return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error='no bounded offset matches first32 observations')
    offset, bad = min(((int(o),np.flatnonzero(expected[o:o+len(bus)] != bus)) for o in candidates),
                      key=lambda item: len(item[1]))
    axis = ((bus>>14)&1).astype(np.uint8)
    physical = np.array_equal(axis,int(axis[0])^(np.arange(len(bus))&1)) and not np.any(bus&0x8000)
    return dict(cap_transport_ok=True, cap_aligned=bool(physical), cap_physical_iq_ok=bool(physical),
                cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                cap_checked_bus_words=len(bus), cap_global_bus_offset=lower+offset,
                cap_period_bus_offset=(lower+offset)%len(reference_period(modulation)),
                cap_start_search_bus_words=upper-lower)


def profile_fields(modulation, mapper, placement=0):
    source = ref16 if modulation == 16 else reference
    pair_bytes = 16384 if mapper else 0
    pair_crc = 3614349055 if mapper == 3 else 2680011457 if mapper else 0
    return dict(modulation=f'{modulation}APSK',mapper=mapper,placement=placement,
                code_placement='per_core_sram' if placement else 'flash_cached',mapper_code_placement='flash_cached',
                input_format='native_packed_coordinates' if modulation == 16 else 'packed_radix17_byte_offsets' if mapper == 3 else 'rolling_radix17_triples',
                mapper_table_bytes=pair_bytes,
                mapper_table_crc=pair_crc, mapper_table_after_crc=pair_crc,
                rate_index=3 if modulation == 16 else 2, gamma100=270 if modulation == 16 else 0,
                gamma1_100=264 if modulation == 32 else 0, gamma2_100=464 if modulation == 32 else 0,
                row_profile=source.PROFILE, pulse_profile=source.PULSE,
                coefficient_sha256=source.COEFFICIENT_SHA, table_bytes=139264 if modulation == 16 else 117912,
                label_crc=LABEL_CRCS[modulation], label_after_crc=LABEL_CRCS[modulation],
                key_prefix_symbols=8 if modulation == 16 else 5,
                key_bytes_per_symbol=1 if modulation == 16 else 4,
                **dict(zip(('old_crc','middle_crc','future_crc'),source.TABLE_CRCS)))


def stream_faults(r, clock, ms, cpw, schedule, expected_git=None, mapper=0, modulation=32, placement=0):
    if r.get('error'):
        return [f"firmware error: {r['error']}"]
    expected = dict(cmd='benchapsklive', clk_hz=clock*1000, ms=ms, cpw=cpw, schedule=schedule,
                    L=8, sps=2, outer_radius=1, source_symbols=SOURCE_SYMBOLS, seed=reference.SEED,
                    history='cyclic_source_tail',runtime_encode_included=True,
                    idle_wait='timer_poll_1us', interrupts_enabled=True, prefill_blocks=OUT_BLOCKS,
                    block_symbols=BLOCK_SYMBOLS, out_blocks=OUT_BLOCKS, out_words=2048,
                    cap_words=CAP_WORDS, cap_stride=1, cap_search_symbols=reference.SEARCH_SYMBOLS,
                    **profile_fields(modulation,mapper,placement))
    if expected_git is not None:
        expected['git'] = expected_git
    faults = [f'{key}={r.get(key)!r}, expected {want!r}' for key, want in expected.items() if r.get(key) != want]
    faults += [key for key in ('errors', 'underruns', 'own_errors', 'idle_sent', 'txstalls')
               if type(r.get(key)) is not int or r[key] != 0]
    try:
        hz, elapsed, blocks = clock*1000, r['elapsed_us'], r['blocks']
        rs = hz / (4*cpw)
        if not (ms*1000 <= elapsed <= ms*1000+3000 and blocks > 0):
            faults.append('duration or generated runtime blocks invalid')
        if not math.isclose(r['sym_rate'], rs, abs_tol=.11):
            faults.append('symbol rate mismatch')
        if r['generated_blocks'] != blocks+OUT_BLOCKS or not 0 <= r['generated_blocks']-r['done_blocks'] <= OUT_BLOCKS:
            faults.append('output block accounting/credit mismatch')
        if abs(r['done_blocks']*BLOCK_SYMBOLS-rs*elapsed/1e6) > 3*BLOCK_SYMBOLS:
            faults.append('DMA block count differs from elapsed output rate')
        cores = r['cores']
        if len(cores) != 2 or [c['core'] for c in cores] != [0, 1]:
            faults.append('invalid per-core records')
        if sum(c['blocks'] for c in cores) != blocks or sum(c['shape_calls'] for c in cores) != blocks:
            faults.append('per-core block accounting mismatch')
        for core in cores:
            address = core['shaper_address']
            lower,upper = ((0x20081000,0x20081800) if core['core'] == 0 else (0x20080000,0x20080800)) if placement else (0x10000000,0x11000000)
            if type(address) is not int or not address&1 or not lower <= (address&~1) < upper:
                faults.append('shaper address differs from declared code placement')
            if not math.isclose(core['work_cycles']/(elapsed*hz/1e6), core['work_busy'], abs_tol=2e-6):
                faults.append('per-core work fraction mismatch')
            if not 0 <= core['work_busy'] <= 1.001:
                faults.append('per-core work exceeds available clock cycles')
            if core['work_cycles'] < core['map_cycles'] + core['shape_cycles']:
                faults.append('work smaller than included map/shape intervals')
            for stage in ('map', 'shape'):
                calls, cyc = core[f'{stage}_calls'], core[f'{stage}_cycles']
                want = cyc/(calls*BLOCK_SYMBOLS) if calls else 0
                if not math.isclose(want, core[f'{stage}_cyc_per_sym'], abs_tol=.00011):
                    faults.append(f'{stage} cycles/symbol accounting mismatch')
                if core[f'worst_{stage}_cyc_per_sym']+.0002 < want:
                    faults.append(f'{stage} worst block smaller than average')
                if not calls and cyc:
                    faults.append(f'{stage} cycles without a call')
        if schedule == 0:
            if cores[0]['map_calls'] != blocks or any(cores[1][key] for key in
                    ('blocks', 'map_calls', 'shape_calls', 'work_cycles')):
                faults.append('single-core schedule accounting mismatch')
        elif schedule == 1:
            if (cores[0]['map_calls'] or cores[1]['shape_calls'] or cores[1]['blocks'] or
                    not 0 <= cores[1]['map_calls']-blocks <= OUT_BLOCKS):
                faults.append('pipeline schedule accounting mismatch')
        else:
            if any(c['map_calls'] != c['shape_calls'] or c['shape_calls'] != c['blocks'] for c in cores):
                faults.append('alternating schedule accounting mismatch')
            if abs(cores[0]['blocks']-cores[1]['blocks']) > 2:
                faults.append('alternating producers are unbalanced')
        cursor = r['cap_symbol_cursor']
        if cursor % BLOCK_SYMBOLS or not 0 <= cursor//BLOCK_SYMBOLS-r['cap_done_blocks'] <= OUT_BLOCKS:
            faults.append('capture cursor/credit mismatch')
        if abs(cursor-rs*ms/2000) > 2*OUT_BLOCKS*BLOCK_SYMBOLS:
            faults.append('capture time differs from midpoint')
        if not (r.get('cap_transport_ok') and r.get('cap_aligned') and r.get('cap_mismatches') == 0
                and r.get('cap_checked_bus_words') == 2*CAP_WORDS):
            faults.append(r.get('cap_error') or 'complete capture mismatch')
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        faults.append(f'invalid runtime metadata: {type(exc).__name__}')
    return list(dict.fromkeys(faults))


def self_test():
    assert selected_profiles([16], [3], [0]) == []
    assert selected_profiles([16], [0], [1]) == []
    assert selected_profiles([32], [0, 1, 2], [1]) == []
    assert selected_profiles([16, 32], range(4), [0, 1]) == [
        (16, 0, 0), (32, 0, 0), (32, 1, 0), (32, 2, 0), (32, 3, 0), (32, 3, 1)]
    reference.self_test()
    cases, capture_cases = 0, 0
    for modulation,mapper,placement in ((16,0,0),(32,0,0),(32,1,0),(32,2,0),(32,3,0),(32,3,1)):
        # All observed values, malformed transport, a changed sample and an
        # outside-window capture are checked separately for each FIR period.
        cursor = 17*BLOCK_SYMBOLS
        lower, _, bus = reference_window(cursor, reference.SEARCH_SYMBOLS, modulation)
        cap, lines = reference.capture_lines(bus[100:100+8192])
        cap.update(modulation=f'{modulation}APSK', cap_symbol_cursor=cursor, cap_search_symbols=reference.SEARCH_SYMBOLS)
        assert validate_capture(cap,lines)['cap_mismatches'] == 0
        assert not validate_capture(cap,lines[1:])['cap_transport_ok']
        assert not validate_capture(dict(cap,cap_crc=cap['cap_crc']^1),lines)['cap_transport_ok']
        bad = bus[100:100+8192].copy(); bad[5000] ^= 1
        br, bl = reference.capture_lines(bad); br.update(modulation=cap['modulation'],cap_symbol_cursor=cursor,cap_search_symbols=reference.SEARCH_SYMBOLS)
        assert validate_capture(br,bl)['cap_mismatches'] == 1
        outside = reference_period(modulation)[(np.arange(8192)+lower-100)%len(reference_period(modulation))]
        br,bl = reference.capture_lines(outside); br.update(modulation=cap['modulation'],cap_symbol_cursor=cursor,cap_search_symbols=reference.SEARCH_SYMBOLS)
        assert not validate_capture(br,bl)['cap_aligned']
        capture_cases += 1
        for clock in (128000,150000):
            hz, ms = clock*1000, 3000
            for cpw in range(3,9):
                rs = hz/(4*cpw); done = int(rs*ms/1000)//BLOCK_SYMBOLS; blocks = done-1
                cursor = (int(rs*ms/2000)//BLOCK_SYMBOLS+3)*BLOCK_SYMBOLS
                _,_,bus = reference_window(cursor,reference.SEARCH_SYMBOLS,modulation)
                cap,lines = reference.capture_lines(bus[100:100+8192])
                for schedule in range(3):
                    core_blocks = [blocks,0] if schedule < 2 else [(blocks+1)//2,blocks//2]
                    cores = []
                    for core,count in enumerate(core_blocks):
                        maps = count if schedule != 1 else (0 if core == 0 else blocks+2)
                        shapes = count; mc,sc = maps*BLOCK_SYMBOLS*8,shapes*BLOCK_SYMBOLS*20
                        work = mc+sc+(maps+shapes)*16
                        address = (0x20081531 if core == 0 else 0x20080745) if placement else 0x10020001
                        cores.append(dict(core=core,shaper_address=address,blocks=count,map_calls=maps,shape_calls=shapes,
                                          map_cycles=mc,shape_cycles=sc,work_cycles=work,
                                          work_busy=work/(ms*hz/1000),map_cyc_per_sym=8 if maps else 0,
                                          shape_cyc_per_sym=20 if shapes else 0,
                                          worst_map_cyc_per_sym=8 if maps else 0,worst_shape_cyc_per_sym=20 if shapes else 0))
                    r = dict(cmd='benchapsklive',git='self-test',clk_hz=hz,ms=ms,cpw=cpw,schedule=schedule,
                             L=8,sps=2,outer_radius=1,source_symbols=SOURCE_SYMBOLS,seed=reference.SEED,
                             history='cyclic_source_tail',runtime_encode_included=True,
                             idle_wait='timer_poll_1us',interrupts_enabled=True,prefill_blocks=OUT_BLOCKS,
                             block_symbols=BLOCK_SYMBOLS,out_blocks=OUT_BLOCKS,out_words=2048,
                             cap_search_symbols=reference.SEARCH_SYMBOLS,sym_rate=rs,elapsed_us=ms*1000,
                             blocks=blocks,generated_blocks=blocks+OUT_BLOCKS,done_blocks=done,cores=cores,
                             cap_symbol_cursor=cursor,cap_done_blocks=cursor//BLOCK_SYMBOLS-3,
                             errors=0,underruns=0,own_errors=0,idle_sent=0,txstalls=0,
                             **profile_fields(modulation,mapper,placement),**cap)
                    r.update(validate_capture(r,lines))
                    check = lambda row: stream_faults(row,clock,ms,cpw,schedule,'self-test',mapper,modulation,placement)
                    if max(c['work_busy'] for c in cores) <= 1.001:
                        assert not check(r),check(r)
                        for field in ('label_after_crc','mapper_table_after_crc','mapper','schedule','blocks','errors','cap_mismatches','cap_symbol_cursor'):
                            bad = copy.deepcopy(r); bad[field] += 1
                            assert check(bad),field
                        for field in ('shape_calls','work_cycles','map_cycles','shaper_address'):
                            bad = copy.deepcopy(r); bad['cores'][0][field] += hz if field.endswith('cycles') else 1
                            assert check(bad),field
                        bad = copy.deepcopy(r); del bad['errors']; assert check(bad)
                        cases += 1
                    else: assert check(r)
    print(json.dumps(dict(self_test='passed',schedule_metadata_cases=cases,capture_profiles=capture_cases,
                          label_crcs=LABEL_CRCS,period_bus_crcs={mod:zlib.crc32(reference_period(mod).tobytes()) for mod in (16,32)},
                          independent_fir_capture_halfwords=8192)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test', action='store_true')
    p.add_argument('--out', type=Path)
    p.add_argument('--ms', type=int, choices=(100, 3000, 60000), default=3000)
    p.add_argument('--clocks', type=int, nargs='+', choices=(128000, 150000), default=[128000, 150000])
    p.add_argument('--cpw', type=int, nargs='+', choices=range(3, 9), default=[4, 5, 6, 8])
    p.add_argument('--modulations',type=int,nargs='+',choices=(16,32),default=[32])
    p.add_argument('--mappers',type=int,nargs='+',choices=range(4),default=[0])
    p.add_argument('--placements',type=int,nargs='+',choices=(0,1),default=[0])
    p.add_argument('--schedules', type=int, nargs='+', choices=range(3), default=[0, 1, 2])
    p.add_argument('--expect-deadline-fault', action='store_true')
    p.add_argument('--port'); p.add_argument('--expected-git')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build-limits')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    a = p.parse_args()
    if a.self_test: self_test(); return
    if a.out is None: p.error('--out is required')
    profiles = selected_profiles(a.modulations, a.mappers, a.placements)
    if not profiles: p.error('no compatible modulation/mapper/placement combination selected')
    a.out.mkdir(parents=True, exist_ok=False)
    files = [f for f in (a.source/'src').rglob('*') if f.is_file()]+[a.source/'CMakeLists.txt']
    hashes = {'firmware/'+str(f.relative_to(a.source)): hashlib.sha256(f.read_bytes()).hexdigest()
              for f in files if f.is_file()}
    for f in (Path(__file__), iq.ROOT/'host/iqbench.py', iq.ROOT/'host/dual_trial.py',
              iq.ROOT/'host/iq32out_trial.py', iq.ROOT/'host/test_iq32native8.py',
              iq.ROOT/'host/test_iq32native8_asm.py', iq.ROOT/'host/test_iq32mapkeys.py',
              iq.ROOT/'host/test_apsk_live.py',iq.ROOT/'host/test_iq32mapkeys_pair.py',
              iq.ROOT/'host/test_iq32offsets.py',
              iq.ROOT/'host/iq16out_trial.py', iq.ROOT/'host/test_iq16native8.py',
              iq.ROOT/'host/test_iq16chunk.py', iq.ROOT/'reference/iqlut.py',
              iq.ROOT/'reference/gen_coeffs.py'):
        hashes[str(f.relative_to(iq.ROOT))] = hashlib.sha256(f.read_bytes()).hexdigest()
    for name in ('iqbench.uf2', 'iqbench.elf'):
        f = a.build/name; shutil.copy2(f, a.out/name); hashes[name] = hashlib.sha256(f.read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=a.ms, clocks_khz=a.clocks,
                    cpw=a.cpw, schedules=a.schedules, mappers=a.mappers,modulations=a.modulations,placements=a.placements,expect_deadline_fault=a.expect_deadline_fault,
                    expected_firmware_git=a.expected_git, head=iq.git_rev(), sha256=hashes,
                    source_firmware_directory=str(a.source.resolve()), build_directory=str(a.build.resolve()),
                    setup='USB only; GPIOs unconnected; stock voltage; cyclic16APSK bit labels /32APSK geometric labels',
                    scope='Runtime label/key mapping, L8/N2 native344 or native333 shaper, DMA/PIO GPIO output; no FEC/SPI/AFE/RF',
                    provenance='UF2 archived; flashing external; verify source-to-flash identity separately',
                    cpu='Per-core work intervals include mapping/filter history keys, shaping, publication; polling waits excluded',
                    placement='Shaper entry/ASM body location; short C history prefix and mapper remain in cached flash',
                    capture='One bounded source-cursor offset for all8192 contiguous halfwords; cyclic tail FIR history')
    (a.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    for modulation in a.modulations: reference_period(modulation)
    iq.LOGGING = False
    board = None; failures = []; fatal = None
    def record(label, result):
        row = dict(label=label, recorded_at=dt.datetime.now().isoformat(), **result)
        with (a.out/'runs.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
        print(json.dumps(row), flush=True)
    try:
        board = iq.Board(a.port)
        info, _ = board.cmd('info'); record('info', info)
        for clock in a.clocks:
            changed, _ = board.cmd(f'clock {clock}'); record(f'clock-{clock}', changed)
            if changed.get('clk_hz') != clock*1000: raise RuntimeError('clock configuration failed')
            for modulation, mapper, placement in profiles:
                for schedule in a.schedules:
                    for cpw in a.cpw:
                        result, lines = board.cmd(f'benchapsklive {cpw} {a.ms} {schedule} {mapper} {modulation} {placement}', timeout=a.ms/1000+60)
                        name = f'capture-{modulation}-{clock}-m{mapper}-p{placement}-s{schedule}-cpw{cpw}.txt'
                        (a.out/name).write_text('\n'.join(lines)+'\n')
                        if not result.get('error') and result.get('cap_words'):
                            result.update(validate_capture(result, lines))
                        faults = stream_faults(result, clock, a.ms, cpw, schedule, a.expected_git,mapper,modulation,placement)
                        if a.expect_deadline_fault:
                            passed = (not result.get('error') and bool(result.get('errors', 0) & 7) and
                                      bool(result.get('underruns') or result.get('own_errors') or result.get('txstalls')))
                            passed &= result.get('clk_hz') == clock*1000 and result.get('cpw') == cpw and result.get('schedule') == schedule and result.get('mapper') == mapper and result.get('placement') == placement and result.get('modulation') == f'{modulation}APSK'
                            if a.expected_git is not None: passed &= result.get('git') == a.expected_git
                            classification = 'expected-deadline-fault' if passed else 'negative-control-failed'
                        else:
                            passed = not faults; classification = 'clean' if passed else 'failed'
                        result.update(faults=faults, classification=classification, capture_file=name, passed=bool(passed))
                        record(f'{modulation}apsk-live-{clock}-m{mapper}-p{placement}-s{schedule}-cpw{cpw}', result)
                        if not passed: failures.append(dict(modulation=modulation,mapper=mapper,placement=placement,clock_khz=clock,schedule=schedule,cpw=cpw,faults=faults))
    except Exception as exc:
        fatal = dict(exception=type(exc).__name__, detail=str(exc)); record('host-exception', fatal)
    finally:
        if board is not None:
            try: board.cmd('clock 128000')
            except Exception as exc: failures.append(dict(cleanup='clock reset failed', detail=str(exc)))
            board.s.close()
        summary = dict(passed=not failures and fatal is None, failures=failures, fatal=fatal)
        (a.out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    if not summary['passed']: raise SystemExit(1)


if __name__ == '__main__': main()
