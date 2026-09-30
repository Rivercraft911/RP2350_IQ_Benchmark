"""Export chart-ready measurements; transport checks are separate from camera drops."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import statistics
import sys


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def analyze(directory, camera_only=False):
    r = json.loads((directory / 'report.json').read_text())
    p = json.loads((directory / 'pico.json').read_text()) if not camera_only else None
    h, pv = (r['sender'], p['pv']) if not camera_only else (None, None)
    native = bool(h and h.get('implementation') == 'native')
    health = [x for x in r['health'] if x['type'] == 'health']
    end = r['health'][-1]
    frames = [json.loads(x) for x in (directory / 'frames.jsonl').read_text().splitlines()]
    manifest = r['manifest']
    origin = health[0]['timestamp_ns']
    rows, previous = [], {}
    for sample in health:
        for cam in sample['cameras']:
            timer = cam['timing_us']['encoder_input_and_send']
            old = previous.get(cam['camera_id'])
            interval = None
            if old and timer['samples'] > old['samples']:
                interval = (timer['total_us'] - old['total_us']) / (timer['samples'] - old['samples']) / 1000
            previous[cam['camera_id']] = timer
            rows.append({'seconds': (sample['timestamp_ns'] - origin) / 1e9, 'camera': cam['camera_id'],
                         'captured_frames': cam['captured_frames'], 'encoded_frames': cam['encoded_frames'],
                         'dropped_frames': cam['dropped_frames'], 'capture_queue': cam['queue'],
                         'encoder_input_send_interval_ms': interval,
                         'queue_dwell_cumulative_ms': (cam['timing_us']['capture_queue_dwell']['mean_us'] / 1000
                                                       if cam['timing_us']['capture_queue_dwell']['samples'] else None),
                         'temperature_c': sample['temperature_c'], 'cpu_busy_percent': sample['cpu_busy_percent'],
                         'throttled_bits': sample['throttled_bits'], 'transport_bytes': sample['outputs']['transport']['wire_bytes']})
    write_csv(directory / 'telemetry.csv', rows)
    write_csv(directory / 'frames.csv', [{'camera': x['camera_id'], 'sequence': x['sequence'],
        'pts_us': x['pts_us'], 'status': x['status'], 'drop_reason': x.get('drop_reason'),
        'encoded_bytes': x.get('encoded_bytes'), 'keyframe': x.get('keyframe')} for x in frames])
    cams = []
    for cam in ('A', 'B'):
        all_frames = [x for x in frames if x['camera_id'] == cam]
        steady = [x for x in all_frames if x['pts_us'] is not None and x['pts_us'] >= 10_000_000]
        delivered = sorted(steady, key=lambda x: x['pts_us'])
        encoded = [x for x in steady if x['status'] == 'encoded']
        intervals = [(y['pts_us']-x['pts_us'])/1000 for x, y in zip(delivered, delivered[1:])]
        t = [x for x in rows if x['camera'] == cam and x['seconds'] >= 10]
        cams.append({'camera': cam, 'encoded_frames_total': sum(x['status'] == 'encoded' for x in all_frames),
                     'steady_encoded_fps': (len(encoded)-1)*1e6/(encoded[-1]['pts_us']-encoded[0]['pts_us']),
                     'steady_h264_payload_mbps': sum(x['encoded_bytes'] for x in encoded[1:]) * 8 / (encoded[-1]['pts_us']-encoded[0]['pts_us']),
                     'steady_delivered_fps': (len(delivered)-1)*1e6/(delivered[-1]['pts_us']-delivered[0]['pts_us']),
                     'delivered_interval_median_ms': statistics.median(intervals),
                     'delivered_interval_max_ms': max(intervals),
                     'delivered_intervals_over_50ms': sum(x > 50 for x in intervals),
                     'all_drops': dict(Counter(x.get('drop_reason') for x in all_frames if x['status'] != 'encoded')),
                     'steady_drops': dict(Counter(x.get('drop_reason') for x in steady if x['status'] != 'encoded')),
                     'untimed_drops': dict(Counter(x.get('drop_reason') for x in all_frames if x['pts_us'] is None)),
                     'mean_interval_encoder_ms': statistics.mean(x['encoder_input_send_interval_ms'] for x in t if x['encoder_input_send_interval_ms'] is not None)})
    steady_health = [x for x in health if (x['timestamp_ns']-origin)/1e9 >= 10]
    a, b = steady_health[0], steady_health[-1]
    rate = 8*(b['outputs']['transport']['wire_bytes']-a['outputs']['transport']['wire_bytes'])/(b['timestamp_ns']-a['timestamp_ns'])*1000
    duration = (end['timestamp_ns'] - manifest['clock_origin_ns']) / 1e9
    requested = manifest['configuration']['duration_seconds']
    starts = [x['timestamp_ns'] for x in r['health'] if x.get('component') == 'session' and x.get('event') == 'started']
    stops = [x['timestamp_ns'] for x in r['health'] if x.get('component') == 'camera' and x.get('event') == 'stopped']
    capture_duration = (min(stops) - starts[-1]) / 1e9 if starts and stops else None
    faults = []
    if r.get('error') or r['capture_exit'] != 0 or r['sink_exit' if camera_only else 'sender_exit'] != 0 or end['failed'] or end['signal']:
        faults.append('process/session failure')
    if duration < requested or capture_duration is None or capture_duration < requested:
        faults.append('capture shorter than requested duration')
    if camera_only:
        if r['sink']['bytes'] != end['outputs']['transport']['wire_bytes']:
            faults.append('capture/UDP sink byte count mismatch')
    else:
        if h['status'] != 'complete' or h['error'] is not None:
            faults.append('sender did not complete normally')
        if h['transfer_uncertain']:
            faults.append('SPI transfer outcome uncertain')
        if pv['selftest'] != 0 or pv['nop'] != 0 or p['tables_ok'] != 1:
            faults.append('Pico mode, message type or coefficient verification differs')
        if h['messages'] <= 0 or h['messages'] != pv['msgs_ok']:
            faults.append('message count mismatch')
        if int(h['crc_chain'], 16) != pv['crc_chain']:
            faults.append('CRC chain mismatch')
        if h['payload_bytes'] != 188 * h['ts_packets']:
            faults.append('payload bytes/TS packet arithmetic mismatch')
        if not native:
            if h['udp']['receive_buffer_below_spec'] or not h['udp']['kernel_drop_monitor']:
                faults.append('UDP receive buffer or drop monitor unavailable')
            if h['udp'].get('kernel_pending_on_close', False):
                faults.append('unread UDP datagrams at shutdown')
        if h['ts_packets'] != pv['ts_packets']:
            faults.append('TS packet count mismatch')
        received=h['payload_bytes'] if native else h['udp']['bytes_received']
        if not h['payload_bytes'] == received == end['outputs']['transport']['wire_bytes']:
            faults.append('capture/UDP/SPI byte count mismatch')
        for keys, data in [(['bad_hdr','bad_crc','bad_sync','lost','short','long','overflows'], pv),
                           (['underruns','own_errors','txstalls','link_overruns'], p),
                           (['pending_payload_bytes'], h) if native else (['buffered_unsent','invalid_datagrams','kernel_drops','queue_overflows'], h['udp'])]:
            faults.extend(k for k in keys if data[k])
    transport = end['outputs']['transport']
    if transport['failed'] or transport['dropped_packets'] or transport['datagram_errors']:
        faults.append('capture transport failed or dropped packets')
    encoded_counts = {c['camera']: c['encoded_frames_total'] for c in cams}
    if transport['video_packets'] != sum(encoded_counts.values()):
        faults.append('encoded video/transport packet count mismatch')
    for camera, recorder in end['outputs']['recorders'].items():
        if recorder['failed'] or recorder['dropped_packets'] or recorder['written_packets'] != encoded_counts[camera]:
            faults.append(f'{camera} recorder failed or lost packets')
    summary = {'label': directory.name, 'requested_duration_s': requested, 'observed_session_duration_s': duration,
               'observed_capture_duration_s': capture_duration, 'capture_signal': end['signal'], 'termination_reason': r.get('error'),
               'implementation': 'native' if native else ('camera-only' if camera_only else 'python'),
               'encoder_input': manifest['configuration'].get('encoder_input'),
               'allocator': manifest['configuration']['capture_allocator'], 'encoder_threads': manifest['configuration']['encoder_threads'],
               'width': manifest['configuration']['width'], 'height': manifest['configuration']['height'],
               'measured_transport_mbps': rate, 'messages': h['messages'] if h else None,
               'payload_bytes': h['payload_bytes'] if h else r['sink']['bytes'],
               'sequence_wraps': h['messages']//65536 if h else None,
               'sender_crc_chain': h['crc_chain'] if h else None,
               'pico_crc_chain': f"0x{pv['crc_chain']:08x}" if pv else None,
               'failures': faults, 'pico_underruns': p['underruns'] if p else None, 'cameras': cams,
               'max_temperature_c': max(x['temperature_c'] for x in health),
               'mean_cpu_busy_percent_after_10s': statistics.mean(x['cpu_busy_percent'] for x in steady_health),
               'throttled_bits_observed': sorted(set(x['throttled_bits'] for x in health)),
               'scope': ('Camera-only local UDP control, no SPI. ' if camera_only else 'Real-camera wired digital test, no RF. ') + 'Steady camera statistics exclude the first 10 seconds.'}
    (directory / 'comparison.json').write_text(json.dumps(summary, indent=2)+'\n')
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directories', nargs='+', type=Path)
    p.add_argument('--camera-only', action='store_true', help='Analyze an explicit local UDP-discard control, without Pico claims')
    args = p.parse_args()
    failed = False
    for directory in args.directories:
        result = analyze(directory, camera_only=args.camera_only)
        print(json.dumps(result, indent=2))
        failed |= bool(result['failures'])
    sys.exit(int(failed))
