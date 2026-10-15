import datetime as dt
import json
from pathlib import Path
import sys

root = Path('/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark')
sys.path.insert(0, str(root / 'host'))
import iqbench as iq
import dual_trial as trial

iq.LOGGING = False
out = root / 'results/dual-dma/2026-10-15/regression-2'
out.mkdir(exist_ok=False)
manifest = json.loads((out.parent / 'spi-2/manifest.json').read_text())
manifest.update(profile='final-regression', started=dt.datetime.now().isoformat(),
                sequence='two legacy PV-SPI restarts; one-core 8; split 16; SPI 16 repeat; restore idle')
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
b = iq.Board()
failed = []

def record(label, result):
    result.update(label=label, expectation='clean', recorded_at=dt.datetime.now().isoformat(),
                  classification='failed' if result.get('faults') else 'clean')
    with (out / 'runs.jsonl').open('a') as f:
        f.write(json.dumps(result) + '\n')
    print(label, json.dumps({k: result.get(k) for k in ('sym_rate','core0_work_busy','core0_active_busy',
          's2_busy','core1_work_busy','cap_mismatches','faults','pv_spi')}), flush=True)
    if result.get('faults'):
        failed.append(label)

try:
    info, _ = b.cmd('info')
    assert info['git'] == 'c6714d76ab-dirty', info
    b.cmd('clock 128000')
    b.cmd('bchkernel 2')
    b.cmd('dualprio 0')
    for index in range(2):
        record(f'legacy-pv-restart-{index+1}', iq.run_pvtx(b, 2, 3000, True, 4096, 20, None))
    for writers, source, cpw in ((2, 3, 2), (4, 4, 1), (4, 5, 1)):
        b.cmd(f'dualwriters {writers}')
        label = f'dual-row2-128000-src{source}-k2-cpw{cpw}-fault0' + (f'-writers{writers}' if writers != 2 else '')
        r, cap = b.cmd(f'dualstream {cpw} 60000 2 {source} 4096 0 0 0 2', timeout=120)
        if 'error' in r:
            raise RuntimeError(r)
        r.update(trial.validate_capture(r, cap, source, 4096))
        r['faults'] = trial.stream_faults(r, True, source)
        if r.get('elapsed_us', 0) < 60000000:
            r['faults'].append('run ended before the requested duration')
        for key, expected in dict(row_symbols=2, schedule=0, divider256=cpw*256,
                                  clk_hz=128000000, source=source, bus_priority_mask=0, writers=writers).items():
            if r.get(key) != expected:
                r['faults'].append(f'{key}: {r.get(key)} != {expected}')
        captures = out / 'captures'
        captures.mkdir(exist_ok=True)
        p = captures / (label + '.txt')
        p.write_text('\n'.join(cap) + '\n')
        r['capture_file'] = str(p.relative_to(out))
        r.update(requested_schedule=0, requested_divider256=0, afe_sample_rate_within_65msps=True)
        record(label, r)
finally:
    for command in ('clock 128000', 'bchkernel 0', 'dualwriters 2', 'dualprio 0'):
        b.cmd(command)
    b.s.close()
if failed:
    raise SystemExit('Failed: ' + ', '.join(failed))
