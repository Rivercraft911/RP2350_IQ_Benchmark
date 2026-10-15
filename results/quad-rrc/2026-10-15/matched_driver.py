"""Same-image L12/two-symbol versus L9/four-symbol 16-Msym/s comparison."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path('/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark')
sys.path.insert(0, str(ROOT / 'host'))
import iqbench as iq
import dual_trial as trial

out = ROOT / sys.argv[1]
out.mkdir(parents=True, exist_ok=False)
snapshot = ROOT / 'results/quad-rrc/2026-10-15/soak-two-2/manifest.json'
manifest = json.loads(snapshot.read_text())
manifest.update(started=dt.datetime.now().isoformat(), profile='matched-quad', duration_ms=60000,
                comparison='Same flashed image; L12 row2/four writers and L9 beta0.8 row4/two writers')
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
iq.LOGGING = False
b = iq.Board()
failures = []
try:
    for command in ('clock 128000', 'bchkernel 2', 'dualprio 0'):
        r, _ = b.cmd(command)
        if r.get('error'):
            raise RuntimeError(r)
    for rows, writers, source in ((2, 4, 4), (4, 2, 4), (2, 4, 5), (4, 2, 5)):
        b.cmd(f'dualwriters {writers}')
        label = f'row{rows}-writers{writers}-source{source}-60s'
        r, cap = b.cmd(f'dualstream 1 60000 2 {source} 4096 0 0 0 {rows}', timeout=120)
        (out / (label + '.txt')).write_text('\n'.join(cap) + '\n')
        r.update(trial.validate_capture(r, cap, source, 4096))
        r['faults'] = trial.stream_faults(r, True, source)
        expected = dict(row_symbols=rows, writers=writers, source=source, clk_hz=128000000,
                        L=trial.pulse_parameters(rows)[0], divider256=256)
        for key, value in expected.items():
            if r.get(key) != value:
                r['faults'].append(f'{key}: got {r.get(key)}, wanted {value}')
        if r.get('elapsed_us', 0) < 60000000:
            r['faults'].append('short duration')
        r.update(label=label, classification='failed' if r['faults'] else 'clean',
                 recorded_at=dt.datetime.now().isoformat())
        with (out / 'runs.jsonl').open('a') as f:
            f.write(json.dumps(r) + '\n')
        print(json.dumps({k: r.get(k) for k in ('label', 'gen_cyc_per_sym', 'core0_work_busy',
                  'core0_active_busy', 's2_busy', 'cap_mismatches', 'faults')}), flush=True)
        if r['faults']:
            failures.append(label)
finally:
    try:
        for command in ('clock 128000', 'bchkernel 0', 'dualwriters 2', 'dualprio 0'):
            b.cmd(command)
    finally:
        b.s.close()
(out / 'summary.json').write_text(json.dumps(dict(failures=failures, passed=not failures), indent=2) + '\n')
if failures:
    raise SystemExit(1)
