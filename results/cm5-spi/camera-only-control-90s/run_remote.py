"""Same capture configuration, with a local UDP drain instead of SPI."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

root = Path('/home/pigeon/pigeonvision')
os.chdir(root)
out = root / 'output/spi-bringup/camera-only-control-90s'
out.mkdir(exist_ok=False)
session = root / 'output/sessions/spi-camera-only-control-90s'
config = json.loads((root / 'output/spi-bringup/current-cached-t3-priority-60s/capture.json').read_text())
config.update(duration_seconds=90, session_dir=str(session), udp_destination='127.0.0.1:1235')
(out / 'capture.json').write_text(json.dumps(config, indent=2) + '\n')
binary = root / 'build/spi-camera-check/pv-capture'
(out / 'provenance.json').write_text(json.dumps({'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
  'change': 'UDP sink discards locally; no SPI process. Same cached buffers, 3 encoder threads, recording, video and mux rates.'}, indent=2)+'\n')
# Wait for the previous hot run to cool before this short comparison.
for _ in range(120):
    if int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) < 55000:
        break
    time.sleep(1)
sink_code = '''import json, signal, socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4194304)
s.bind(("127.0.0.1",1235)); s.settimeout(.2)
run=True; count=0; total=0
def stop(*args):
 global run
 run=False
signal.signal(signal.SIGTERM,stop)
while run:
 try: data=s.recv(65535)
 except socket.timeout: continue
 count+=1; total+=len(data)
print(json.dumps({"datagrams":count,"bytes":total}))
'''
error = None
with (out / 'sink.json').open('w') as so, (out / 'capture.stderr').open('w') as ce:
    sink = subprocess.Popen(['python3','-c',sink_code],stdout=so)
    capture = None
    try:
        time.sleep(.3)
        capture = subprocess.Popen([str(binary),'--config',str(out/'capture.json')],stdout=subprocess.DEVNULL,stderr=ce)
        start=time.monotonic()
        while capture.poll() is None:
            if int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) >= 80000:
                raise RuntimeError('temperature reached 80 C')
            if time.monotonic()-start > 105 or sink.poll() is not None:
                raise RuntimeError('capture exceeded duration or UDP sink ended')
            time.sleep(1)
        time.sleep(.5)
    except Exception as exc:
        error=str(exc)
    finally:
        for proc in (capture,sink):
            if proc and proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
report={'error':error,'capture_exit':capture.returncode if capture else None,'sink_exit':sink.returncode,
        'health':[json.loads(x) for x in (session/'health.jsonl').read_text().splitlines()],
        'manifest':json.loads((session/'session.json').read_text()),'sink':json.loads((out/'sink.json').read_text())}
(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'error':error,'capture_exit':report['capture_exit'],'sink':report['sink']}))
