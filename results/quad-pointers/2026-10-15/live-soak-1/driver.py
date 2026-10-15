"""L8 PIO-pointer -> FIFO sample-DMA live experiment; USB-only, GPIOs unwired.

Build/flash separately. Positive runs require every sparse observation to match one
fixed offset of the independent steady-state waveform. Fault1 freezes source grants
and must stop at the finite credited prefix; an absent negative capture is expected.
The public output backend's inherited DMA-abort wait has no separate timeout here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zlib

import iqbench as iq
from dual_trial import validate_capture
from pulse_profiles import L8_PROFILE

SEED, INPUT_WORDS, CAP_WORDS = 0x1234abcd, 8192, 4096
SOURCE_EMPTY, PIO_ERROR, ERR_POINTER = 8, 16, 1 << 16
OUTPUT_TXSTALL = 3


def validate_run(result: dict, lines: list[str], cpw: int, ms: int, fault: int,
                 clock_hz: int | None = None, expected_git: str | None = None) -> dict:
    """Fail closed on metadata, source-prefix ownership, errors and all captured samples."""
    r = result
    faults = []
    capture = {}
    try:
        if r.get('error') or r.get('cap_error'):
            faults.append(str(r.get('error') or r['cap_error']))
        expected = dict(cmd='quadlive', source=1, sps=4, L=8, row_profile=L8_PROFILE,
                        filter_beta=None, row_symbols=4, writers=2, schedule=0,
                        bus_priority_mask=0, cpw=cpw, divider256=256*cpw, ms=ms,
                        fault=fault, seed=SEED, in_words=INPUT_WORDS, table_bytes=65536,
                        table_crc=3433922394, restored=True, selectors_restored=True,
                        core1_idle=True, descriptor_gen_busy=0.0, sample_math_busy=0.0,
                        idle_wait='spin', poll_interval_us=24, read_beyond_grants=False,
                        pre_abort_input_read_addr_stable=True, pointer_other_pio_flags=0,
                        cap_stride=17, cap_divider256=256*cpw, cap_fifo_only=False)
        if clock_hz is not None:
            expected['clk_hz'] = clock_hz
        if expected_git is not None:
            expected['git'] = expected_git
        faults += [key for key, value in expected.items() if r.get(key) != value]
        clock = int(r['clk_hz'])
        if abs(float(r['sym_rate']) - clock/(8*cpw)) > 0.1:
            faults.append('symbol rate/divider')
        base, arena, origin = int(r['table_base']), int(r['ring_base']), int(r['input_base'])
        if base & 65535 or not 0x20000000 <= base <= 0x20070000:
            faults.append('table alignment/range')
        if r.get('ring_bytes') != 131072 or not arena <= base < base + 65536 <= arena + 131072:
            faults.append('table outside existing ring arena')
        if origin & 32767 or not 0x20000000 <= origin <= 0x20078000:
            faults.append('input ring alignment/range')
        words = iq.m.xorshift32(SEED, INPUT_WORDS).astype('<u4')
        input_crc = zlib.crc32(words.tobytes())
        if r.get('previous') != int(words[-1]) or r.get('input_crc') != input_crc or r.get('input_after_crc') != input_crc:
            faults.append('immutable PRBS ring/initial history')
        elapsed, cycles = int(r['elapsed_us']), int(r['supervision_cycles'])
        busy = cycles/(elapsed*clock/1e6) if elapsed > 0 else -1
        if not 0 <= busy <= 1 or any(abs(float(r[key])-busy) > 1e-6 for key in ('supervision_busy','core0_work_busy')):
            faults.append('supervision cycle fraction')
        if not 0 < int(r['polls']) or not 24 <= int(r['max_poll_gap_us']) <= 32:
            faults.append('supervision poll deadline')
        if not 0 < int(r['max_supervision_cycles']) <= cycles:
            faults.append('supervision work accounting')
        initial = list(map(int, r['start_input_issue_words']))
        live = list(map(int, r['input_issue_words']))
        final = list(map(int, r['final_input_issue_words']))
        pre_abort = list(map(int,r['pre_abort_input_issue_words']))
        addresses = list(map(int, r['pre_abort_input_read_addr']))
        reusable, published, granted = (int(r[key]) for key in ('reusable_words','published_words','granted_words'))
        if any(len(axis) != 2 for axis in (initial,live,final,pre_abort,addresses)):
            raise ValueError('two input axes required')
        if not (INPUT_WORDS <= published <= reusable + INPUT_WORDS and 0 < granted <= published and granted % 512 == 0):
            faults.append('published/granted/reusable bounds')
        if any(not 0 <= a <= b <= granted for a,b in zip(live,final)):
            faults.append('source issue beyond granted prefix')
        if any(not a <= b <= c <= b+8 for a,b,c in zip(live,pre_abort,final)):
            faults.append('source issue snapshots across abort')
        if reusable != max(min(final)-8,0):
            # The final READ_ADDR snapshot can be a few reads ahead of the producer's
            # last snapshot. Reuse must stay conservative and close to both axes.
            if not 0 <= max(min(final)-8,0)-reusable <= 8:
                faults.append('guarded source reuse bound')
        if any(address != origin + 2*axis + 4*(count & (INPUT_WORDS-1))
               for axis,(address,count) in enumerate(zip(addresses,pre_abort))):
            faults.append('pre-abort READ_ADDR/source issue correspondence')
        ptr, live_ptr, out, error = (int(r[key]) for key in ('pointer_errors','live_pointer_errors','output_errors','errors'))
        sm_mask = int(r['pointer_sm_mask'])
        if sm_mask.bit_count() != 2 or sm_mask & ~15 or any(int(r[key]) & ~sm_mask for key in ('pointer_txstall_mask','pointer_rxstall_mask')):
            faults.append('pointer SM flag ownership')
        if not fault:
            if ptr or live_ptr or out or error or r.get('txstalls_i') or r.get('txstalls_q') or r.get('pointer_txstall_mask') or r.get('null_trigger_mask'):
                faults.append('positive live hardware fault')
            if r.get('grants_frozen') or r.get('frozen_granted_words') != 0:
                faults.append('unexpected source grant freeze')
            if not ms*1000 <= elapsed <= ms*1000 + 1000:
                faults.append('positive run duration')
            if not r.get('capture_started') or r.get('capture_count') != CAP_WORDS:
                faults.append('positive capture incomplete')
            expected_reads = elapsed*clock/(1e6*8*cpw*16)
            if any(abs(b-a-expected_reads) > 32 + expected_reads*0.001 for a,b in zip(initial,final)):
                faults.append('source issue rate/duration')
        else:
            frozen = int(r['frozen_granted_words'])
            if not r.get('grants_frozen') or frozen != granted or any(count > frozen for count in final):
                faults.append('source read beyond frozen finite credit')
            allowed_ptr = SOURCE_EMPTY | (PIO_ERROR if r.get('pointer_txstall_mask') else 0)
            if not ptr & SOURCE_EMPTY or ptr & ~allowed_ptr or not live_ptr & SOURCE_EMPTY or live_ptr & ~allowed_ptr:
                faults.append('negative pointer fault scope')
            if not int(r.get('null_trigger_mask',0)) or out & ~OUTPUT_TXSTALL or error != (ERR_POINTER | out):
                faults.append('negative output/bench fault scope')
            if not ms*250 <= elapsed < ms*1000:
                faults.append('negative did not terminate after grant freeze')
        if r.get('cap_words') == CAP_WORDS:
            capture = validate_capture(r, lines, 1, CAP_WORDS)
            if not (capture.get('cap_transport_ok') and capture.get('cap_aligned') and
                    capture.get('cap_mismatches') == 0 and capture.get('cap_checked_bus_words') == 8192):
                faults.append(capture.get('cap_error') or 'sparse waveform mismatch')
        elif not fault or r.get('cap_words') != 0 or lines:
            faults.append('unexpected partial capture transport')
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        faults.append(str(error))
    return dict(capture, faults=list(dict.fromkeys(faults)),
                classification='failed' if faults else 'credit_starvation_detected' if fault else 'digital_live_clean')


def validate_clock(result: dict, khz: int, expected_git: str | None = None):
    if result.get('error') or result.get('cmd') != 'info' or result.get('clk_hz') != khz*1000:
        raise ValueError(f'clock command failed for{khz}kHz: {result}')
    if expected_git is not None and result.get('git') != expected_git:
        raise ValueError(f'firmware identity differs from expected{expected_git}: {result.get("git")}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--clocks', nargs='+', type=int, default=[128000])
    p.add_argument('--cpws', nargs='+', type=int, choices=(1,2), default=[2,1])
    p.add_argument('--faults', nargs='+', type=int, choices=(0,1), default=[0,1])
    p.add_argument('--ms', type=int, default=1000)
    p.add_argument('--reps', type=int, default=1)
    p.add_argument('--port')
    p.add_argument('--expected-git', help='Require this exact firmware git identity in clock and run responses')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build-limits')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    args = p.parse_args()
    if args.reps < 1 or not 100 <= args.ms <= 60000:
        p.error('--reps must be positive and --ms100..60000')
    args.out.mkdir(parents=True, exist_ok=False)
    build, source = args.build.resolve(), args.source.resolve()
    shutil.copy2(__file__, args.out/'driver.py')
    hashes = {}
    for name in ('iqbench.uf2','iqbench.elf'):
        shutil.copy2(build/name,args.out/name)
        hashes[name] = hashlib.sha256((args.out/name).read_bytes()).hexdigest()
    files = [f for f in (source/'src').glob('*') if f.suffix in ('.c','.h','.S')]
    files += [source/'CMakeLists.txt']
    hashes.update({'firmware/'+str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    for relative in ('host/quadlive_trial.py','host/dual_trial.py','host/iqbench.py','host/pulse_profiles.py',
                     'reference/iqlut.py','reference/gen_coeffs.py'):
        hashes[relative] = hashlib.sha256((iq.ROOT/relative).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(),clocks_khz=args.clocks,cpws=args.cpws,
                    faults=args.faults,reps=args.reps,ms=args.ms,expected_firmware_git=args.expected_git,
                    setup='USB only; external GPIOs unwired; digital output active during trial',
                    scope='Immutable cyclic PRBS source, L8 pointer and output DMA; Core1 idle, no FEC/SPI or RF/AFE qualification',
                    supervision='Cycle-accounted poll body; remaining core0 spin waits, not energy-idle CPU',
                    cleanup='Uses public output backend with inherited unbounded ABORT wait',
                    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=iq.ROOT,text=True).strip(),
                    source_firmware_directory=str(source),build_directory=str(build),sha256=hashes)
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        for clock in args.clocks:
            clock_result,_ = board.cmd(f'clock {clock}')
            with (args.out/'clocks.jsonl').open('a') as f:
                f.write(json.dumps(dict(requested_khz=clock,response=clock_result))+'\n')
            validate_clock(clock_result,clock,args.expected_git)
            for cpw in args.cpws:
                for fault in args.faults:
                    for rep in range(args.reps):
                        r, lines = board.cmd(f'quadlive {cpw} {args.ms} {fault}',timeout=args.ms/1000+20)
                        (args.out/f'capture-{clock}-{cpw}-{fault}-{rep}.txt').write_text('\n'.join(lines)+'\n')
                        r.update(validate_run(r,lines,cpw,args.ms,fault,clock*1000,args.expected_git))
                        r['rep'] = rep
                        with (args.out/'runs.jsonl').open('a') as f:
                            f.write(json.dumps(r)+'\n')
                        print(json.dumps(r),flush=True)
                        failed |= bool(r['faults'])
    finally:
        try:
            clock_result,_ = board.cmd('clock 128000')
            with (args.out/'clocks.jsonl').open('a') as f:
                f.write(json.dumps(dict(requested_khz=128000,cleanup=True,response=clock_result))+'\n')
            validate_clock(clock_result,128000,args.expected_git)
        finally:
            board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
