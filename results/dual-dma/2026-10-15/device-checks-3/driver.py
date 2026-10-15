import datetime as dt
import hashlib,json,subprocess,sys
from pathlib import Path
root=Path('/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark')
sys.path.insert(0,str(root/'host'))
import iqbench as iq
iq.LOGGING=False
out=root/'results/dual-dma/2026-10-15/final-1'
out.mkdir(exist_ok=False)
binary=root/'firmware/build-limits/iqbench.uf2'
(out/'iqbench.uf2').write_bytes(binary.read_bytes())
(out/'picotool-flash.txt').write_text(Path('/tmp/yapogee-503bce5-flash.txt').read_text())
flash=dict(head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
 sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),verified_flash=True,board_serial='D7304C6F543390D2')
(out/'flashed-firmware.json').write_text(json.dumps(flash,indent=2)+'\n')
checks=out.parent/'device-checks-3';checks.mkdir(exist_ok=False)
m=json.loads((out.parent/'spi-2/manifest.json').read_text())
m.update(started=dt.datetime.now().isoformat(),head=flash['head'],profile='device-encoder-and-pointer-checks',
 status=subprocess.check_output(['git','status','--short'],cwd=root,text=True))
m['sha256']={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in m['sha256']}
(checks/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
b=iq.Board()
try:
 info,_=b.cmd('info'); assert info['git'].startswith('503bce5'),info
 (checks/'info.json').write_text(json.dumps(info,indent=2)+'\n')
 b.cmd('clock 128000');b.cmd('bchkernel 2')
 for code in range(21):
  for pilots in (0,1):
   r=iq.run_dvbs2(b,code,pilots,4,None)
   with (checks/'fec.jsonl').open('a') as f:f.write(json.dumps(r)+'\n')
 pointer,_=b.cmd('ptrbench 32',timeout=120)
 assert pointer['completed']==32 and not any(pointer[k] for k in ('errors','mismatches','txstall_mask','dma_error_mask')),pointer
 (checks/'pointers.json').write_text(json.dumps(pointer,indent=2)+'\n')
finally:
 b.cmd('clock 128000');b.cmd('bchkernel 0');b.s.close()
if iq.FAILURES:raise SystemExit(iq.FAILURES)
subprocess.run([sys.executable,'host/dual_trial.py','spi','--ms','60000','--out',str(out.relative_to(root)),
 '--build','firmware/build-limits'],cwd=root,check=True)
