from pathlib import Path
import sys,json,datetime as dt
root=Path('/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark')
sys.path.insert(0,str(root/'host'))
import iqbench as iq
iq.LOGGING=False
out=root/'results/dual-dma/2026-10-15/packed-capture-1';out.mkdir(exist_ok=False)
m=json.loads((out.parent/'final-1/manifest.json').read_text())
m.update(profile='packed-capture-start-check',started=dt.datetime.now().isoformat(),duration_ms=1000)
(out/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
b=iq.Board();command=b.cmd;case=''
def capture_command(line,**kwargs):
 r,cap=command(line,**kwargs)
 if cap:
  (out/(case+'.txt')).write_text('\n'.join(cap)+'\n')
 return r,cap
b.cmd=capture_command
try:
 for clock in (128000,150000):
  b.cmd(f'clock {clock}')
  for cpw in (8,9):
   case=f'packed-{clock}-cpw{cpw}'
   r=iq.run_stream(b,'lut_win',4,12,'0',cpw,1000,4096,None)
   assert r['layout']==0 and r['cap_words']==4096 and r['cap_skip']==0,r
   r['capture_file']=case+'.txt'
   with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(r)+'\n')
finally:
 b.cmd('clock 128000');b.cmd('bchkernel 0');b.cmd('dualwriters 2');b.cmd('dualprio 0');b.s.close()
if iq.FAILURES:raise SystemExit(iq.FAILURES)
