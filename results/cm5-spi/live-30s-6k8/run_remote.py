"""Bounded dual-camera run. Start external pvtx on the Pico before this script."""
import json
import os
from pathlib import Path
import subprocess
import time

root = Path('/home/pigeon/pigeonvision')
os.chdir(root)
out = root / 'output/spi-bringup/live-30s-6k8'
out.mkdir(exist_ok=False)
session = root / 'output/sessions/spi-live-30s-6k8'
if session.exists():
    raise RuntimeError('session already exists')
config = json.loads((root / 'output/spi-bringup/capture-spi.json').read_text())
config.update(duration_seconds=30, session_dir=str(session), udp_destination='127.0.0.1:1234')
config_file = out / 'capture.json'
config_file.write_text(json.dumps(config, indent=2) + '\n')
(out / 'rmem-before.txt').write_text(Path('/proc/sys/net/core/rmem_max').read_text())
subprocess.run(['sudo', '-n', '/usr/sbin/sysctl', '-w', 'net.core.rmem_max=4194304'], check=True, stdout=subprocess.DEVNULL)

def socket_bound():
    return any(line.split()[1] == '0100007F:04D2' for line in Path('/proc/net/udp').read_text().splitlines()[1:])

if socket_bound():
    raise RuntimeError('UDP 127.0.0.1:1234 already bound')
sender = capture = None
try:
    with (out / 'sender.stdout').open('w') as so, (out / 'sender.stderr').open('w') as se, (out / 'capture.stdout').open('w') as co, (out / 'capture.stderr').open('w') as ce:
        env = dict(os.environ, PYTHONPATH=str(root / 'software/python'))
        command = [str(root / 'software/.venv-spi/bin/python'), '-m', 'pigeonvision.spi_transport', '--udp', '127.0.0.1:1234', '--hz', '20000000', '--duration', '38', '--idle-timeout', '15', '--ready-timeout', '2', '--summary', str(out / 'sender.json')]
        (out / 'sender-command.json').write_text(json.dumps(command, indent=2) + '\n')
        sender = subprocess.Popen(command, env=env, stdout=so, stderr=se)
        deadline = time.monotonic() + 4
        while not socket_bound():
            if sender.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('sender did not bind UDP')
            time.sleep(.02)
        capture = subprocess.Popen([str(root / 'build/flight/pv-capture'), '--config', str(config_file)], stdout=co, stderr=ce)
        cap_exit = capture.wait(timeout=36)
        sender_exit = sender.wait(timeout=12)
    health = [json.loads(line) for line in (session / 'health.jsonl').read_text().splitlines()]
    report = {'capture_exit': cap_exit, 'sender_exit': sender_exit, 'sender': json.loads((out / 'sender.json').read_text()), 'health': health}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'capture_exit': cap_exit, 'sender_exit': sender_exit, 'messages': report['sender']['messages'], 'udp': report['sender']['udp'], 'remote_output': str(out)}))
finally:
    for process in (capture, sender):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
