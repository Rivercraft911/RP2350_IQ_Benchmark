"""Two-core APSK component figures from complete, independently checked60s runs.

Only runtime mapping/shaping/output is measured; no FEC, SPI or RF throughput.
Sources and source archives are hash-checked before any figure is written.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tarfile
import tempfile

import numpy as np
import apsk_live_trial as trial

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT/'results/apsk-dual/2026-10-15'
SOURCE16 = CAMPAIGN/'dual16-soak-1'
SOURCE32 = CAMPAIGN/'dual32-offset-soak-1'
LEGACY16_UF2 = '53d8755b8e51ca787e74ba6e247202e08662df9a6a49c4ad9d5e2129f5101bf5'
REFERENCE_FILES = ('host/iqbench.py', 'host/dual_trial.py', 'host/iq32out_trial.py',
                   'host/iq16out_trial.py', 'host/test_iq32native8.py',
                   'host/test_iq16native8.py', 'host/test_iq16chunk.py',
                   'reference/iqlut.py', 'reference/gen_coeffs.py')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path):
    return str(path.resolve().relative_to(ROOT))


def integer(value):
    return type(value) is int and value >= 0


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_row(row, modulation, git, legacy16=False):
    require(row.get('passed') is True and row.get('classification') == 'clean' and
            row.get('faults') == [] and not row.get('error'), 'run is not positively qualified')
    clock, cpw = row.get('clk_hz'), row.get('cpw')
    require(type(clock) is int and clock in (128000000, 150000000) and type(cpw) is int and cpw in (4, 5), 'unexpected clock/output divisor')
    profile = trial.profile_fields(modulation, 0 if modulation == 16 else 3,
                                   0 if modulation == 16 else 1)
    if legacy16:
        require(modulation == 16, 'legacy exception applies only to pinned16APSK')
        for field in ('placement', 'input_format', 'mapper_code_placement'):
            require(field not in row, 'pinned legacy schema changed')
            del profile[field]
    expected = dict(cmd='benchapsklive', git=git, ms=60000, schedule=2, L=8, sps=2,
                    outer_radius=1, source_symbols=8192, seed=0x1234abcd,
                    history='cyclic_source_tail', runtime_encode_included=True,
                    idle_wait='timer_poll_1us', interrupts_enabled=True,
                    prefill_blocks=4, block_symbols=1024, out_blocks=4, out_words=2048,
                    cap_words=4096, cap_stride=1, cap_search_symbols=4128, **profile)
    require(all(row.get(key) == value for key, value in expected.items()), 'waveform/mode/profile mismatch')
    for key in ('errors', 'underruns', 'own_errors', 'idle_sent', 'txstalls'):
        require(type(row.get(key)) is int and row[key] == 0, f'output fault: {key}')
    for key in ('elapsed_us', 'blocks', 'generated_blocks', 'done_blocks',
                'cap_symbol_cursor', 'cap_done_blocks', 'cap_crc'):
        require(integer(row.get(key)), f'invalid integer statistic: {key}')
    elapsed, blocks = row['elapsed_us'], row['blocks']
    require(60000000 <= elapsed <= 60003000 and blocks > 0, 'short or invalid runtime')
    rate, budget = clock/(4*cpw), elapsed*clock/1e6
    require(finite(row.get('sym_rate')) and math.isclose(row['sym_rate'], rate, abs_tol=.11),
            'symbol rate/divisor mismatch')
    require(row['generated_blocks'] == blocks+4 and
            0 <= row['generated_blocks']-row['done_blocks'] <= 4, 'output credit/accounting mismatch')
    require(abs(row['done_blocks']*1024-rate*elapsed/1e6) <= 3072, 'DMA rate accounting mismatch')
    cores = row.get('cores')
    require(type(cores) is list and len(cores) == 2 and [c.get('core') for c in cores] == [0, 1],
            'per-core records missing')
    for core in cores:
        for key in ('blocks', 'map_calls', 'shape_calls', 'map_cycles', 'shape_cycles', 'work_cycles'):
            require(integer(core.get(key)), f'invalid per-core statistic: {key}')
        require(core['map_calls'] == core['shape_calls'] == core['blocks'] > 0,
                'alternating producer count mismatch')
        require(core['work_cycles'] >= core['map_cycles']+core['shape_cycles'],
                'included work exceeds total work')
        require(finite(core.get('work_busy')) and 0 < core['work_busy'] <= 1.001 and
                math.isclose(core['work_busy'], core['work_cycles']/budget, abs_tol=2e-6),
                'per-core work fraction mismatch')
        for stage in ('map', 'shape'):
            measured = core[f'{stage}_cycles']/(core[f'{stage}_calls']*1024)
            require(measured > 0 and finite(core.get(f'{stage}_cyc_per_sym')) and
                    math.isclose(core[f'{stage}_cyc_per_sym'], measured, abs_tol=.00011),
                    f'{stage} timing/accounting mismatch')
            worst = core.get(f'worst_{stage}_cyc_per_sym')
            require(finite(worst) and worst+.0002 >= measured, f'invalid worst {stage} block')
        if legacy16:
            require('shaper_address' not in core, 'pinned legacy shaper schema changed')
        else:
            address = core.get('shaper_address')
            lower = 0x20081000 if core['core'] == 0 else 0x20080000
            require(type(address) is int and address & 1 and lower <= (address & ~1) < lower+2048,
                    'shaper address differs from per-core SRAM declaration')
    require(sum(c['blocks'] for c in cores) == blocks and
            abs(cores[0]['blocks']-cores[1]['blocks']) <= 2, 'per-core aggregate count mismatch')
    cursor = row['cap_symbol_cursor']
    require(cursor % 1024 == 0 and 0 <= cursor//1024-row['cap_done_blocks'] <= 4 and
            abs(cursor-rate*60000/2000) <= 8192, 'capture cursor/time credit mismatch')
    require(row.get('cap_transport_ok') is True and row.get('cap_aligned') is True and
            row.get('cap_physical_iq_ok') is True and row.get('cap_mismatches') == 0 and
            row.get('cap_checked_bus_words') == 8192 and not row.get('cap_error'),
            'complete physical capture validation missing')


def validate_capture(row, lines):
    checked = trial.validate_capture(row, lines)
    require(checked.get('cap_transport_ok') is True and checked.get('cap_aligned') is True and
            checked.get('cap_physical_iq_ok') is True and checked.get('cap_mismatches') == 0 and
            checked.get('cap_checked_bus_words') == 8192, 'independent full capture mismatch')
    for field in ('cap_global_bus_offset', 'cap_period_bus_offset', 'cap_start_search_bus_words'):
        require(row.get(field) == checked.get(field), f'independent capture alignment changed: {field}')
    return checked


def build_evidence(folder, manifest):
    binary_hashes = manifest['sha256']
    matches = []
    for candidate in folder.parent.glob('build-*/manifest.json'):
        build = json.loads(candidate.read_text())
        hashes = build.get('sha256', {})
        if all(hashes.get('build/'+name) == binary_hashes.get(name) for name in ('iqbench.uf2', 'iqbench.elf')):
            matches.append((candidate, build))
    require(len(matches) == 1, 'requires one uniquely matching archived build')
    path, build = matches[0]
    require(build.get('git_rev') == '#define GIT_REV \"'+manifest['expected_firmware_git']+'\"',
            'build/campaign firmware identity mismatch')
    files = [path, path.parent/'firmware-source.tar.gz', path.parent/'flash.txt', path.parent/'build.txt']
    require(all(p.is_file() for p in files), 'build/source/flash evidence missing')
    expected_sources = {name: value for name, value in build['sha256'].items() if name.startswith('firmware/')}
    with tarfile.open(files[1], 'r:gz') as archive:
        actual_sources = {entry.name: hashlib.sha256(archive.extractfile(entry).read()).hexdigest()
                          for entry in archive.getmembers() if entry.isfile()}
    require(actual_sources == expected_sources, 'archived firmware source differs from build manifest')
    for name, value in binary_hashes.items():
        if name.startswith('firmware/'):
            require(actual_sources.get(name) == value, 'campaign/source build mismatch: '+name)
    absent = []
    for name in ('iqbench.uf2', 'iqbench.elf'):
        binary = folder/name
        if not binary.is_file():
            # Images are gitignored local copies; a fresh clone records them as unchecked.
            absent.append(name)
            continue
        require(sha(binary) == binary_hashes.get(name), 'binary hash mismatch: '+name)
        files.append(binary)
    flash = files[2].read_text()
    require('Verifying Flash:' in flash and '\n  OK\n' in flash and
            'The device was rebooted to start the application.' in flash, 'verified flash record missing')
    return dict(source_files=len(actual_sources), path=relative(path.parent),
                sha256={relative(p): sha(p) for p in files}, absent_local_binaries=absent,
                archived_firmware_sha256=actual_sources)


def load_campaign(folder, modulation, strict_references=True):
    # Rendering requires the exact recorded reference sources. The self-test instead
    # re-validates the archived captures with the current sources and reports drift.
    folder = folder.resolve()
    if folder.is_file():
        require(folder.name == 'runs.jsonl', 'source file must be runs.jsonl')
        folder = folder.parent
    manifest_path, summary_path, runs_path = (folder/name for name in ('manifest.json', 'summary.json', 'runs.jsonl'))
    require(all(p.is_file() for p in (manifest_path, summary_path, runs_path)),
            f'complete campaign is not available: {folder}')
    manifest, summary = json.loads(manifest_path.read_text()), json.loads(summary_path.read_text())
    require(summary.get('passed') is True and summary.get('failures') == [] and summary.get('fatal') is None,
            'campaign summary is not clean')
    legacy16 = modulation == 16
    require(not legacy16 or folder == SOURCE16.resolve(), 'legacy16APSK source is pinned')
    expected = dict(ms=60000, clocks_khz=[128000, 150000], schedules=[2],
                    mappers=[0] if modulation == 16 else [3], modulations=[modulation],
                    expect_deadline_fault=False)
    require(all(manifest.get(k) == value for k, value in expected.items()), 'campaign mode/schedule/duration mismatch')
    require(manifest.get('cpw') in ([4], [5]), 'campaign output divisor is not pinned')
    if legacy16:
        require(manifest['sha256'].get('iqbench.uf2') == LEGACY16_UF2 and 'placements' not in manifest,
                'unknown legacy16APSK firmware/schema')
    else:
        require(manifest.get('placements') == [1], '32APSK requires recorded per-core SRAM placement')
    git = manifest.get('expected_firmware_git')
    require(type(git) is str and bool(git), 'expected firmware identity missing')
    references, drift = {}, []
    for name in REFERENCE_FILES:
        path = ROOT/name
        if manifest['sha256'].get(name) != sha(path):
            require(not strict_references, 'independent reference source drift: '+name)
            drift.append(name)
        references[name] = sha(path)
    evidence = build_evidence(folder, manifest)
    selected = []
    for number, line in enumerate(runs_path.read_text().splitlines(), 1):
        row = json.loads(line)
        if row.get('cmd') != 'benchapsklive':
            require(not row.get('exception'), 'campaign includes a host exception')
            continue
        validate_row(row, modulation, git, legacy16)
        require(row['cpw'] == manifest['cpw'][0], 'run divisor differs from campaign')
        capture = folder/row['capture_file']
        require(capture.parent == folder and capture.is_file(), 'capture file unavailable')
        checked = validate_capture(row, capture.read_text().splitlines())
        selected.append(dict(row=row, source=dict(path=relative(runs_path), line=number, sha256=sha(runs_path)),
                             capture=dict(path=relative(capture), sha256=sha(capture), validation=checked)))
    require(len(selected) == 2 and {entry['row']['clk_hz'] for entry in selected} == {128000000, 150000000},
            'requires exactly one qualified60s run at each stock clock')
    selected.sort(key=lambda entry: entry['row']['clk_hz'])
    return dict(runs=selected, legacy_missing_fields=['placement', 'input_format', 'mapper_code_placement',
                 'cores[].shaper_address'] if legacy16 else [],
                manifest=dict(path=relative(manifest_path), sha256=sha(manifest_path)),
                summary=dict(path=relative(summary_path), sha256=sha(summary_path)), build=evidence,
                reference_sha256=references, reference_drift=drift,
                recorded_driver_sha256=manifest['sha256']['host/apsk_live_trial.py'])


def self_test():
    selected = load_campaign(SOURCE16, 16, strict_references=False)
    original = selected['runs'][0]['row']
    cases = 0
    for field, value in (('ms', 3000), ('modulation', '32APSK'), ('mapper', 1), ('errors', 1),
                         ('passed', False), ('classification', 'failed'), ('label_crc', 0),
                         ('table_bytes', 1), ('cap_checked_bus_words', 8191), ('sym_rate', float('nan')),
                         ('cap_symbol_cursor', float('nan'))):
        bad = copy.deepcopy(original)
        bad[field] = value
        try: validate_row(bad, 16, original['git'], True)
        except ValueError: cases += 1
        else: raise AssertionError('accepted '+field)
    for field in ('map_cycles', 'shape_cyc_per_sym', 'work_busy', 'worst_map_cyc_per_sym'):
        bad = copy.deepcopy(original)
        bad['cores'][0][field] = float('nan')
        try: validate_row(bad, 16, original['git'], True)
        except ValueError: cases += 1
        else: raise AssertionError('accepted per-core NaN '+field)
    modern = copy.deepcopy(original)
    modern.update(trial.profile_fields(32, 3, 1))
    for core in modern['cores']:
        core['shaper_address'] = 0x20081531 if core['core'] == 0 else 0x20080745
    _, _, bus = trial.reference_window(modern['cap_symbol_cursor'], 4128, 32)
    cap, lines = trial.reference.capture_lines(bus[100:100+8192])
    modern.update(cap)
    modern.update(trial.validate_capture(modern, lines))
    validate_row(modern, 32, original['git'])
    validate_capture(modern, lines)
    for field, value in (('mapper', 2), ('placement', 0), ('input_format', 'rolling_radix17_triples')):
        bad = copy.deepcopy(modern)
        bad[field] = value
        try: validate_row(bad, 32, original['git'])
        except ValueError: cases += 1
        else: raise AssertionError('accepted modern '+field)
    for altered in (lines[1:], [line.replace('@cap 0 ', '@cap 16 ', 1) for line in lines]):
        try: validate_capture(modern, altered)
        except ValueError: cases += 1
        else: raise AssertionError('accepted malformed capture')
    bad_bus = bus[100:100+8192].copy()
    bad_bus[5000] ^= 1
    bad_cap, bad_lines = trial.reference.capture_lines(bad_bus)
    corrupted = dict(modern, **bad_cap)
    try: validate_capture(corrupted, bad_lines)
    except ValueError: cases += 1
    else: raise AssertionError('accepted wrong waveform with matching transport CRC')
    manifest = json.loads((SOURCE16/'manifest.json').read_text())
    for field in ('iqbench.uf2', 'firmware/src/bench/bench_apsk_live.c'):
        bad_manifest = copy.deepcopy(manifest)
        bad_manifest['sha256'][field] = '0'*64
        try: build_evidence(SOURCE16, bad_manifest)
        except ValueError: cases += 1
        else: raise AssertionError('accepted source/binary drift '+field)
    print(json.dumps(dict(self_test='passed', negative_controls=cases,
                          reference_drift_since_campaign=selected['reference_drift'],
                          real_legacy_runs_revalidated=2, synthetic_modern_metadata_only=True,
                          capture_observations_per_run=8192)))


def render(source32, out):
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir())/'yapogee-mpl-cache'))
    sys.path.insert(0, str(ROOT/'reference'))
    from plotstyle import BLUE, GREEN, INK, RED, WIDTH, dots, plt, save_figure
    selected = {16: load_campaign(SOURCE16, 16), 32: load_campaign(source32, 32)}
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(WIDTH, 2.8),
                                gridspec_kw={'width_ratios': [1, 1.3]})
    for modulation, color, marker, linestyle in ((16, BLUE, 's', '-'), (32, RED, 'o', '--')):
        rows = [entry['row'] for entry in selected[modulation]['runs']]
        dots(ax, [r['clk_hz']/1e6 for r in rows], [r['sym_rate']/1e6 for r in rows],
             color, f'{modulation}APSK', marker=marker, linestyle=linestyle)
    points = {(entry['row']['clk_hz']/1e6, entry['row']['sym_rate']/1e6)
              for campaign in selected.values() for entry in campaign['runs']}
    for clock, rate in sorted(points):
        ax.annotate(f'{rate:g}', (clock, rate), xytext=(0, 5),
                    textcoords='offset points', ha='center', fontsize=7.5)
    ax.set(xlim=(123, 155), xticks=[128, 150], ylim=(0, 11),
           xlabel='System clock (MHz)', ylabel='Symbol rate (Msym/s)')
    ax.set_title('(a) Sustained output')
    ax.legend(loc='lower right')
    ax.grid(axis='y')
    rows150 = [next(entry['row'] for entry in selected[m]['runs'] if entry['row']['clk_hz'] == 150000000)
               for m in (16, 32)]
    stages = []
    for row in rows150:
        budget = row['elapsed_us']*row['clk_hz']/1e6
        for core in row['cores']:
            stages.append([100*core['map_cycles']/budget, 100*core['shape_cycles']/budget,
                           100*(core['work_cycles']-core['map_cycles']-core['shape_cycles'])/budget])
    stages = np.array(stages)
    bottom = np.zeros(4)
    for index, color, label in ((0, BLUE, 'Map'), (1, RED, 'Shape'), (2, GREEN, 'Control')):
        bx.bar(np.arange(4), stages[:, index], bottom=bottom, color=color,
               edgecolor=INK, linewidth=.4, width=.6, label=label, zorder=3)
        bottom += stages[:, index]
    for index, value in enumerate(bottom):
        bx.annotate(f'{value:.1f}', (index, value), xytext=(0, 4),
                    textcoords='offset points', ha='center', fontsize=7.5)
    bx.set(xticks=np.arange(4), xticklabels=['16APSK\nC0', '16APSK\nC1', '32APSK\nC0', '32APSK\nC1'],
           ylim=(0, 114), yticks=[0, 25, 50, 75, 100], ylabel='Instrumented work (%)')
    bx.set_title('(b) Per-core work, 150 MHz')
    bx.legend(loc='upper left', ncol=3, fontsize=7, handlelength=1.2, columnspacing=1)
    bx.set_axisbelow(True)
    bx.grid(axis='y')
    out.mkdir(parents=True, exist_ok=True)
    save_figure(fig, out/'apsk_dual.png')
    record = dict(schema='yapogee-apsk-dual-figures-v1', selected=selected,
                  caption='Two-core alternating1024-symbol blocks; complete60s runtime mapping, shaping and GPIO output. '
                          '16APSK uses native344/flash;32APSK uses native333 with byte-offset keys and per-core SRAM. '
                          'No FEC, live SPI, AFE or RF qualification. These are the qualified points in the pinned campaigns.',
                  formulas=dict(rate='clk_hz/(4*cpw)', per_core_pct='100*stage_cycles/(elapsed_us*clk_hz/1e6)',
                                control='work_cycles-map_cycles-shape_cycles; includes publication and call/control overhead; timer polling excluded'),
                  uncertainty='Recorded finite-run averages; no inferred confidence intervals or additional throughput headroom.',
                  validation='All8192 contiguous GPIO observations independently revalidated per run; no capture/profile exemptions for legacy16APSK.',
                  style=dict(path='reference/plotstyle.py', sha256=sha(ROOT/'reference/plotstyle.py')),
                  script_sha256=sha(Path(__file__)), capture_validator_sha256=sha(ROOT/'host/apsk_live_trial.py'),
                  per_core150_work_pct=stages.tolist(),
                  output_sha256={f'apsk_dual.{extension}': sha(out/f'apsk_dual.{extension}') for extension in ('png', 'svg', 'pdf')})
    (out/'selection.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps(dict(output=str(out), figures=['apsk_dual.png', 'apsk_dual.svg', 'apsk_dual.pdf'],
                          provenance='selection.json')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source32', type=Path, default=SOURCE32)
    parser.add_argument('--out', type=Path, default=ROOT/'results/plots/apsk-dual-2026-10-15')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        render(args.source32, args.out)


if __name__ == '__main__':
    main()
