"""Focused final-image QPSK GCS/SPI/FEC/output regression; run from repo root."""
import argparse, datetime as dt, hashlib, json, shutil, sys, tarfile
from pathlib import Path
root=Path.cwd();sys.path.insert(0,str(root/'host'))
import iqbench as iq
import dual_trial as trial
from pulse_profiles import L8_PROFILE
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('out',type=Path);p.add_argument('--ms',type=int,default=60000)
a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
source=Path('/tmp/yapogee-poll128-final-snapshot/firmware')
build=source.parent/'build';expected_git='88aff075bc-dirty'
shutil.copy2(__file__,a.out/'driver.py')
files=[*source.glob('src/*'),source/'CMakeLists.txt',source/'git_rev.cmake',source/'pico_sdk_import.cmake']
sha={'firmware/'+str(f.relative_to(source)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file()}
for folder in ('host','reference'):
 for f in (root/folder).rglob('*.py'):
  if 'build' not in f.parts:sha[str(f.relative_to(root))]=hashlib.sha256(f.read_bytes()).hexdigest()
for name in ('iqbench.elf','iqbench.uf2'):
 shutil.copy2(build/name,a.out/name);sha[name]=hashlib.sha256((build/name).read_bytes()).hexdigest()
shutil.copy2('/tmp/yapogee-poll128-final-flash.txt',a.out/'flash.txt')
with tarfile.open(a.out/'firmware-source.tar.gz','w:gz') as tf:tf.add(source,arcname='firmware')
(a.out/'manifest.json').write_text(json.dumps(dict(started=dt.datetime.now().isoformat(),
 expected_firmware_git=expected_git,sha256=sha,source=str(source),build=str(build),duration_ms=a.ms,
 setup='USB only; GPIOs unconnected; stock voltage; on-chip SPI32MHz',
 scope='Complete GCS/SPI/FEC/PL/shaper/output digital regression; no AFE or RF'),indent=2)+'\n')
iq.LOGGING=False;b=iq.Board();r=None
try:
 for command,key,value in (
  ('clock 128000','clk_hz',128000000),('bchkernel 2','kernel',2),('mapkernel 1','kernel',1),
  ('quadprofile 1','profile',1),('dualwriters 4','writers',4),('dualprio 0','mask',0)):
  configured,_=b.cmd(command)
  if configured.get('error') or configured.get(key)!=value:raise RuntimeError((command,configured))
 r,lines=b.cmd(f'dualstream 1 {a.ms} 2 6 4096 0 0 0 4 8',timeout=a.ms/1000+40)
 (a.out/'capture.txt').write_text('\n'.join(lines)+'\n')
 if not r.get('error'):r.update(trial.validate_capture(r,lines,6))
 r['faults']=trial.stream_faults(r,True,6)
 expected=dict(cmd='dualstream',git=expected_git,clk_hz=128000000,source=6,code_index=8,map_kernel=1,
  row_symbols=4,row_profile=L8_PROFILE,L=8,sps=4,writers=4,schedule=0,divider256=256,
  bus_priority_mask=0,sym_rate=16000000.0)
 r['faults'] += [f'configuration {k}' for k,v in expected.items() if r.get(k)!=v]
 if not a.ms*1000<=r.get('elapsed_us',0)<=a.ms*1000+1000:r['faults'].append('duration')
 r['passed']=not r['faults'];r['classification']='clean' if r['passed'] else 'failed'
 (a.out/'runs.jsonl').write_text(json.dumps(r)+'\n')
 print(json.dumps(r),flush=True)
finally:
 try:
  for command in ('quadprofile 0','bchkernel 0','mapkernel 0','dualwriters 2','dualprio 0','clock 128000'):b.cmd(command)
 finally:b.s.close()
(a.out/'summary.json').write_text(json.dumps(dict(passed=bool(r and r['passed'])),indent=2)+'\n')
raise SystemExit(0 if r and r['passed'] else 1)
