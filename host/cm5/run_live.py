"""Coordinate a bounded real-camera SPI run, then collect both endpoints' evidence."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'host'))
from iqbench import Board
from live_remote import validate_options


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native-spi', action='store_true')
    p.add_argument('--udp-mode', choices=['threaded', 'direct'], default='threaded')
    p.add_argument('--transfer', choices=['duplex', 'tx-only'], default='duplex')
    p.add_argument('--label', required=True)
    p.add_argument('--seconds', type=int, required=True)
    p.add_argument('--allocator', choices=['libcamera', 'dma_heap_cached'], required=True)
    p.add_argument('--encoder-input', choices=['dmabuf', 'copy'], default='dmabuf')
    p.add_argument('--threads', type=int, default=2)
    p.add_argument('--profile-sender', action='store_true')
    p.add_argument('--sender-cpus')
    p.add_argument('--capture-cpus')
    p.add_argument('--sender-nice', type=int, default=0)
    p.add_argument('--remote-root', default='/home/pigeon/pigeonvision')
    p.add_argument('--capture-config', default='output/spi-bringup/capture-spi.json')
    p.add_argument('--binary', default='build/spi-camera-check/pv-capture')
    p.add_argument('--port', default='/dev/cu.usbmodem2101')
    p.add_argument('--ssh-config', required=True)
    p.add_argument('--ssh-host', required=True)
    a = p.parse_args()
    try:
        validate_options(a)
    except ValueError as exc:
        p.error(str(exc))
    out = ROOT / 'results/cm5-spi' / a.label
    out.mkdir(parents=True, exist_ok=False)
    program = Path(__file__).with_name('live_remote.py').read_text()
    (out / 'run_remote.py').write_text(program)
    ssh = ['ssh', '-F', a.ssh_config, a.ssh_host]
    remote_args = ['python3', '-', '--label', a.label, '--seconds', str(a.seconds), '--allocator', a.allocator, '--threads', str(a.threads), '--sender-nice', str(a.sender_nice), '--root', a.remote_root, '--capture-config', a.capture_config, '--binary', a.binary]
    for key in ('sender_cpus', 'capture_cpus'):
        if getattr(a, key):
            remote_args += ['--' + key.replace('_', '-'), getattr(a, key)]
    if a.profile_sender:
        remote_args.append('--profile-sender')
    remote_args += ['--transfer', a.transfer, '--udp-mode', a.udp_mode, '--encoder-input', a.encoder_input]
    if a.native_spi:
        remote_args.append('--native-spi')
    pico_command = f'pvtx 2 {(a.seconds + 20) * 1000} 0 0 20'
    meta = {'started_utc': datetime.now(timezone.utc).isoformat(), 'pico_command': pico_command,
            'remote_command': shlex.join(remote_args), 'scope': 'wired digital link; real dual cameras; no RF',
            'host_git': subprocess.check_output(['git', 'describe', '--always', '--dirty'], cwd=ROOT, text=True).strip()}
    (out / 'metadata.json').write_text(json.dumps(meta, indent=2) + '\n')
    board = Board(a.port)
    try:
        info, _ = board.cmd('info')
        (out / 'pico-info.json').write_text(json.dumps(info, indent=2) + '\n')
        with ThreadPoolExecutor(max_workers=1) as pool:
            receive = pool.submit(board.cmd, pico_command, a.seconds + 35)
            time.sleep(.5)
            with (out / 'remote.stdout').open('w') as stdout, (out / 'remote.stderr').open('w') as stderr:
                remote = subprocess.run(ssh + [shlex.join(remote_args)], input=program, text=True,
                                        stdout=stdout, stderr=stderr, timeout=a.seconds + 18)
            pico, _ = receive.result(timeout=a.seconds + 35)
            (out / 'pico.json').write_text(json.dumps(pico, indent=2) + '\n')
        files = {f: f'{a.remote_root}/output/spi-bringup/{a.label}/{f}'
                 for f in ['report.json', 'profile.json', 'provenance.json', 'capture.json', 'capture.stderr', 'sender.stderr', 'sender.json', 'sender-command.json']}
        if a.native_spi:
            for name in ('sender.stderr', 'sender-command.json'):
                files.pop(name)
        if a.profile_sender:
            files['sender-profile.txt'] = f'{a.remote_root}/output/spi-bringup/{a.label}/sender-profile.txt'
        files.update({f: f'{a.remote_root}/output/sessions/spi-{a.label}/{f}'
                      for f in ['frames.jsonl', 'segments.jsonl', 'session.json']})
        missing = []
        for name, remote_path in files.items():
            result = subprocess.run(ssh + ['cat ' + shlex.quote(remote_path)], capture_output=True)
            if result.returncode:
                missing.append({'file': name, 'error': result.stderr.decode(errors='replace').strip()})
            else:
                (out / name).write_bytes(result.stdout)
        (out / 'collection.json').write_text(json.dumps({'missing': missing}, indent=2) + '\n')
        print(json.dumps({'label': a.label, 'remote_exit': remote.returncode,
                          'pico': pico['pv'], 'missing': missing}, indent=2))
        return remote.returncode or int(bool(missing))
    finally:
        board.s.close()


if __name__ == '__main__':
    raise SystemExit(main())
