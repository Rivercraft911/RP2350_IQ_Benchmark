"""Run one external-CM5 PV-SPI case and preserve both endpoints' reports.

The Pico never drives the SPI input pins in this harness (selftest=0).
Run each case separately and inspect it before increasing the clock.
"""
from __future__ import annotations

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


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--transfer', choices=['duplex', 'tx-only'], default='duplex')
    p.add_argument('--label', required=True)
    p.add_argument('--port', default='/dev/cu.usbmodem2101')
    p.add_argument('--ssh-host', required=True)
    p.add_argument('--ssh-config', type=Path)
    p.add_argument('--remote-root', default='/home/pigeon/pigeonvision')
    p.add_argument('--hz', required=True, type=int)
    p.add_argument('--pico-seconds', required=True, type=float)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--count', type=int)
    group.add_argument('--seconds', type=float)
    a = p.parse_args()
    if not a.label or Path(a.label).name != a.label:
        p.error('label must be one directory name')
    if a.hz <= 0 or a.pico_seconds <= 0 or (a.count is not None and a.count <= 0):
        p.error('clock, count and duration must be positive')
    if a.seconds is not None and not 0 < a.seconds < a.pico_seconds - 3:
        p.error('leave at least 3 seconds for receiver startup/drain')
    out = ROOT / 'results/cm5-spi' / a.label
    out.mkdir(parents=True, exist_ok=False)
    ssh = ['ssh']
    if a.ssh_config:
        ssh += ['-F', str(a.ssh_config)]
    ssh += [a.ssh_host]
    sender = ['software/.venv-spi/bin/python', '-m', 'pigeonvision.spi_transport',
              '--pattern', '--hz', str(a.hz), '--transfer', a.transfer, '--ready-timeout', '2']
    sender += ['--count', str(a.count)] if a.count is not None else ['--duration', str(a.seconds)]
    command = 'cd ' + shlex.quote(a.remote_root) + ' && PYTHONPATH=software/python ' + shlex.join(sender)
    metadata = {'time_utc': datetime.now(timezone.utc).isoformat(),
                'host_git': subprocess.check_output(['git', 'describe', '--always', '--dirty'], cwd=ROOT, text=True).strip(),
                'command': command, 'requested_sck_hz': a.hz,
                'pico_seconds': a.pico_seconds, 'source': 'deterministic TS pattern',
                'scope': 'wired digital link; no RF hardware, clock not scope-measured'}
    write_json(out / 'metadata.json', metadata)
    board = Board(a.port)
    try:
        info, _ = board.cmd('info')
        write_json(out / 'pico-info.json', info)
        with ThreadPoolExecutor(max_workers=1) as workers:
            receive = workers.submit(board.cmd, f'pvtx 2 {int(a.pico_seconds * 1000)} 0 0 20', a.pico_seconds + 10)
            time.sleep(.5)
            sent = subprocess.run(ssh + [command], capture_output=True, text=True,
                                  timeout=a.pico_seconds - 1)
            (out / 'sender.stdout').write_text(sent.stdout)
            (out / 'sender.stderr').write_text(sent.stderr)
            pico, capture = receive.result(timeout=a.pico_seconds + 10)
        write_json(out / 'pico.json', pico)
        if capture:
            (out / 'capture.txt').write_text('\n'.join(capture) + '\n')
        host = json.loads(sent.stdout.strip().splitlines()[-1])
        write_json(out / 'sender.json', host)
        pv = pico.get('pv', {})
        faults = {key: pv.get(key) for key in ('bad_hdr', 'bad_crc', 'bad_sync', 'lost', 'short', 'long', 'overflows')}
        errors = []
        if sent.returncode or host.get('status') != 'complete':
            errors.append('sender did not complete cleanly')
        if pv.get('selftest') != 0:
            errors.append('receiver was not in external-input mode')
        if host.get('messages', 0) <= 0 or host.get('messages') != pv.get('msgs_ok'):
            errors.append('sent/accepted message counts differ or are zero')
        if a.count is not None and host.get('messages') != a.count:
            errors.append('requested finite message count not reached')
        if int(host.get('crc_chain', '-1'), 16) != pv.get('crc_chain'):
            errors.append('sent/accepted CRC chains differ')
        if host.get('ts_packets') != pv.get('ts_packets'):
            errors.append('TS packet counts differ')
        if any(value is None or value != 0 for value in faults.values()):
            errors.append('receiver error counters nonzero or missing')
        if pv.get('nop') != 0:
            errors.append('unexpected NOP messages')
        if pico.get('tables_ok') != 1:
            errors.append('coefficient table verification missing or failed')
        for key in ('underruns', 'own_errors', 'txstalls', 'link_overruns'):
            if pico.get(key) != 0:
                errors.append(f'{key} nonzero or missing')
        result = {'requested_sck_hz': a.hz, 'sender_exit': sent.returncode,
                  'messages_sent': host.get('messages'), 'messages_accepted': pv.get('msgs_ok'),
                  'sender_ts_mbps': host.get('ts_mbps'), 'sender_crc_chain': host.get('crc_chain'),
                  'pico_crc_chain': f"0x{pv.get('crc_chain', 0):08x}", 'receiver_errors': faults,
                  'ready_waits': host.get('ready_waits'), 'max_ready_wait_ms': host.get('max_ready_wait_ms'),
                  'underruns': pico.get('underruns'), 'errors': errors}
        write_json(out / 'comparison.json', result)
        print(json.dumps(result, indent=2), flush=True)
        return bool(errors)
    finally:
        board.s.close()


if __name__ == '__main__':
    raise SystemExit(main())
