"""Single 60-second full-rate 5/6 generic-stream trial on the retained b29d1a7 image."""
from pathlib import Path
import json
import shutil
import sys
root = Path.cwd()
sys.path.insert(0, str(root/'host'))
import iqbench as iq
import dual_trial as trial
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=False)
saved = root/'results/qpsk-codes/2026-10-15/quick-5-6-map1-2'
manifest = json.loads((saved/'manifest.json').read_text())
manifest.update(profile='one-gcs-5/6-16', duration_ms=60000, source=6)
(out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
shutil.copy2(__file__, out/'driver.py')
shutil.copy2(saved/'flash.txt', out/'flash.txt')
iq.LOGGING = False
b = iq.Board()
try:
    for command in ('clock 128000','bchkernel 2','mapkernel 1','dualwriters 4','dualprio 0'):
        r,_ = b.cmd(command)
        if r.get('error'): raise RuntimeError(r)
    r,cap = b.cmd('dualstream 1 60000 2 6 4096 0 0 0 4 8', timeout=120)
    (out/'capture.txt').write_text('\n'.join(cap)+'\n')
    r.update(trial.validate_capture(r,cap,6))
    r['faults'] = trial.stream_faults(r,True,6)
    for k,v in dict(code_index=8,source=6,map_kernel=1,writers=4,row_symbols=4,clk_hz=128000000).items():
        if r.get(k)!=v: r['faults'].append(f'{k}: got{r.get(k)}, wanted{v}')
    if r.get('elapsed_us',0)<60000000: r['faults'].append('short duration')
    r['classification']='failed' if r['faults'] else 'clean'
    (out/'runs.jsonl').write_text(json.dumps(r)+'\n')
    print(json.dumps(r),flush=True)
finally:
    try:
        for command in ('bchkernel 0','mapkernel 0','dualwriters 2','dualprio 0','clock 128000'):
            b.cmd(command)
    finally: b.s.close()
if r['faults']: raise SystemExit(1)
