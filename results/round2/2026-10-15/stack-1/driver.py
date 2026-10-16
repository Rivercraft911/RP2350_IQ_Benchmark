"""Stack-overflow hypothesis test for the kernel-3 hangs: run each case, then stackcheck.

Run from the round2-merge worktree root after flashing ./iqbench.uf2. Stops at the first
case whose command or the following stackcheck gets no reply (a hang).
"""
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, 'host')
import iqbench as iq
out = Path(__file__).parent / 'runs.jsonl'
iq.LOGGING = False
b = iq.Board(None)
def log(label, r):
    with out.open('a') as f: f.write(json.dumps(dict(label=label, recorded_at=dt.datetime.now().isoformat(), **r)) + '\n')
def check(label):
    r, _ = b.cmd('stackcheck', timeout=10)
    log(f'stackcheck-after-{label}', r)
    c0, c1 = r.get('core0_scratch_y', {}), r.get('core1_scratch_x', {})
    print(f'  stack after {label}: core0 deepest {c0.get("deepest_use_bytes")} headroom {c0.get("headroom_above_code_bytes")} '
          f'code_ok {c0.get("code_matches_flash")} | core1 deepest {c1.get("deepest_use_bytes")} '
          f'headroom {c1.get("headroom_above_code_bytes")} code_ok {c1.get("code_matches_flash")}', flush=True)
CASES = [
    ('boot', None, []),
    ('desc-910-k2-ts', 'dualstream 1 3000 2 5 4096 0 0 0 4 10', ['dualprio 0', 'bchkernel 2']),
    ('desc-910-k3-ts', 'dualstream 1 3000 2 5 4096 0 0 0 4 10', ['dualprio 0', 'bchkernel 3']),
    ('ptr-56-k2-gcs-p13', 'dualptr 1 3000 6 0 8', ['dualprio 13', 'bchkernel 2']),
    ('ptr-910-k2-gcs-p13', 'dualptr 1 3000 6 0 10', ['dualprio 13', 'bchkernel 2']),
    ('ptr-910-k3-gcs-p13', 'dualptr 1 3000 6 0 10', ['dualprio 13', 'bchkernel 3']),
    ('desc-910-k3-gcs', 'dualstream 1 3000 2 6 4096 0 0 0 4 10', ['dualprio 0', 'bchkernel 3']),
    ('ptr-910-k3-ts-p13', 'dualptr 1 3000 5 0 10', ['dualprio 13', 'bchkernel 3']),
]
try:
    for line in ('info', 'dualwriters 4', 'quadprofile 1', 'mapkernel 1', 'ldpcpack 1', 'bchbank 1',
                 'clock 128000', 'dualclock 1'):
        r, _ = b.cmd(line, timeout=30); log(line, r)
    for label, command, setup in CASES:
        for line in setup: b.cmd(line, timeout=30)
        if command:
            t = time.time()
            try:
                r, _ = b.cmd(command, timeout=60)
            except Exception as e:
                log(label, dict(exception=type(e).__name__, detail=str(e), host_seconds=time.time() - t))
                print('HANG in', label, flush=True); raise
            log(label, {k: v for k, v in r.items() if k != 'pv_spi'})
            print(label, {k: r.get(k) for k in ('elapsed_us', 'errors', 'underruns', 'core0_active_busy', 'core0_work_busy',
                                                 'core1_work_busy', 'reason')}, flush=True)
        check(label)
finally:
    try:
        for line in ('dualclock 0', 'dualprio 0', 'bchkernel 0', 'ldpcpack 0', 'bchbank 0', 'mapkernel 0'): b.cmd(line, timeout=10)
    finally:
        b.s.close()
