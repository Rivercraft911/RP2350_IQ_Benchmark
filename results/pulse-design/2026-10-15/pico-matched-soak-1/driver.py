"""Matched L9/L8 pulse trials on the frozen BCH-placement image."""
from pathlib import Path
import argparse,json,sys,shutil
root=Path.cwd();sys.path.insert(0,str(root/'host'))
import iqbench as iq
import dual_trial as trial
from pulse_profiles import L8_PROFILE
p=argparse.ArgumentParser();p.add_argument('out',type=Path);p.add_argument('--ms',type=int,default=3000)
p.add_argument('--quick',action='store_true');a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
shutil.copy2(__file__,a.out/'driver.py')
saved=root/'results/8psk-live/2026-10-15/bch-placement-quick-2'
manifest=json.loads((saved/'manifest.json').read_text());manifest.update(scope='Matched QPSK L9/L8 profile comparison',duration_ms=a.ms)
(a.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
cases=[(profile,6,8,1,0) for profile in (0,1)]
if a.quick: cases=[(0,1,5,1,0),(1,1,5,1,0),(1,3,5,2,0),(1,3,5,1,384)]+cases
iq.LOGGING=False;b=iq.Board();results=[]
try:
 for c in ('clock 128000','bchkernel 2','mapkernel 1','dualwriters 4','dualprio 0'):b.cmd(c)
 for profile,source,code,cpw,divider in cases:
  b.cmd(f'quadprofile {profile}')
  r,cap=b.cmd(f'dualstream {cpw} {a.ms} 2 {source} 4096 0 0 {divider} 4 {code}',timeout=a.ms/1000+40)
  label=f'profile{profile}-source{source}-code{code}-cpw{cpw}-div{divider}'
  (a.out/(label+'.txt')).write_text('\n'.join(cap)+'\n')
  r.update(trial.validate_capture(r,cap,source));r['faults']=trial.stream_faults(r,True,source)
  expected=dict(code_index=code,source=source,map_kernel=1,writers=4,row_symbols=4,clk_hz=128000000,
                L=8 if profile else 9,row_profile=L8_PROFILE if profile else 'l9_quad',
                git='dc9e67977a-dirty',divider256=divider or cpw*256)
  for k,v in expected.items():
   if r.get(k)!=v:r['faults'].append(f'{k} got{r.get(k)} wanted{v}')
  if r.get('elapsed_us',0)<a.ms*1000:r['faults'].append('short duration')
  r.update(label=label,passed=not r['faults']);results.append(r)
  with(a.out/'runs.jsonl').open('a')as f:f.write(json.dumps(r)+'\n')
  print(json.dumps(r),flush=True)
finally:
 try:
  for c in ('quadprofile 0','bchkernel 0','mapkernel 0','dualwriters 2','dualprio 0','clock 128000'):b.cmd(c)
 finally:b.s.close()
passed=all(r['passed'] for r in results)
(a.out/'summary.json').write_text(json.dumps(dict(passed=passed),indent=2)+'\n')
raise SystemExit(0 if passed else 1)
