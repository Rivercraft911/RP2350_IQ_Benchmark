from pathlib import Path
import sys,json,datetime as dt
import numpy as np
root=Path('/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark')
sys.path.insert(0,str(root/'host'))
import iqbench as iq
import dual_spi as spi
out=root/'results/dual-dma/2026-10-15/legacy-capture-1'
out.mkdir(exist_ok=False)
manifest=json.loads((out.parent/'spi-2/manifest.json').read_text())
manifest.update(profile='legacy-capture-restart-diagnostic',started=dt.datetime.now().isoformat(),duration_ms=3000)
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
finally:
 b.cmd('clock 128000');b.cmd('bchkernel 0');b.cmd('dualwriters 2');b.cmd('dualprio 0');b.s.close()
