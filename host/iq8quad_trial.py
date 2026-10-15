"""Isolated two-SPS 8PSK shaper timing and digital output checks; no full DVB-S2/RF claim."""
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
from test_iq8quad import coefficients, direct_reference, input_reference


def reference_bus(seed=0x1234abcd):
    _, bits, _ = input_reference(seed)
    taps, coef = coefficients()
    _, axes, _ = direct_reference([np.tile(b, 2) for b in bits], taps, coef)
    count = len(bits[0]) * 2
    i, q = (axis[count:] & 0x3fff for axis in axes)
    return np.asarray((i | 0x4000) | (q << 16), dtype='<u4').view('<u2')


def validate_capture(result, lines, reference):
    got = parse_capture(lines)
    if result.get('cap_error') or len(got) != 4096 or result.get('cap_words') != 4096:
        return dict(cap_aligned=False, cap_mismatches=None, cap_error='incomplete capture')
    if zlib.crc32(got.tobytes()) != result.get('cap_crc'):
        return dict(cap_aligned=False, cap_mismatches=None, cap_error='serial CRC')
    bus = got.view('<u2')
    candidates = np.flatnonzero(reference == bus[0])
    for j in range(1, 16):
        candidates = candidates[reference[(candidates + j) % len(reference)] == bus[j]]
    best = None
    for offset in candidates:
        bad = int(np.count_nonzero(bus != reference[(int(offset) + np.arange(len(bus))) % len(reference)]))
        if best is None or bad < best[1]:
            best = int(offset), bad
        if not bad:
            break
    return dict(cap_aligned=best is not None, cap_offset=best[0] if best else None,
                cap_mismatches=best[1] if best else None, cap_checked_bus_words=len(bus))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--ms', type=int, default=3000, choices=range(1000, 60001))
    p.add_argument('--clocks', nargs='+', type=int, default=[128000, 150000])
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    build = iq.ROOT / 'firmware/build-limits'
    source = [f for f in (iq.ROOT / 'firmware/src').glob('*') if f.suffix in ('.c', '.h', '.S')]
    source.append(iq.ROOT / 'firmware/CMakeLists.txt')
    hashes = {str(f.relative_to(iq.ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in source}
    for name in ('iqbench.uf2', 'iqbench.elf'):
        shutil.copy2(build / name, a.out / name)
        hashes[name] = hashlib.sha256((build / name).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=a.ms, clocks_khz=a.clocks,
                    head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=iq.ROOT, text=True).strip(),
                    sha256=hashes, setup='USB only, unconnected GPIOs, stock voltage',
                    scope='PRBS rotated8PSK L9/N2 shaper; no FEC/SPI/RF qualification; finite contiguous capture')
    (a.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    ref = reference_bus()
    iq.LOGGING = False
    failures = []
    b = iq.Board()
    try:
        for clock in a.clocks:
            b.cmd(f'clock {clock}')
            for ms in (0, a.ms):
                result, cap = b.cmd(f'bench8quad {ms} 8', timeout=ms / 1000 + 60)
                faults = []
                if result.get('error') or not result.get('crc_ok'):
                    faults.append('component CRC/configuration')
                if ms:
                    (a.out / f'capture-{clock}.txt').write_text('\n'.join(cap) + '\n')
                    result.update(validate_capture(result, cap, ref))
                    faults += [k for k in ('underruns', 'own_errors', 'txstalls') if result.get(k)]
                    if not result.get('cap_aligned') or result.get('cap_mismatches') != 0:
                        faults.append('capture mismatch')
                    if result.get('elapsed_us', 0) < ms * 1000:
                        faults.append('short duration')
                if result.get('clk_hz') != clock * 1000:
                    faults.append('clock mismatch')
                result.update(faults=faults, classification='failed' if faults else 'clean')
                with (a.out / 'runs.jsonl').open('a') as f:
                    f.write(json.dumps(result) + '\n')
                print(json.dumps(result), flush=True)
                if faults:
                    failures.append((clock, ms, faults))
    finally:
        try:
            b.cmd('clock 128000')
        finally:
            b.s.close()
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
