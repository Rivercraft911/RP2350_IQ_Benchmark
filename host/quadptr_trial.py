"""Pin-free finite L8 pointer/address/credit test; no live-rate or RF qualification.

Build and flash separately. Both modes check every2048I+2048Q numerical pointer,
serial integrity, exact512-word source boundary, terminal fault scope and cleanup.
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

import numpy as np
import iqbench as iq
from dual_trial import parse_capture

PROFILE = 'qpsk_l8_n4_both_m010_far2'
SEED, PREVIOUS, INPUT_WORDS, AXIS_POINTERS = 0x1234abcd, 0x716bc429, 512, 2048
SOURCE_EMPTY, PIO_ERROR = 8, 16


def reference_addresses(mode: int, base: int, seed=SEED, previous=PREVIOUS):
    if mode not in (0, 1):
        raise ValueError('mode must be0(raw) or1(BSWAP)')
    words = iq.m.xorshift32(seed, INPUT_WORDS).astype('<u4')
    words[:4] = [0, 0xffffffff, 0x5555aaaa, previous]
    axes = []
    for axis in range(2):
        timeline = [(previous >> (16 * axis + j)) & 1 for j in range(16)]
        timeline += [(int(w) >> (16 * axis + j)) & 1 for w in words for j in range(16)]
        addresses = []
        for pos in range(0, 16 * INPUT_WORDS, 4):
            window = timeline[9 + pos:20 + pos]
            # Independent chronological bits -> PIO row ordering, three/four/four groups.
            key_bits = window[:3] + window[7:11] + window[3:7]
            addresses.append(base + 32 * sum(bit << j for j, bit in enumerate(key_bits)))
        values = np.array(addresses, dtype='<u4')
        axes.append(values if mode else values.byteswap())
    return words, np.concatenate(axes).astype('<u4')


def validate_capture(result: dict, lines: list[str], mode: int, clock_hz=None) -> dict:
    faults = []
    try:
        got = parse_capture(lines)
        if result.get('cap_error') or result.get('cap_words') != 4096 or len(got) != 4096:
            raise ValueError('incomplete4096-word capture')
        if zlib.crc32(got.tobytes()) != result.get('cap_crc'):
            raise ValueError('serial capture CRC')
        base = int(result['table_base'])
        if base & 65535 or not 0x20000000 <= base <= 0x20070000:
            raise ValueError('table alignment/range')
        arena = int(result['ring_base'])
        if result.get('ring_bytes') != 131072 or not arena <= base < base + 65536 <= arena + 131072:
            raise ValueError('table outside existing ring arena')
        words, expected = reference_addresses(mode, base)
    except (KeyError, TypeError, ValueError) as error:
        return dict(faults=[str(error)], classification='failed', host_checked_pointers=0,
                    host_address_mismatches=None)
    if result.get('error') or not result.get('ok') or result.get('errors'):
        faults.append('firmware component failure')
    expected_fields = dict(cmd='benchquadptr', mode=mode, profile=PROFILE, table_bytes=65536,
                           table_crc=3433922394, seed=SEED, previous=PREVIOUS,
                           input_words=512, input_ring_words=8192, cap_layout='I_then_Q',
                           capture_axis_words=[2048, 2048], capture_bswap=bool(mode),
                           input_issue_words=[512, 512], input_remaining=[0, 0],
                           input_busy_mask=0, published_words=512, granted_words=512,
                           read_addr_stable=True, other_pio_flags=0, capture_dma_errors=0,
                           cleanup_ok=True, restored=True, gpio_configured=False,
                           unexpected_pointer_errors=0, address_mismatches=[0, 0])
    if clock_hz is not None:
        expected_fields['clk_hz'] = clock_hz
    faults += [key for key, value in expected_fields.items() if result.get(key) != value]
    if result.get('input_crc') != zlib.crc32(words.tobytes()):
        faults.append('input reference CRC')
    dma_mask, sm_mask = int(result.get('input_dma_mask', 0)), int(result.get('pointer_sm_mask', 0))
    if dma_mask.bit_count() != 2 or dma_mask & ~0xffff or result.get('null_trigger_mask') != dma_mask:
        faults.append('both terminal null triggers')
    txstall = int(result.get('terminal_txstall_mask', 0))
    if sm_mask.bit_count() != 2 or sm_mask & ~15 or txstall & ~sm_mask or result.get('sm_enabled_at_stop') != sm_mask:
        faults.append('pointer SM terminal state')
    if int(result.get('rxstall_mask', 0)) & ~sm_mask:
        faults.append('RX stall mask outside pointer SMs')
    allowed = SOURCE_EMPTY | (PIO_ERROR if txstall and not result.get('other_pio_flags') else 0)
    raw_errors = int(result.get('pointer_errors', 0))
    if not raw_errors & SOURCE_EMPTY or raw_errors & ~allowed or result.get('expected_pointer_errors') != allowed:
        faults.append('unexpected pointer fault scope')
    try:
        origin = int(result['input_base'])
        if origin & 32767 or result.get('input_read_addr') != [origin + 2048, origin + 2050]:
            faults.append('uncredited source word512 boundary')
    except (KeyError, TypeError, ValueError):
        faults.append('source READ_ADDR missing')
    mismatch = np.flatnonzero(got != expected)
    per_axis = [int(np.count_nonzero(got[a*2048:(a+1)*2048] != expected[a*2048:(a+1)*2048])) for a in range(2)]
    if len(mismatch):
        faults.append('numerical address mismatch')
    expected_crc = [zlib.crc32(expected[a*2048:(a+1)*2048].tobytes()) for a in range(2)]
    if result.get('expected_axis_crc') != expected_crc or result.get('axis_crc') != expected_crc:
        faults.append('axis CRC/reference mismatch')
    return dict(faults=faults, classification='failed' if faults else 'finite_component_clean',
                host_checked_pointers=4096, host_address_mismatches=per_axis,
                host_first_bad=int(mismatch[0]) if len(mismatch) else None,
                host_expected_axis_crc=expected_crc)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--clocks', nargs='+', type=int, default=[128000, 150000])
    p.add_argument('--modes', nargs='+', type=int, choices=(0, 1), default=[0, 1])
    p.add_argument('--reps', type=int, default=1)
    p.add_argument('--port')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build-limits')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    args = p.parse_args()
    if args.reps < 1:
        p.error('--reps must be positive')
    args.out.mkdir(parents=True, exist_ok=False)
    build = args.build.resolve()
    firmware = args.source.resolve()
    shutil.copy2(__file__, args.out/'driver.py')
    hashes = {}
    for name in ('iqbench.uf2', 'iqbench.elf'):
        shutil.copy2(build / name, args.out / name)
        hashes[name] = hashlib.sha256((args.out / name).read_bytes()).hexdigest()
    source = [f for f in (firmware/'src').rglob('*') if f.suffix in ('.c', '.h', '.S')]
    source += [firmware/'CMakeLists.txt']
    hashes.update({'firmware/'+str(f.relative_to(firmware)):hashlib.sha256(f.read_bytes()).hexdigest() for f in source})
    hashes['host/quadptr_trial.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), clocks_khz=args.clocks, modes=args.modes,
                    reps=args.reps, setup='USB only; no GPIO configured by this component',
                    scope='Finite L8 pointer addresses and512-word credit boundary; no live-output, sample-waveform or RF claim',
                    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=iq.ROOT,text=True).strip(),
                    source_firmware_directory=str(firmware),build_directory=str(build),sha256=hashes)
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        for clock in args.clocks:
            board.cmd(f'clock {clock}')
            for mode in args.modes:
                for rep in range(args.reps):
                    result, cap = board.cmd(f'benchquadptr {mode}',timeout=20)
                    (args.out/f'capture-{clock}-{mode}-{rep}.txt').write_text('\n'.join(cap)+'\n')
                    result.update(validate_capture(result,cap,mode,clock*1000))
                    result['rep'] = rep
                    with (args.out/'runs.jsonl').open('a') as f:
                        f.write(json.dumps(result)+'\n')
                    print(json.dumps(result),flush=True)
                    failed |= bool(result['faults'])
    finally:
        try:
            board.cmd('clock 128000')
        finally:
            board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
