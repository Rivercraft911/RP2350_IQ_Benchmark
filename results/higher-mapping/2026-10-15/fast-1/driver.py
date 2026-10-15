"""Run from repo root: python3 SCRIPT OUTPUT_DIR. Component-only Pico mapper A/B."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zlib
import numpy as np

root = Path.cwd()
sys.path.insert(0, str(root / 'host'))
import iqbench as iq
from test_dvbs2_8psk import scalar_frame, packed_groups, ref

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=False)
shutil.copy2(__file__, out / 'driver.py')
build = Path('/tmp/yapogee-new-gate-snapshot/build')
source = Path('/tmp/yapogee-new-gate-snapshot/firmware/src')
hashes = {}
for name in ('iqbench.uf2', 'iqbench.elf'):
    shutil.copy2(build / name, out / name)
    hashes[name] = hashlib.sha256((build / name).read_bytes()).hexdigest()
for f in source.glob('*'):
    if f.suffix in ('.c', '.h', '.S'):
        hashes['firmware/src/' + f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
manifest = dict(started=dt.datetime.now().isoformat(), sha256=hashes,
                head=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
                setup='USB only, stock128MHz, no GPIO connections',
                scope='Encoder/mapper components; no full higher-order streaming claim')
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
iq.LOGGING = False
b = iq.Board()
failures = []

def record(r):
    with (out / 'runs.jsonl').open('a') as f:
        f.write(json.dumps(r) + '\n')

try:
    b.cmd('clock 128000')
    b.cmd('bchkernel 2')
    for kernel in (0, 1):
        b.cmd(f'mapkernel {kernel}')
        for code in (5, 6, 8):
            for pilots in (0, 1):
                r = iq.run_dvbs2(b, code, pilots, 8, 'same-image-map-ab')
                if r.get('map_kernel') != kernel or not r.get('crc_ok'):
                    failures.append((kernel, code, pilots))
                record(r)
    for code in (4, 5, 6, 8, 9, 10, 15, 16, 17, 19, 20):
        for pilots in (0, 1):
            r, _ = b.cmd(f'bench8map {code} {pilots} 64', timeout=120)
            shape, rate = r['code'].split()
            short = shape == 'short'
            kbch = ref.code(rate, short).kbch
            words = iq.m.xorshift32(0x9e3779b9 ^ code, (kbch+31)//32)
            bits = ((words[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()[:kbch]
            fec = ref.fecframe(bits, rate, short)
            points = scalar_frame(fec, rate, short, bool(pilots))
            expected = zlib.crc32(packed_groups(points, flush=True).tobytes())
            r.update(expected_crc=expected, crc_ok=r['crc'] == expected,
                     symbols_ok=r['frame_symbols'] == len(points))
            if not r['crc_ok'] or not r['symbols_ok']:
                failures.append(('8psk',code,pilots))
            record(r)
            print(json.dumps(r), flush=True)
finally:
    try:
        for command in ('mapkernel 0', 'bchkernel 0', 'clock 128000'):
            b.cmd(command)
    finally:
        b.s.close()
(out / 'summary.json').write_text(json.dumps(dict(failures=failures, passed=not failures), indent=2)+'\n')
if failures:
    raise SystemExit(1)
