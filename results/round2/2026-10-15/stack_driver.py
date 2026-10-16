"""Measure core-0 stack depth per case; never execute corrupted scratch code.

python3 results/round2/2026-10-15/stack_driver.py <out-dir> <case> [<case> ...]
Cases: desc-<code>-k<bch>-<ts|gcs>, ptr-<code>-k<bch>-<ts|gcs>-p<prio>. After each case
`stackcheck` runs; if scratch code no longer matches flash or the stack paint is exhausted,
the driver records it, asks the firmware to reboot (picotool -f) and stops.
"""
import datetime as dt, json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, 'host')
import iqbench as iq
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=False)
iq.LOGGING = False
b = iq.Board(None)
def log(label, r):
    with (out / 'runs.jsonl').open('a') as f:
        f.write(json.dumps(dict(label=label, recorded_at=dt.datetime.now().isoformat(), **r)) + '\n')
def check(label):
    r, _ = b.cmd('stackcheck', timeout=10)
    log('stackcheck-' + label, r)
    c0 = r['core0_scratch_y']
    print(f'  stack {label}: core0 deepest {c0["deepest_use_bytes"]}/{c0["stack_bytes"]} B '
          f'exhausted {c0["paint_exhausted"]} code_ok {c0["code_matches_flash"]} '
          f'first_corrupt {c0["first_corrupt_code_offset"]} | core1 deepest {r["core1_scratch_x"]["deepest_use_bytes"]}',
          flush=True)
    return not c0['code_matches_flash'] or c0['paint_exhausted'] or not r['core1_scratch_x']['code_matches_flash']
damaged = False
try:
    for line in ('info', 'dualwriters 4', 'quadprofile 1', 'mapkernel 1', 'ldpcpack 1', 'bchbank 1',
                 'clock 128000', 'dualclock 1'):
        r, _ = b.cmd(line, timeout=30); log(line, r)
    damaged = check('boot')
    for case in sys.argv[2:]:
        if damaged: break
        parts = case.split('-')
        kind, code, kernel, src = parts[0], int(parts[1]), int(parts[2][1:]), 5 if parts[3] == 'ts' else 6
        prio = int(parts[4][1:]) if len(parts) > 4 else 0
        for line in (f'dualprio {prio}', f'bchkernel {kernel}'): b.cmd(line, timeout=30)
        command = (f'dualstream 1 3000 2 {src} 4096 0 0 0 4 {code}' if kind == 'desc'
                   else f'dualptr 1 3000 {src} 0 {code}')
        r, _ = b.cmd(command, timeout=60)
        log(case, {k: v for k, v in r.items() if k != 'pv_spi'})
        print(case, {k: r.get(k) for k in ('elapsed_us', 'errors', 'core0_active_busy', 'core0_work_busy',
                                           'core1_work_busy', 'reason')}, flush=True)
        damaged = check(case)
finally:
    if damaged:
        print('scratch code damaged or stack paint exhausted: rebooting before anything else runs', flush=True)
        b.s.close()
        subprocess.run(['picotool', 'reboot', '-f'], capture_output=True)
    else:
        for line in ('dualclock 0', 'dualprio 0', 'bchkernel 0', 'ldpcpack 0', 'bchbank 0', 'mapkernel 0'):
            b.cmd(line, timeout=10)
        b.s.close()
