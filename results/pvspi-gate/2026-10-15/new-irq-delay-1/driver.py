"""Compare PV-SPI gates with deliberate IRQ masking; run from repository root."""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import zlib

root = Path.cwd()
sys.path.insert(0, str(root / 'host'))
import iqbench as iq
from dual_spi import pv

p = argparse.ArgumentParser()
p.add_argument('output', type=Path)
p.add_argument('--build', type=Path, default=root / 'firmware/build-limits')
p.add_argument('--source', type=Path, default=root / 'firmware')
p.add_argument('--old-gate', action='store_true')
p.add_argument('--ms', type=int, default=3000)
p.add_argument('--delays', type=int, nargs='+', default=[0, 20, 128, 200])
args = p.parse_args()
out = args.output
out.mkdir(parents=True, exist_ok=False)
shutil.copy2(__file__, out / 'driver.py')
hashes = {}
for name in ('iqbench.uf2', 'iqbench.elf'):
    shutil.copy2(args.build / name, out / name)
    hashes[name] = hashlib.sha256((out / name).read_bytes()).hexdigest()
for f in sorted((args.source / 'src').glob('*')):
    if f.is_file(): hashes[str(f.relative_to(args.source))] = hashlib.sha256(f.read_bytes()).hexdigest()
(out/'manifest.json').write_text(json.dumps(dict(started=dt.datetime.now().isoformat(),
    head=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
    gate='old CPU READY' if args.old_gate else 'PIO hardware READY',
    source=str(args.source), sha256=hashes, duration_ms=args.ms,
    scope='USB-only on-chip SPI loopback; synthetic IRQ delay, no RF'), indent=2)+'\n')
period = b''.join(pv.pattern_packet(k) for k in range(112))
fields = b''.join(pv.message(k, pv.pattern_payload(k))[-4:] for k in range(16))
def periodic_crc(data, n):
    crc = 0
    for _ in range(n // len(data)): crc = zlib.crc32(data, crc)
    return zlib.crc32(data[:n % len(data)], crc)
iq.LOGGING = False
b = iq.Board()
results = []
try:
    for clock in (128000, 150000):
        b.cmd(f'clock {clock}')
        for delay in args.delays:
            r, _ = b.cmd(f'pvstress {args.ms} {delay}', timeout=args.ms/1000 + 30)
            faults = [k for k in ('lost','bad_hdr','bad_crc','bad_sync','short_msgs',
                      'long_msgs','overflows','drain_timeouts','late_closes','errors') if r.get(k,0)]
            if r.get('packets') != 7*r.get('ok',0): faults.append('packets/accepted')
            if r.get('packet_crc') != periodic_crc(period, r.get('packets',0)*188): faults.append('packet CRC')
            if r.get('crc_chain') != periodic_crc(fields, r.get('ok',0)*4): faults.append('message CRC chain')
            if r.get('elapsed_us',0) < args.ms*1000: faults.append('short duration')
            if r.get('ok',0) < 20: faults.append('no runtime arrivals')
            if r.get('irq_mask_us') != delay or r.get('clk_hz') != clock*1000: faults.append('configuration')
            expected_failure = args.old_gate and delay > 0
            r.update(faults=faults, negative_control=expected_failure,
                     passed=bool(faults) if expected_failure else not faults)
            results.append(r)
            with (out/'runs.jsonl').open('a') as f: f.write(json.dumps(r)+'\n')
            print(json.dumps(r), flush=True)
finally:
    try: b.cmd('clock 128000')
    finally: b.s.close()
passed = all(r['passed'] for r in results)
(out/'summary.json').write_text(json.dumps(dict(passed=passed, cases=len(results)),indent=2)+'\n')
raise SystemExit(0 if passed else 1)
