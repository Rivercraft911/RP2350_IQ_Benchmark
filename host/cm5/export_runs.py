"""Combine checked-in comparisons into one CSV for plotting."""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'results/cm5-spi'


def main():
    comparisons = [json.loads(p.read_text()) for p in sorted(DATA.glob('*/comparison.json'))]
    runs = [r for r in comparisons if 'observed_capture_duration_s' in r]
    rows = []
    for run in runs:
        for cam in run['cameras']:
            rows.append({
                'run': run['label'], 'camera': cam['camera'],
                'requested_seconds': run['requested_duration_s'],
                'capture_seconds': run['observed_capture_duration_s'],
                'allocator': run['allocator'], 'encoder_threads': run['encoder_threads'],
                'width': run['width'], 'height': run['height'],
                'encoded_fps_after_10s': cam['steady_encoded_fps'],
                'delivered_fps_after_10s': cam['steady_delivered_fps'],
                'h264_payload_mbps_after_10s': cam['steady_h264_payload_mbps'],
                'intervals_over_50ms': cam['delivered_intervals_over_50ms'],
                'encoder_ms': cam['mean_interval_encoder_ms'],
                'capture_drops': sum(cam['all_drops'].values()),
                'steady_capture_drops': sum(cam['steady_drops'].values()),
                'ts_mbps': run['measured_transport_mbps'],
                'spi_messages': run['messages'], 'payload_bytes': run['payload_bytes'],
                'sequence_wraps': run['sequence_wraps'],
                'sender_crc_chain': run['sender_crc_chain'], 'pico_crc_chain': run['pico_crc_chain'],
                'pico_underruns': run['pico_underruns'],
                'maximum_temperature_c': run['max_temperature_c'],
                'mean_total_cpu_percent': run['mean_cpu_busy_percent_after_10s'],
                'termination_reason': run['termination_reason'],
                'failures': '; '.join(run['failures']), 'scope': run['scope'],
            })
    with (DATA/'runs.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    path = DATA/'summary.json'
    summary = json.loads(path.read_text())
    summary['live_runs'] = runs
    summary['state'] = 'Tests stopped. Camera-only control completed; 600-second SPI attempt stopped at 80 C after 210 seconds. No RF hardware used.'
    path.write_text(json.dumps(summary, indent=2)+'\n')


if __name__ == '__main__':
    main()
