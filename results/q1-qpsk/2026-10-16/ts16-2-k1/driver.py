"""Live SPI/FEC -> hardware QPSK pointer trial; build and flash separately.

This runner opens the board only when explicitly invoked. Positive DDR runs check
all 4096 sparse GPIO snapshots against the independent TS/GCS reference and clock
parity. Inverted-clock and finite-credit controls must detect their injected cause.
The optional control uses the existing four-writer CPU descriptor pipeline.
Use --writers 4 for the alternating-DMA four-writer pointer experiment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

import iqbench as iq
from dual_spi import QPSK_RATES, packet_faults, qpsk_config
from dual_trial import DAC_CLOCK_NAMES, stream_faults, validate_capture
from pulse_profiles import L8_PROFILE
from quadlive_trial import poll_limits, validate_clock

INPUT_WORDS, CAP_WORDS = 8192, 4096
SOURCE_EMPTY, PIO_ERROR, ERR_POINTER, OUTPUT_TXSTALL = 8, 16, 1 << 19, 3
POINTER_CODES = (5, 8, 9, 10)


def validate_run(result: dict, lines: list[str], cpw: int, ms: int, source: int,
                 fault: int = 0, clock_mode: int = 1, clock_hz: int | None = None,
                 expected_git: str | None = None, code_index: int = 8,
                 bch_kernel: int = 2, bch_bank: int = 1, priority: int = 0,
                 writers: int = 2, pv_kernel: int = 0) -> dict:
    """Fail closed on configuration, ownership, SPI provenance and every snapshot."""
    r, faults, capture = result, [], {}
    try:
        if r.get('error') or r.get('cap_error'):
            faults.append(str(r.get('error') or r['cap_error']))
        # Older images omit the TS adapter fields; that is only acceptable for the default.
        if r.get('pv_kernel', 0) != pv_kernel or pv_kernel not in (0, 1, 2, 3) or (pv_kernel and source != 5):
            faults.append('pv_kernel')
        leaf = int(r.get('pv_leaf_base', 0))
        ingress = float(r.get('core0_ingress_busy', 0.0))
        if not math.isfinite(ingress) or not 0 <= ingress <= 1:
            faults.append('core0_ingress_busy')
        if pv_kernel == 3:
            # Core 0 must have built the runtime BBFRAMEs and core 1 none of them.
            if (r.get('ingress_core') != 0 or not ingress > 0 or not int(r.get('ingress_steps', 0)) > 0 or
                    r.get('ingress_step_packets') != 4 or r.get('pv_spi', {}).get('ts_busy') != 0):
                faults.append('core-0 TS ingress')
        elif r.get('ingress_core', 1) != 1 or ingress or r.get('ingress_steps', 0):
            faults.append('core-0 TS ingress')
        if pv_kernel in (1, 3):
            table, origin = int(r.get('table_base', 0)), int(r.get('input_base', 0))
            if (leaf & 3 or not 0x20000000 <= leaf < 0x20080000 or table <= leaf < table + 65536 or
                    origin <= leaf < origin + 32768):
                faults.append('TS word leaf placement')
        elif leaf:
            faults.append('TS word leaf placement')
        expected = dict(cmd='dualptr4' if writers == 4 else 'dualptr', source=source,
                        cpw=cpw, divider256=256*cpw,
                        ms=ms, fault=fault, sps=4, L=8, row_symbols=4, writers=writers,
                        row_profile=L8_PROFILE, filter_beta=None, schedule=0,
                        bus_priority_mask=priority, bch_kernel=bch_kernel, bch_bank=bch_bank,
                        map_kernel=1, ldpc_pack=1,
                        frame_parallel=False, descriptor_gen_busy=0.0,
                        sample_math_busy=0.0, in_words=INPUT_WORDS, table_bytes=65536,
                        table_crc=3433922394, restored=True, selectors_restored=True,
                        poll_interval_us=128, max_poll_interval_us=256,
                        read_beyond_grants=False, pointer_other_pio_flags=0,
                        cap_stride=17, cap_divider256=256*cpw, cap_fifo_only=False,
                        cap_format='pins32', dac_clock_pio=0 if writers == 4 else 2,
                        dac_clock=DAC_CLOCK_NAMES['ddr' if clock_mode == 1 else 'inverted'])
        expected.update(qpsk_config(code_index))
        if writers == 4:
            expected.update(pointer_pio=0, pointer_routing='alternating_dma')
        if (cpw not in (1, 2) or source not in (5, 6) or fault not in (0, 1) or clock_mode not in (1, 2) or
                code_index not in POINTER_CODES or bch_kernel not in (2, 3) or bch_bank not in (0, 1) or
                writers not in (2, 4)):
            faults.append('unsupported requested configuration')
        if fault and clock_mode != 1:
            faults.append('grant-freeze and inverted-clock controls must run separately')
        if clock_hz is not None:
            expected['clk_hz'] = clock_hz
        if expected_git is not None:
            expected['git'] = expected_git
        faults += [key for key, value in expected.items() if r.get(key) != value]
        clock, elapsed = int(r['clk_hz']), int(r['elapsed_us'])
        rate = float(r['sym_rate'])
        if not math.isfinite(rate) or abs(rate-clock/(8*cpw)) > 0.1:
            faults.append('symbol rate/divider')
        for key in ('core0_work_busy', 'supervision_busy', 'split_bch_busy', 'core1_work_busy'):
            value = float(r[key])
            if not math.isfinite(value) or not 0 <= value <= 1:
                faults.append(key)
        if float(r['supervision_busy']) > float(r['core0_work_busy']) + 0.0002:
            faults.append('supervision exceeds core 0 work')
        if float(r['split_bch_busy']) > float(r['core0_work_busy']) + 0.0002:
            faults.append('frontend exceeds core 0 work')
        if abs(float(r['core0_work_busy'])-float(r['supervision_busy'])-float(r['split_bch_busy'])-ingress) > 0.000004:
            faults.append('core 0 work accounting')
        budget, deadline = poll_limits(clock)
        if (r.get('required_credit_runway_words') != 2*budget or
                r.get('source_cursor_deadline_us') != deadline or
                (not fault and int(r['min_credit_runway_words']) < 2*budget) or
                not 0 < int(r['max_poll_gap_us']) <= 256):
            faults.append('source cursor/credit runway guard')
        base, origin = int(r['table_base']), int(r['input_base'])
        if base & 65535 or not 0x20000000 <= base <= 0x20070000:
            faults.append('table alignment/range')
        if origin & 32767 or not 0x20000000 <= origin <= 0x20078000:
            faults.append('input ring alignment/range')
        if not (origin+32768 <= base or base+65536 <= origin):
            faults.append('source ring overlaps shaping table')
        issued = list(map(int, r['input_issue_words']))
        reusable, published, granted = (int(r[key]) for key in
                                       ('reusable_words', 'published_words', 'granted_words'))
        if len(issued) != 2:
            raise ValueError('two input axes required')
        if not (0 <= reusable <= min(issued) and 0 <= published <= reusable+INPUT_WORDS and
                0 < granted <= published and granted % 512 == 0 and
                all(0 <= count <= granted for count in issued)):
            faults.append('published/granted/reusable ownership bounds')
        sm_mask = int(r['pointer_sm_mask'])
        txstall, rxstall = int(r['pointer_txstall_mask']), int(r['pointer_rxstall_mask'])
        if sm_mask.bit_count() != 2 or sm_mask & ~15 or (txstall | rxstall) & ~sm_mask:
            faults.append('pointer SM fault ownership')
        clock_sm = int(r['dac_clock_sm'])
        if not 0 <= clock_sm < 4 or sm_mask & (1 << clock_sm):
            faults.append('DDR clock SM ownership')
        ptr, out, errors = (int(r[key]) for key in ('pointer_errors', 'output_errors', 'errors'))
        for name in ('underruns', 'own_errors', 'input_overruns'):
            if r.get(name):
                faults.append(name)
        if not r.get('s2_runtime_frames') or not r.get('split_bch_frames'):
            faults.append('no runtime FEC/frontend frame')
        faults.extend(packet_faults(r))
        if not fault:
            if ptr or out or errors or txstall or r.get('txstalls_i') or r.get('txstalls_q') or r.get('null_trigger_mask'):
                faults.append('positive hardware fault')
            if r.get('grants_frozen') or r.get('frozen_granted_words') != 0:
                faults.append('unexpected grant freeze')
            if not ms*1000 <= elapsed <= ms*1000+1000:
                faults.append('positive run duration')
            if not r.get('capture_started') or r.get('capture_count') != CAP_WORDS:
                faults.append('capture did not complete')
        else:
            allowed = SOURCE_EMPTY | (PIO_ERROR if txstall else 0)
            if not r.get('grants_frozen') or int(r['frozen_granted_words']) != granted:
                faults.append('finite grant freeze missing')
            if not ptr & SOURCE_EMPTY or ptr & ~allowed or out & ~OUTPUT_TXSTALL or errors != ERR_POINTER | out:
                faults.append('grant-freeze fault scope')
            if not r.get('null_trigger_mask') or not ms*250 <= elapsed < ms*1000:
                faults.append('grant-freeze did not stop at finite prefix')
        if r.get('cap_words') == CAP_WORDS:
            capture = validate_capture(r, lines, source, CAP_WORDS)
            if not (capture.get('cap_transport_ok') and capture.get('cap_aligned') and
                    capture.get('cap_mismatches') == 0 and capture.get('cap_checked_bus_words') == CAP_WORDS):
                faults.append(capture.get('cap_error') or 'sparse waveform mismatch')
            checked = capture.get('cap_clock_checked', 0)
            if clock_mode == 1:
                if not capture.get('cap_clock_ok'):
                    faults.append('DDR clock parity')
            elif checked != CAP_WORDS or capture.get('cap_clock_mismatches') != checked:
                faults.append('inverted DDR clock cause not detected')
        elif not fault or r.get('cap_words') != 0 or lines or r.get('capture_started') or r.get('capture_count'):
            faults.append('unexpected partial capture transport')
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        faults.append(str(error))
    faults = list(dict.fromkeys(faults))
    classification = ('failed' if faults else 'credit_starvation_detected' if fault else
                      'inverted_clock_detected' if clock_mode == 2 else 'digital_live_clean')
    return dict(capture, faults=faults, classification=classification,
                negative_control_detected=not faults and bool(fault or clock_mode == 2))


def validate_control(result: dict, lines: list[str], cpw: int, ms: int, source: int,
                     clock_hz: int, expected_git: str | None = None, code_index: int = 8,
                     bch_kernel: int = 2, ldpc_pack: int | None = None,
                     bch_bank: int | None = None) -> dict:
    """The existing optimized four-writer pipeline at the same clock and source."""
    r = dict(result)
    capture = validate_capture(r, lines, source, CAP_WORDS)
    r.update(capture)
    faults = stream_faults(r, True, source)
    expected = dict(cmd='dualstream', cpw=cpw, ms=ms, source=source, clk_hz=clock_hz,
                    divider256=256*cpw, row_symbols=4, row_profile=L8_PROFILE,
                    writers=4, map_kernel=1, bch_kernel=bch_kernel,
                    cap_format='pins32', dac_clock=DAC_CLOCK_NAMES['ddr'])
    expected.update(qpsk_config(code_index))
    if ldpc_pack is not None:
        expected['ldpc_pack'] = ldpc_pack
    if bch_bank is not None:
        expected['bch_bank'] = bch_bank
    if expected_git is not None:
        expected['git'] = expected_git
    faults += [key for key, value in expected.items() if r.get(key) != value]
    if r.get('elapsed_us', 0) < ms*1000:
        faults.append('control ended early')
    return dict(capture, faults=list(dict.fromkeys(faults)),
                classification='failed' if faults else 'cpu_descriptor_control_clean')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--clocks', nargs='+', type=int, default=[128000])
    p.add_argument('--cpws', nargs='+', type=int, choices=(1, 2), default=[1])
    p.add_argument('--sources', nargs='+', type=int, choices=(5, 6), default=[6])
    p.add_argument('--faults', nargs='+', type=int, choices=(0, 1), default=[0])
    p.add_argument('--clock-modes', nargs='+', type=int, choices=(1, 2), default=[1])
    p.add_argument('--writers', type=int, choices=(2, 4), default=2,
                   help='Pointer-fed writers: 2 uses dualptr; 4 uses dualptr4')
    p.add_argument('--control', action='store_true', help='Also run the existing four-writer pipeline')
    p.add_argument('--priority', type=int, choices=range(16), default=0,
                   help='dualprio bus-priority mask for every run (register fields PROC0/PROC1/DMA_R/DMA_W)')
    p.add_argument('--code-index', type=int, choices=POINTER_CODES, default=8)
    p.add_argument('--bch-kernel', type=int, choices=(2, 3), default=2)
    p.add_argument('--bch-bank', type=int, choices=(0, 1), default=1)
    p.add_argument('--pv-kernel', type=int, choices=(0, 1, 2, 3), default=0,
                   help='TS adapter: 0 existing, 1 fused CRC-8/word leaf in SRAM, 2 same in C from flash, '
                        '3 as 1 with core 0 building the BBFRAMEs (source 5 only)')
    p.add_argument('--ms', type=int, default=3000)
    p.add_argument('--reps', type=int, default=1)
    p.add_argument('--port')
    p.add_argument('--expected-git', help='Require exact firmware git identity')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    args = p.parse_args()
    if args.reps < 1 or not 100 <= args.ms <= 60000 or any(clock <= 0 for clock in args.clocks):
        p.error('positive repetitions/clocks and 100 <= --ms <= 60000 required')
    if 1 in args.faults and 1 not in args.clock_modes:
        p.error('grant-freeze requires --clock-modes 1')
    if args.pv_kernel and args.sources != [5]:
        p.error('--pv-kernel 1-3 selects the TS adapter: use --sources 5')
    args.out.mkdir(parents=True, exist_ok=False)
    build, source = args.build.resolve(), args.source.resolve()
    shutil.copy2(__file__, args.out/'driver.py')
    hashes = {}
    for name in ('iqbench.uf2', 'iqbench.elf'):
        shutil.copy2(build/name, args.out/name)
        hashes[name] = hashlib.sha256((build/name).read_bytes()).hexdigest()
    files = [f for f in (source/'src').glob('*') if f.suffix in ('.c', '.h', '.S')]
    files += [source/'CMakeLists.txt']
    hashes.update({'firmware/'+str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    for relative in ('host/dualptr_trial.py', 'host/dual_trial.py', 'host/dual_spi.py',
                     'host/quadlive_trial.py', 'host/iqbench.py', 'host/pulse_profiles.py',
                     'host/cm5/pv_spi_tx.py', 'reference/dvbs2/dvbs2.py',
                     'reference/iqlut.py', 'reference/gen_coeffs.py'):
        hashes[relative] = hashlib.sha256((iq.ROOT/relative).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), clocks_khz=args.clocks,
                    cpws=args.cpws, sources=args.sources, faults=args.faults,
                    clock_modes=args.clock_modes, writers=args.writers, priority=args.priority,
                    control=args.control, reps=args.reps,
                    code_index=args.code_index, bch_kernel=args.bch_kernel, ldpc_pack=1,
                    bch_bank=args.bch_bank, pv_kernel=args.pv_kernel,
                    ms=args.ms, expected_firmware_git=args.expected_git,
                    setup='USB only; external GPIOs unwired; on-chip PV-SPI source; digital output active',
                    scope=f'Live QPSK {QPSK_RATES[args.code_index]} L8/N4 pointer/output pipeline; '
                          'split FEC, no frame-parallel or RF claim',
                    capture='All4096 sparse pins32 observations, one fixed waveform offset; GPIO28/IQ_FLAG parity',
                    head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=iq.ROOT, text=True).strip(),
                    source_firmware_directory=str(source), build_directory=str(build), sha256=hashes)
    (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    iq.LOGGING = False
    board, failed = iq.Board(args.port), False

    def command(line):
        response, _ = board.cmd(line)
        with (args.out/'commands.jsonl').open('a') as f:
            f.write(json.dumps(dict(command=line, response=response))+'\n')
        if response.get('error'):
            raise ValueError(f'{line}: {response["error"]}')
        return response

    def run(label, line, validator):
        nonlocal failed
        try:
            result, lines = board.cmd(line, timeout=args.ms/1000+30)
            (args.out/f'{label}.txt').write_text('\n'.join(lines)+'\n')
            result.update(validator(result, lines))
        except Exception as error:
            result = dict(faults=['host exception'], exception=type(error).__name__, detail=str(error), classification='failed')
            with (args.out/'runs.jsonl').open('a') as f:
                f.write(json.dumps(dict(label=label, command=line, **result))+'\n')
            raise
        with (args.out/'runs.jsonl').open('a') as f:
            f.write(json.dumps(dict(label=label, command=line, **result))+'\n')
        print(label, json.dumps(result), flush=True)
        failed |= bool(result['faults'])

    try:
        for option in (f'dualprio {args.priority}', 'dualwriters 4', 'quadprofile 1', f'bchkernel {args.bch_kernel}',
                       'mapkernel 1', 'ldpcpack 1', f'bchbank {args.bch_bank}'):
            command(option)
        for clock in args.clocks:
            validate_clock(command(f'clock {clock}'), clock, args.expected_git)
            for cpw in args.cpws:
                for source_id in args.sources:
                    for rep in range(args.reps):
                        if args.control:
                            command('dualclock 1')
                            label = f'control-{clock}-{cpw}-{source_id}-code{args.code_index}-{rep}'
                            run(label, f'dualstream {cpw} {args.ms} 2 {source_id} 4096 0 0 0 4 {args.code_index}',
                                lambda r, lines: validate_control(r, lines, cpw, args.ms, source_id,
                                                                  clock*1000, args.expected_git,
                                                                  args.code_index, args.bch_kernel, 1, args.bch_bank))
                        for clock_mode in args.clock_modes:
                            command(f'dualclock {clock_mode}')
                            for fault in args.faults:
                                if clock_mode == 2 and fault:
                                    continue
                                pointer_cmd = 'dualptr4' if args.writers == 4 else 'dualptr'
                                adapter = f' {args.pv_kernel}' if args.pv_kernel else ''   # default line unchanged
                                label = (f'{pointer_cmd}-{clock}-{cpw}-{source_id}-code{args.code_index}'
                                         f'{"-pvk" + str(args.pv_kernel) if args.pv_kernel else ""}'
                                         f'-clock{clock_mode}-fault{fault}-{rep}')
                                run(label, f'{pointer_cmd} {cpw} {args.ms} {source_id} {fault} {args.code_index}{adapter}',
                                    lambda r, lines: validate_run(r, lines, cpw, args.ms, source_id,
                                                                  fault, clock_mode, clock*1000, args.expected_git,
                                                                  args.code_index, args.bch_kernel, args.bch_bank,
                                                                  priority=args.priority, writers=args.writers,
                                                                  pv_kernel=args.pv_kernel))
    finally:
        try:
            command('dualclock 0')
            validate_clock(command('clock 128000'), 128000, args.expected_git)
        finally:
            board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
