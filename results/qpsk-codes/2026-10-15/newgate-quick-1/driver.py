"""Full FEC/SPI regression on the source-frozen new-gate image."""
from pathlib import Path
import json, sys, shutil
root=Path.cwd();sys.path.insert(0,str(root/'host'))
import iqbench as iq
import dual_trial as trial
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=False)
shutil.copy2(__file__,out/'driver.py')
frozen=root/'results/pvspi-gate/2026-10-15/new-irq-delay-1'
manifest=json.loads((frozen/'manifest.json').read_text())
manifest.update(scope='Full QPSK5/6 TS and GCS, frozen new-gate image', duration_ms=3000,
                image_archive=str(frozen.relative_to(root)))
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
iq.LOGGING=False;b=iq.Board();results=[]
try:
 for c in ('clock 128000','bchkernel 2','mapkernel 1','dualwriters 4','dualprio 0'): b.cmd(c)
 for source in (5,6):
  for cpw in (2,1):
   r,cap=b.cmd(f'dualstream {cpw} 3000 2 {source} 4096 0 0 0 4 8',timeout=40)
   (out/f'cap-src{source}-cpw{cpw}.txt').write_text('\n'.join(cap)+'\n')
   r.update(trial.validate_capture(r,cap,source)); r['faults']=trial.stream_faults(r,True,source)
   for k,v in dict(code_index=8,source=source,map_kernel=1,writers=4,row_symbols=4,clk_hz=128000000).items():
    if r.get(k)!=v:r['faults'].append(f'{k} got{r.get(k)} wanted{v}')
   if r.get('elapsed_us',0)<3000000:r['faults'].append('short duration')
   r['passed']=not r['faults'];results.append(r)
   with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(r)+'\n')
   print(json.dumps(r),flush=True)
finally:
 try:
  for c in ('bchkernel 0','mapkernel 0','dualwriters 2','dualprio 0','clock 128000'):b.cmd(c)
 finally:b.s.close()
passed=all(r['passed'] for r in results)
(out/'summary.json').write_text(json.dumps(dict(passed=passed),indent=2)+'\n')
raise SystemExit(0 if passed else 1)
