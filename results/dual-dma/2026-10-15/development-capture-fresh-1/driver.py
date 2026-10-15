from pathlib import Path
import sys,json,datetime as dt,hashlib,subprocess
import numpy as np
root=Path('/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark')
sys.path.insert(0,str(root/'host'))
import iqbench as iq
import dual_spi as spi
out=root/'results/dual-dma/2026-10-15/development-capture-fresh-1'
out.mkdir(exist_ok=False)
manifest=json.loads((out.parent/'spi-2/manifest.json').read_text())
manifest.update(profile='legacy-capture-restart-diagnostic',started=dt.datetime.now().isoformat(),duration_ms=3000)
manifest['sha256']={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in manifest['sha256']}
manifest['status']=subprocess.check_output(['git','status','--short'],cwd=root,text=True)
(out/'iqbench.uf2').write_bytes((root/'firmware/build-limits/iqbench.uf2').read_bytes())
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
frames=[spi.ref.plframe(spi.ref.fecframe(spi.bbframe(k),'2/3'),'2/3',pilots=True) for k in range(8)]
words=iq.pack_symbols(np.concatenate([s[0] for s in frames]),np.concatenate([s[1] for s in frames]))
lut=iq.m.Lut(iq.gen_coeffs.ALPHA,4,12,iq.gen_coeffs.KAISER_BETA,iq.gen_coeffs.HEADROOM_DB)
reference=iq.m.generate(words,lut).view(np.uint16)
np.save(out/'reference-bus.npy',reference)
b=iq.Board()
try:
 b.cmd('clock 128000'); b.cmd('bchkernel 2')
 for k in range(5):
  r,cap=b.cmd('pvtx 2 3000 1 4096 20',timeout=90)
  path=out/f'capture-{k}.txt';path.write_text('\n'.join(cap)+'\n')
  r.update(iq.check_capture(r,cap,0,0,ref=reference))
  r.update(trial=k,capture_file=path.name)
  with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(r)+'\n')
  print(k,{key:r.get(key) for key in ('cap_aligned','cap_mismatches','cap_error','cap_offset','underruns','txstalls')},flush=True)
 for clock in (128000,150000):
  b.cmd(f'clock {clock}')
  for kernel,cpw in (('lut_asm',2),('lut_asm_p',2),('lut_asm_p',3)):
   r=iq.run_stream(b,kernel,4,12,'0',cpw,1000,4096,None)
   with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(dict(r,trial=f'{clock}-{kernel}-{cpw}'))+'\n')
finally:
 b.cmd('clock 128000');b.cmd('bchkernel 0');b.cmd('dualwriters 2');b.cmd('dualprio 0');b.s.close()

if iq.FAILURES: raise SystemExit(iq.FAILURES)
