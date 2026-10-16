"""Reproduce the 60 s QPSK 9/10 TS hang: kernel 3 then kernel 2, same image, direct commands.

Run from the C2 worktree root after a fresh power cycle. Each row is appended before the next.
"""
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, 'host')
import dual_trial, iqbench as iq
out = Path(__file__).parent / 'runs.jsonl'
iq.LOGGING = False
b = iq.Board(None)
def log(label, r):
    with out.open('a') as f: f.write(json.dumps(dict(label=label, recorded_at=dt.datetime.now().isoformat(), **r)) + '\n')
try:
    for line in ('info', 'dualprio 0', 'dualwriters 4', 'quadprofile 1', 'mapkernel 1', 'ldpcpack 1', 'bchbank 1',
                 'clock 128000', 'dualclock 1'):
        r, _ = b.cmd(line, timeout=30); log(line, r)
    for kernel, source in ((3, 5), (2, 5)):
        b.cmd(f'bchkernel {kernel}', timeout=30)
        t = time.time()
        try:
            r, lines = b.cmd(f'dualstream 1 60000 2 {source} 4096 0 0 0 4 10', timeout=120)
        except Exception as e:
            log(f'k{kernel}-src{source}', dict(exception=type(e).__name__, detail=str(e), host_seconds=time.time() - t))
            print('HANG', kernel, source, time.time() - t, flush=True)
            raise
        if 'error' not in r:
            r.update(dual_trial.validate_capture(r, lines, source, 4096))
        r['faults'] = dual_trial.stream_faults(r, True, source)
        log(f'k{kernel}-src{source}', r)
        print(kernel, source, {k: r.get(k) for k in ('elapsed_us', 'underruns', 'core0_active_busy', 'core1_work_busy',
              'split_bch_max_slice_cyc', 'max_poll_gap_us', 'cap_mismatches', 'cap_clock_mismatches', 'faults')}, flush=True)
finally:
    try:
        for line in ('dualclock 0', 'bchkernel 0', 'ldpcpack 0', 'bchbank 0', 'mapkernel 0'): b.cmd(line, timeout=10)
    finally:
        b.s.close()
