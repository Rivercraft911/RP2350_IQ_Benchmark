"""Run on the CM5; the caller starts the Pico in external-input mode first."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import time


def validate_options(a):
    if (Path(a.label).name != a.label or a.label in ('.', '..')
            or not 10 <= a.seconds <= 1200 or not 1 <= a.threads <= 8
            or not -20 <= a.sender_nice <= 19):
        raise ValueError('invalid label, duration, thread count or priority')
    for cpus in (a.sender_cpus, a.capture_cpus):
        if cpus and not re.fullmatch(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*', cpus):
            raise ValueError('invalid CPU list')
    if a.native_spi and (a.sender_cpus or a.sender_nice or a.profile_sender
                         or a.udp_mode != 'threaded' or a.transfer != 'duplex'):
        raise ValueError('native SPI does not use Python sender options')


def configure_capture(config, a, session):
    config = dict(config)
    config.update(duration_seconds=a.seconds, session_dir=str(session),
                  udp_destination=None if a.native_spi else '127.0.0.1:1234',
                  capture_allocator=a.allocator, encoder_threads=a.threads,
                  encoder_input=a.encoder_input)
    config.pop('spi', None)
    if a.native_spi:
        config['spi'] = {'device': '/dev/spidev0.0', 'gpiochip': '',
                         'hz': 20000000, 'ready_line': 25}
    return config


def native_summary(final, error, capture_exit):
    if not final or not final['outputs']['transport'].get('spi'):
        return None
    summary = dict(final['outputs']['transport']['spi'])
    summary['crc_chain'] = f"0x{summary['crc_chain']:08x}"
    summary.update(implementation='native', error=error,
                   status='complete' if not error and capture_exit == 0
                   and not final['failed'] and not final.get('signal') else 'error')
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path('/home/pigeon/pigeonvision'))
    p.add_argument('--native-spi', action='store_true')
    p.add_argument('--udp-mode', choices=['threaded', 'direct'], default='threaded')
    p.add_argument('--transfer', choices=['duplex', 'tx-only'], default='duplex')
    p.add_argument('--label', required=True)
    p.add_argument('--seconds', type=int, required=True)
    p.add_argument('--allocator', choices=['libcamera', 'dma_heap_cached'], required=True)
    p.add_argument('--capture-config', default='output/spi-bringup/capture-spi.json')
    p.add_argument('--binary', default='build/spi-camera-check/pv-capture')
    p.add_argument('--encoder-input', choices=['dmabuf', 'copy'], default='dmabuf')
    p.add_argument('--threads', type=int, default=2)
    p.add_argument('--profile-sender', action='store_true')
    p.add_argument('--sender-cpus', help='taskset CPU list for the SPI process')
    p.add_argument('--capture-cpus', help='taskset CPU list for capture/encoders')
    p.add_argument('--sender-nice', type=int, default=0)
    a = p.parse_args()
    try:
        validate_options(a)
    except ValueError as exc:
        p.error(str(exc))
    root = a.root.resolve()
    os.chdir(root)
    out = root / 'output/spi-bringup' / a.label
    out.mkdir(exist_ok=False)
    session = root / 'output/sessions' / ('spi-' + a.label)
    if session.exists():
        raise RuntimeError('session already exists')
    config = json.loads((root / a.capture_config).read_text())
    config = configure_capture(config, a, session)
    config_file = out / 'capture.json'
    config_file.write_text(json.dumps(config, indent=2) + '\n')
    binary = (root / a.binary).resolve()
    provenance = {'sender_cpus': a.sender_cpus, 'capture_cpus': a.capture_cpus,
                  'cpu_governor': Path('/sys/devices/system/cpu/cpufreq/policy0/scaling_governor').read_text().strip(),
                  'interrupts_before': Path('/proc/interrupts').read_text(),
                  'sender_nice': a.sender_nice, 'binary': str(binary), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                  'source_sha256': {str(f.relative_to(root)): hashlib.sha256(f.read_bytes()).hexdigest()
                                    for f in sorted((root / 'software/flight').rglob('*'))
                                    if f.is_file() and f.suffix in ('.cpp', '.hpp', '.txt')}}
    if not a.native_spi:
        provenance.update(
            sender_sha256=hashlib.sha256((root / 'software/python/pigeonvision/spi_transport.py').read_bytes()).hexdigest(),
            rmem_before=Path('/proc/sys/net/core/rmem_max').read_text().strip())
    (out / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    if not a.native_spi:
        subprocess.run(['sudo', '-n', '/usr/sbin/sysctl', '-w', 'net.core.rmem_max=4194304'], check=True, stdout=subprocess.DEVNULL)

    def bound():
        return any(line.split()[1] == '0100007F:04D2' for line in Path('/proc/net/udp').read_text().splitlines()[1:])

    if not a.native_spi and bound():
        raise RuntimeError('UDP 127.0.0.1:1234 already bound')
    sender = capture = None
    profiles = []
    t0 = time.monotonic()
    error = None
    try:
        with (out / 'sender.stdout').open('w') as so, (out / 'sender.stderr').open('w') as se, (out / 'capture.stdout').open('w') as co, (out / 'capture.stderr').open('w') as ce:
            if not a.native_spi:
                env = dict(os.environ, PYTHONPATH=str(root / 'software/python'))
                command = [str(root / 'software/.venv-spi/bin/python'), '-m', 'pigeonvision.spi_transport', '--udp', '127.0.0.1:1234', '--hz', '20000000', '--transfer', a.transfer, '--udp-mode', a.udp_mode, '--duration', str(a.seconds + 10), '--idle-timeout', '15', '--ready-timeout', '2', '--summary', str(out / 'sender.json')]
                if a.profile_sender:
                    command[1:1] = ['-m', 'cProfile', '-o', str(out / 'sender.pstats')]
                if a.sender_cpus:
                    command = ['taskset', '-c', a.sender_cpus] + command
                (out / 'sender-command.json').write_text(json.dumps(command, indent=2) + '\n')
                sender = subprocess.Popen(command, env=env, stdout=so, stderr=se)
                if a.sender_nice:
                    subprocess.run(['sudo', '-n', 'renice', '-n', str(a.sender_nice), '-p', str(sender.pid)], check=True, stdout=subprocess.DEVNULL)
                deadline = time.monotonic() + 4
                while not bound():
                    if sender.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('sender did not bind UDP')
                    time.sleep(.02)
            capture_command = [str(binary), '--config', str(config_file)]
            if a.capture_cpus:
                capture_command = ['taskset', '-c', a.capture_cpus] + capture_command
            capture = subprocess.Popen(capture_command, stdout=co, stderr=ce)
            deadline = time.monotonic() + 10
            manifest = session / 'session.json'
            while not manifest.exists():
                if capture.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('capture did not create manifest')
                time.sleep(.05)
            effective = json.loads(manifest.read_text())['configuration']
            for key in ('width', 'height', 'fps', 'bitrate', 'mux_bitrate', 'encoder_threads', 'encoder_input', 'capture_allocator', 'camera_controls', 'duration_seconds', 'spi'):
                if effective.get(key) != config.get(key):
                    raise RuntimeError(f'capture ignored or changed {key}: {effective.get(key)!r} != {config.get(key)!r}')
            last_progress = 0
            while capture.poll() is None:
                elapsed = time.monotonic() - t0
                if elapsed > a.seconds + 8:
                    raise TimeoutError('capture exceeded duration')
                if sender is not None and sender.poll() is not None:
                    raise RuntimeError('SPI sender ended during capture')
                temp = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
                profiles.append({'seconds': elapsed, 'temperature_c': temp,
                                 'cpu_freq_khz': int(Path('/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq').read_text()),
                                 'cpu_stat': Path('/proc/stat').read_text(),
                                 'capture_affinity': sorted(os.sched_getaffinity(capture.pid)),
                                 'sender_affinity': sorted(os.sched_getaffinity(sender.pid)) if sender else None,
                                 'capture_stat': Path(f'/proc/{capture.pid}/stat').read_text(),
                                 'sender_stat': Path(f'/proc/{sender.pid}/stat').read_text() if sender else None,
                                 'clk_tck': os.sysconf('SC_CLK_TCK')})
                if temp >= 80:
                    raise RuntimeError('temperature reached 80 C; stopping test')
                if elapsed - last_progress >= 30:
                    print(json.dumps({'elapsed_s': round(elapsed, 1), 'temperature_c': temp}), flush=True)
                    last_progress = elapsed
                time.sleep(1)
            capture.wait()
            if sender:
                sender.wait(timeout=15)
    except Exception as exc:
        error = str(exc)
    finally:
        (out / 'profile.json').write_text(json.dumps(profiles, indent=2) + '\n')
        for process in (capture, sender):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    if a.profile_sender and (out / 'sender.pstats').exists():
        import pstats
        with (out / 'sender-profile.txt').open('w') as stream:
            pstats.Stats(str(out / 'sender.pstats'), stream=stream).strip_dirs().sort_stats('tottime').print_stats(45)
    health_file = session / 'health.jsonl'
    report = {'interrupts_after': Path('/proc/interrupts').read_text(), 'capture_exit': capture.returncode if capture else None,
              'sender_exit': sender.returncode if sender else None, 'error': error,
              'sender': json.loads((out / 'sender.json').read_text()) if (out / 'sender.json').exists() else None,
              'health': [json.loads(line) for line in health_file.read_text().splitlines()] if health_file.exists() else [],
              'session_dir': str(session),
              'manifest': json.loads((session / 'session.json').read_text()) if (session / 'session.json').exists() else None}
    if a.native_spi:
        final = next((h for h in reversed(report['health']) if h['type'] == 'session_end'), None)
        report['sender'] = native_summary(final, error, report['capture_exit'])
        report['sender_exit'] = report['capture_exit']
        if report['sender'] is None:
            report['error'] = error or 'capture ended without native SPI summary'
        (out / 'sender.json').write_text(json.dumps(report['sender'], indent=2) + '\n')
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'capture_exit': report['capture_exit'], 'sender_exit': report['sender_exit'],
                      'error': report['error'], 'messages': report['sender']['messages'] if report['sender'] else None,
                      'remote_output': str(out)}), flush=True)
    return int(bool(report['error'] or report['capture_exit'] != 0
                    or report['sender_exit'] != 0 or not report['sender']
                    or report['sender']['status'] != 'complete'))


if __name__ == '__main__':
    raise SystemExit(main())
