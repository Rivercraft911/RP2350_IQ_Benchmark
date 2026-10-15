"""Repeat the tightest configuration; run with a NEW output-directory argument."""
import datetime
import hashlib
import json
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'host'))
import dual_trial as trial
import iqbench as iq

def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'runs.jsonl').exists() or (out / 'manifest.json').exists():
        raise SystemExit('choose a new output directory')
    original = ROOT / 'results/dual-dma/2026-10-14/qualify-1/manifest.json'
    manifest = json.loads(original.read_text())
    # Firmware, test runner and model must match the qualified image. Plot revisions do not
    # drive hardware or validation and are deliberately excluded from this assertion.
    for name, expected in manifest['sha256'].items():
        if name == 'host/plot_dual.py':
            continue
        path = ROOT / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    manifest.update(current_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    qualified_source_manifest=str(original.relative_to(ROOT)),
                    started=datetime.datetime.now().isoformat(), profile='repeat-16',
                    clocks_khz=[128000], duration_ms=60000,
                    reproduction_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    iq.LOGGING = False
    board = iq.Board()
    try:
        for command in ('info', 'clock 128000', 'bchkernel 2', 'dualwriters 4', 'dualprio 0'):
            response, _ = board.cmd(command)
            assert 'error' not in response, response
            if command == 'info':
                assert response.get('git') == manifest['head'][:10], response
        r, lines = board.cmd('dualstream 1 60000 2 4 4096 0 0 0 2', timeout=125)
        r.update(trial.validate_capture(r, lines, 4))
        r['faults'] = trial.stream_faults(r, True, 4)
        for key, expected in dict(clk_hz=128000000, row_symbols=2, writers=4, source=4,
                                  divider256=256, schedule=0, bus_priority_mask=0,
                                  split_bb_stride_words=64, split_bch_slice_words=128).items():
            if r.get(key) != expected:
                r['faults'].append(f'{key}: {r.get(key)} != {expected}')
        if r.get('elapsed_us', 0) < 60000000:
            r['faults'].append('duration shorter than requested')
        r.update(label='dual-row2-128000-src4-k2-cpw1-fault0-writers4-repeat60',
                 expectation='clean', recorded_at=datetime.datetime.now().isoformat(),
                 classification='failed' if r['faults'] else 'clean',
                 capture_file='capture.txt', requested_schedule=0, requested_divider256=0)
        (out / 'capture.txt').write_text('\n'.join(lines) + '\n')
        (out / 'runs.jsonl').write_text(json.dumps(r) + '\n')
        print(json.dumps(r, indent=2), flush=True)
        assert not r['faults'], r['faults']
    finally:
        for command in ('dualprio 0', 'dualwriters 2', 'clock 128000', 'bchkernel 0'):
            board.cmd(command)
        board.s.close()

if __name__ == '__main__':
    main()
