"""Four scientific figures from pinned waveform campaigns; never accesses a Pico.

python3 host/plot_waveforms.py --self-test
python3 host/plot_waveforms.py --check
python3 host/plot_waveforms.py --out results/plots/waveforms-2026-10-15

Writes PNG/SVG/PDF plus waveform_selection.json with exact source rows, hashes,
image provenance and derived formulas. Full-pipeline plots require clean60s runs;
component timings and the explicitly failed SRAM control have separate selectors.
No confidence intervals are inferred from single trials or firmware averages.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PATHS = {
    'pulse': 'results/pulse-design/2026-10-15/pico-matched-soak-1/runs.jsonl',
    'eight': 'results/8psk-live/2026-10-15/bch-placement-soak-1/runs.jsonl',
    'vcm': 'results/vcm/2026-10-15/soak-1/runs.jsonl',
    'map_old': 'results/higher-mapping/2026-10-15/first-1/runs.jsonl',
    'map_new': 'results/higher-mapping/2026-10-15/fast-1/runs.jsonl',
    'sixteen_l9': 'results/16apsk-chunk/2026-10-15/burst-1/runs.jsonl',
    'sixteen_l8': 'results/16apsk-chunk/2026-10-15/native-l8-1/runs.jsonl',
    'a1': 'results/memory-placement/2026-10-15/a1-ramlow/runs.jsonl',
    'b': 'results/memory-placement/2026-10-15/b-scratchy/runs.jsonl',
    'a2': 'results/memory-placement/2026-10-15/a2-ramlow/runs.jsonl',
    'pointer_old': 'results/quad-pointers/2026-10-15/live-soak-1/runs.jsonl',
    'pointer_new': 'results/quad-pointers/2026-10-15/poll128-soak-1/runs.jsonl',
}
COLORS = {'qpsk': '#326b91', 'eight': '#c05b32', 'vcm': '#756098',
          'teal': '#17847b', 'gray': '#7c8995', 'bad': '#b7484c'}
FAULTS = ('errors', 'underruns', 'own_errors', 'idle_sent', 'txstalls',
          'txstalls_i', 'txstalls_q', 'input_overruns')
PV_FAULTS = ('lost', 'bad_hdr', 'bad_crc', 'bad_sync', 'short_msgs', 'long_msgs',
             'overflows', 'gcs_underflows', 'drain_timeouts', 'late_closes')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    """Keep provenance with each immutable raw row, not only with a chosen label."""
    full = ROOT/path
    rows = []
    for line, text in enumerate(full.read_text().splitlines(), 1):
        if text.strip():
            row = json.loads(text)
            row['_source'] = dict(path=path, line=line, file_sha256=digest(full))
            rows.append(row)
    return rows


def clean_run(row, milliseconds=60000):
    if (row.get('classification') not in (None, 'clean') or row.get('passed') is False or
            (row.get('classification') is None and row.get('passed') is not True) or
            row.get('host_crc_ok') is False or row.get('error') or row.get('faults') or row.get('cap_reference_failed') is True or row.get('fault', 0) != 0 or
            row.get('ms', 0) < milliseconds or row.get('elapsed_us', 0) < milliseconds*1000 or
            any(row.get(key, 0) for key in FAULTS)):
        return False
    if not (row.get('cap_transport_ok') is True and row.get('cap_aligned') is True and
            row.get('cap_words') == 4096 and row.get('cap_checked_bus_words') == 8192 and
            row.get('cap_mismatches') == 0 and not row.get('cap_error') and
            len(row.get('cap_axis_counts', [])) == 2 and min(row['cap_axis_counts']) >= 2048):
        return False
    stats = row.get('pv_spi', {})
    return (stats.get('bb_mode') == 'gcs' and stats.get('ok', 0) > 24 and
            stats.get('runtime_bbframes', 0) > 0 and stats.get('runtime_messages', 0) > 0 and
            len(stats.get('bb_crc', [])) == 4 and
            all(stats.get(key, -1) == 0 for key in PV_FAULTS) and
            stats.get('gcs_bytes') == stats.get('bbframes', 0)*6720)


def clean_pipeline(row, kind, milliseconds=60000):
    if not clean_run(row, milliseconds):
        return False
    common = (row.get('code_index') == 8 and row.get('kbch') == 53840 and
              row.get('s2_code') == 'normal 5/6' and row.get('row_symbols') == 4)
    if not all(np.isfinite(row.get(k, float('nan'))) for k in ('sym_rate', 'elapsed_us', 'gen_busy', 'gen_cyc_per_sym')):
        return False
    if row.get('cmd') in ('bench8live', 'benchvcm') and not np.isfinite(row.get('bb_payload_bps', float('nan'))):
        return False
    if not common:
        return False
    if kind == 'qpsk':
        return (row.get('cmd') == 'dualstream' and row.get('source') == 6 and
                row.get('sps') == 4 and row.get('L') in (8, 9) and row.get('sym_rate') == 16e6 and
                row.get('clk_hz') == 128e6 and row.get('s2_runtime_frames', 0) > 0 and
                row.get('split_bch_frames', 0) > 0 and row.get('tables_ok') == 1 and
                row.get('row_profile') == ('l9_quad' if row['L'] == 9 else 'qpsk_l8_n4_both_m010_far2'))
    if kind in ('eight', 'vcm'):
        return (row.get('cmd') == ('bench8live' if kind == 'eight' else 'benchvcm') and
                row.get('sps') == 2 and row.get('L') == 9 and row.get('pilots') is True and
                row.get('gold') == 0 and row.get('rotation_pi') == .125 and
                row.get('clk_hz') in (128e6, 150e6) and row.get('cpw') == 4 and
                row.get('sym_rate') == row['clk_hz']/16 and row.get('encoder_runtime_frames', 0) > 0 and
                row.get('table_crc_a') == 0xcd2d4635 and row.get('table_crc_b') == 0x135bd352 and
                (kind != 'vcm' or (row.get('cap_transition_observed') is True and
                 row.get('waveform_plan') == '8psk_qpsk_alternating' and
                 row.get('qpsk_frames', 0) > 0 and row.get('psk8_frames', 0) > 0)))
    return False


def clean_component(row, kind):
    if (row.get('classification') not in (None, 'clean') or row.get('passed') is False or
            row.get('error') or row.get('faults') or any(row.get(k, 0) for k in FAULTS) or row.get('fault', 0)):
        return False
    if row.get('clk_hz') != 128000000:
        return False
    if kind == 'map':
        return (row.get('cmd') == 'bench8map' and row.get('code_index') == 8 and row.get('pilots') == 1 and
                row.get('gold') == 0 and row.get('frame_symbols') == 22194 and row.get('reps', 0) >= 8 and
                row.get('crc_ok') is True and row.get('symbols_ok') is True and
                row.get('crc') == row.get('expected_crc') == 1814858131 and np.isfinite(row.get('map_cyc', 0)) and row.get('map_cyc', 0) > 0)
    if kind in ('l9', 'l8'):
        expected = (2951358184, 606382727) if kind == 'l9' else (2596089319, 4290221674)
        names = {'reverse_bits', 'reverse_map', 'burst_map'} if kind == 'l9' else {'native_c', 'native_burst'}
        kernels = row.get('kernels', [])
        return (row.get('cmd') == ('bench16chunk' if kind == 'l9' else 'bench16native8') and
                row.get('L') == (9 if kind == 'l9' else 8) and row.get('sps') == 2 and
                row.get('gamma100') == 270 and row.get('input_crc') == 787286031 and
                row.get('code_placement') == 'flash_cached' and row.get('reps', 0) >= 8 and
                {k.get('name') for k in kernels} == names and all(k.get('crc_ok') is True and
                 (k.get('raw_crc'), k.get('packed_crc')) == expected and np.isfinite(k.get('cyc_per_sym', 0)) and k.get('cyc_per_sym', 0) > 0 for k in kernels))
    return False


def unique(rows, predicate, label):
    selected = [row for row in rows if predicate(row)]
    if len(selected) != 1:
        raise ValueError(f'{label}: expected exactly one eligible record, found{len(selected)}')
    return selected[0]


def clean_pointer(row, policy):
    """Metadata gate; raw samples and the frozen campaign validator are checked too."""
    try:
        clock = row['clk_hz']
        expected = dict(cmd='quadlive', source=1, sps=4, L=8,
            row_profile='qpsk_l8_n4_both_m010_far2', filter_beta=None,
            row_symbols=4, writers=2, schedule=0, bus_priority_mask=0, cpw=1,
            divider256=256, ms=60000, fault=0, seed=305441741, in_words=8192,
            previous=2266577571, input_crc=1401002126, input_after_crc=1401002126,
            table_bytes=65536, table_crc=3433922394, reason='complete',
            restored=True, selectors_restored=True, core1_idle=True,
            descriptor_gen_busy=0.0, sample_math_busy=0.0, idle_wait='spin',
            poll_interval_us=24 if policy == 'old' else 128,
            read_beyond_grants=False, grants_frozen=False, frozen_granted_words=0,
            cap_stride=17, cap_divider256=256, cap_fifo_only=False,
            cap_words=4096, capture_count=4096, capture_started=True,
            cap_aligned=True, cap_mismatches=0, cap_checked_bus_words=8192,
            cap_transport_ok=True, cap_reference_failed=False,
            classification='digital_live_clean')
        if policy not in ('old', 'new') or clock not in (128000000, 150000000):
            return False
        if any(row.get(k) != v for k, v in expected.items()):
            return False
        if (row.get('passed') is False or row.get('faults') or row.get('error') or row.get('cap_error') or
                any(row.get(k, -1) != 0 for k in ('errors', 'pointer_errors', 'live_pointer_errors',
                    'output_errors', 'txstalls_i', 'txstalls_q', 'pointer_txstall_mask', 'null_trigger_mask'))):
            return False
        fields = ('sym_rate', 'elapsed_us', 'supervision_cycles', 'supervision_busy',
                  'core0_work_busy', 'polls', 'max_poll_gap_us', 'max_supervision_cycles')
        if not all(np.isfinite(row[k]) for k in fields):
            return False
        elapsed = row['elapsed_us']
        fraction = row['supervision_cycles']/(elapsed*clock/1e6)
        if not (60000000 <= elapsed <= 60001000 and row['sym_rate'] == clock/8 and
                0 < fraction < 1 and row['polls'] > 0 and
                all(abs(row[k]-fraction) <= 1e-6 for k in ('supervision_busy', 'core0_work_busy'))):
            return False
        if policy == 'new':
            budget = 8 + (clock*257 + 49000000-1)//49000000
            return (row.get('max_poll_interval_us') == 256 and
                row.get('poll_issue_budget_words') == budget and
                row.get('required_credit_runway_words') == 2*budget and
                row.get('source_cursor_deadline_us') == 8184*49000000//clock and
                np.isfinite(row['min_credit_runway_words']) and row['min_credit_runway_words'] >= 2*budget and
                np.isfinite(row['max_source_snapshot_span_us']) and 0 < row['max_source_snapshot_span_us'] <= 256 and
                128 <= row['max_poll_gap_us'] <= 256)
        return 24 <= row['max_poll_gap_us'] <= 32
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False


def pointer_evidence(row, policy, lines=None):
    """Re-run each campaign's pinned validator on its complete raw capture."""
    folder = (ROOT/row['_source']['path']).parent
    manifest = json.loads((folder/'manifest.json').read_text())
    assert manifest['ms'] == 60000 and manifest['cpws'] == [1] and manifest['faults'] == [0]
    assert manifest['reps'] == 1 and manifest['clocks_khz'] == [128000, 150000]
    assert manifest['expected_firmware_git'] == row['git']
    driver = folder/'driver.py'
    assert digest(driver) == manifest['sha256']['host/quadlive_trial.py']
    for name in ('iqbench.uf2', 'iqbench.elf'):
        assert digest(folder/name) == manifest['sha256'][name]
    capture = folder/f'capture-{row["clk_hz"]//1000}-1-0-0.txt'
    spec = importlib.util.spec_from_file_location('frozen_quadlive_'+policy, driver)
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    result = validator.validate_run(row, capture.read_text().splitlines() if lines is None else lines, 1, 60000, 0,
                                    row['clk_hz'], manifest['expected_firmware_git'])
    assert result.get('classification') == 'digital_live_clean' and not result.get('faults'), result
    assert result['cap_checked_bus_words'] == 8192 and result['cap_mismatches'] == 0
    assert result['cap_offset'] == row['cap_offset']
    files = [capture, driver, folder/'manifest.json', folder/'firmware-source.tar.gz']
    files += [ROOT/p for p in ('host/dual_trial.py', 'host/iqbench.py', 'host/pulse_profiles.py',
                              'reference/iqlut.py', 'reference/gen_coeffs.py')]
    return dict(validation='Pinned campaign validator re-run on all8192 raw GPIO observations; finite metadata gate added',
                capture=result, file_sha256={str(p.relative_to(ROOT)): digest(p) for p in files},
                snapshot_span=dict(observed_us=row.get('max_source_snapshot_span_us'),
                    note='Old24us campaign records begin gaps/body cycles, not a source-snapshot span or minimum credit runway'),
                credit_proof=dict(pio_cycles_per_word=49, reserve_words=8, timer_quantization_us=1,
                    budget_formula='8+ceil(clk_hz*(256+1)/(49*1e6))', runway_formula='2*budget',
                    qualification='Both-axis snapshot span includes current and previous call timing; finite zero sentinel gates unpublished reads') if policy == 'new' else None)


def select():
    loaded = {name: load(path) for name, path in PATHS.items()}
    selected = {}
    for span in (8, 9):
        selected[f'qpsk_l{span}'] = unique(loaded['pulse'], lambda r: clean_pipeline(r, 'qpsk') and r['L'] == span, f'QPSK L{span}')
    for kind in ('eight', 'vcm'):
        for clock in (128, 150):
            selected[f'{kind}_{clock}'] = unique(loaded[kind], lambda r: clean_pipeline(r, kind) and r['clk_hz'] == clock*1e6, f'{kind}{clock}')
    for which in ('old', 'new'):
        selected['map_'+which] = unique(loaded['map_'+which], lambda r: clean_component(r, 'map'), '8PSK mapper '+which)
    for span in ('l9', 'l8'):
        selected['sixteen_'+span] = unique(loaded['sixteen_'+span], lambda r: clean_component(r, span), '16APSK '+span)
    # These are pinned A/B/A diagnostic controls, explicitly separated from the
    # clean performance selector. B is expected to fail and must never enter it.
    for which in ('a1', 'b', 'a2'):
        selected[which] = unique(loaded[which], lambda r: r.get('cmd') == 'bench8live' and r.get('clk_hz') == 128000000, which)
        r = selected[which]
        if which == 'b':
            assert r.get('classification') == 'failed' and r.get('errors') == 2 and r.get('underruns') == 1
            assert r.get('elapsed_us', 3e6) < 3e6 and not clean_pipeline(r, 'eight', 3000)
        else:
            assert clean_pipeline(r, 'eight', 3000)
    comparison = ROOT/'results/memory-placement/2026-10-15/comparison.json'
    assert json.loads(comparison.read_text())['passed'] is True
    for policy in ('old', 'new'):
        for clock in (128, 150):
            row = unique(loaded['pointer_'+policy], lambda r: clean_pointer(r, policy) and
                         r['clk_hz'] == clock*1000000, f'PIO pointer{policy}{clock}')
            row['_validation'] = pointer_evidence(row, policy)
            selected[f'pointer_{policy}_{clock}'] = row
    return selected


def payload(row):
    if row['cmd'] == 'dualstream':
        return row['sym_rate']*(row['kbch']-80)/33282
    return row['bb_payload_bps']


def table_bytes(row):
    assert row['sps'] == 4 and row['row_symbols'] == 4 and row['L'] in (8, 9)
    return (1 << (row['L']+3))*8*4



def vcm_certificate(selected):
    path = ROOT/'results/vcm/2026-10-15/soak-1/revalidation.json'
    if not path.exists():
        raise ValueError('VCM independent revalidation.json is not present yet')
    certificate = json.loads(path.read_text())
    assert certificate.get('passed') is True, 'VCM independent revalidation failed'
    for clock in (128, 150):
        row = selected[f'vcm_{clock}']
        assert certificate.get('expected_firmware_git') == row['git']
        checks = [check for check in certificate.get('runs', [])
                  if check.get('clock_khz') == clock*1000 and check.get('ms') == row['ms']]
        assert len(checks) == 1 and checks[0].get('passed') is True and not checks[0].get('faults')
        capture = checks[0].get('capture', {})
        assert capture.get('cap_global_bus_offset') == row['cap_global_bus_offset']
        assert capture.get('cap_transition_observed') is True
    raw_hashes = certificate.get('raw_sha256', {})
    for name in ('runs.jsonl', 'capture-128000.txt', 'capture-150000.txt'):
        actual = path.parent/name
        matches = [value for key, value in raw_hashes.items() if Path(key).name == name]
        assert len(matches) == 1 and matches[0] == digest(actual), ('VCM certificate raw hash', name)
    return dict(path=str(path.relative_to(ROOT)), sha256=digest(path), certificate=certificate)


def provenance(selected):
    artifacts = {}
    for path in PATHS.values():
        folder = (ROOT/path).parent
        for name in ('manifest.json', 'iqbench.uf2', 'iqbench.elf'):
            p = folder/name
            if p.exists():
                artifacts[str(p.relative_to(ROOT))] = digest(p)
    for row in selected.values():
        artifacts.update(row.get('_validation', {}).get('file_sha256', {}))
    return dict(schema='yapogee-waveform-figures-v1', script_sha256=digest(Path(__file__)),
                style=dict(path='reference/plotstyle.py', sha256=digest(ROOT/'reference/plotstyle.py')),
                vcm_revalidation=vcm_certificate(selected),
                selected=selected, artifact_sha256=artifacts,
                derived=dict(nominal_width_hz='(1+0.20)*sym_rate; nominal RRC width, not measured RF',
                    qpsk_payload_bps='sym_rate*(kbch-80)/33282; BB data excludes80-bit BBHEADER',
                    psk8_vcm_payload='bb_payload_bps from the firmware record',
                    qpsk_table_bytes='2^(L+3) rows *8 uint32 words/row *4 bytes; excludes other buffers',
                    pointer_supervision_pct='100*supervision_cycles/(elapsed_us*clk_hz/1e6)',
                    pointer_runway='Required2*(8+ceil(clk_hz*257/(49*1e6))) words; observed minimum from128us campaign only'),
                scopes=dict(full_pipeline='Only clean60s on-chip SPI/GCS runs with complete checked captures',
                    components='Recorded per-call averages, CRC-qualified; no full16APSK modem or RF claim',
                    placement='Same-source A/B/A at128MHz; B is an explicitly failed control, not performance',
                    uncertainty='No inferred confidence interval or error bars; no pooling across trials',
                    source_to_flash='Preserved campaign manifests/binary hashes; plotting does not rebuild or prove source-to-flash identity',
                    cpu='No comparison of QPSK total C0 active with8PSK C0 shaping fraction',
                    pointers='Immutable raw PRBS only, no FEC/SPI; measured poll-body supervision, remaining C0 spins;150MHz75MS/s/axis exceeds proposed65MS/s AFE'))


def self_test():
    selected = select()
    good = selected['qpsk_l8']
    for key, value in (('errors', 1), ('fault', 1), ('faults', ['negative control']), ('ms', 3000),
                       ('elapsed_us', 59999999), ('cap_mismatches', 1), ('cap_aligned', False), ('sps', 2),
                       ('passed', False), ('classification', 'failed'), ('L', 12), ('gen_busy', float('nan'))):
        bad = copy.deepcopy(good); bad[key] = value
        assert not clean_pipeline(bad, 'qpsk'), (key, value)
    for kind, key in (('eight', 'eight_150'), ('vcm', 'vcm_150')):
        row = selected[key]
        assert clean_pipeline(row, kind)
        for field in PV_FAULTS:
            bad = copy.deepcopy(row); bad['pv_spi'][field] = 1
            assert not clean_pipeline(bad, kind), field
        bad = copy.deepcopy(row); bad['sps'] = 4
        assert not clean_pipeline(bad, kind)
    bad = copy.deepcopy(selected['vcm_150']); bad['cap_transition_observed'] = False
    assert not clean_pipeline(bad, 'vcm')
    for key, kind in (('map_new', 'map'), ('sixteen_l9', 'l9'), ('sixteen_l8', 'l8')):
        bad = copy.deepcopy(selected[key])
        if kind == 'map': bad['crc_ok'] = False
        else: bad['kernels'][0]['crc_ok'] = False
        assert not clean_component(bad, kind)
        bad = copy.deepcopy(selected[key]); bad['classification'] = 'failed'
        assert not clean_component(bad, kind)
    assert table_bytes(good) == 65536 and table_bytes(selected['qpsk_l9']) == 131072
    assert not clean_pipeline(selected['b'], 'eight', 3000)
    for policy in ('old', 'new'):
        good = selected[f'pointer_{policy}_128']
        for key, value in (('errors', 1), ('ms', 3000), ('elapsed_us', 59999999),
                ('classification', 'failed'), ('fault', 1), ('input_crc', 0), ('table_crc', 0),
                ('cap_mismatches', 1), ('cap_checked_bus_words', 8191), ('L', 9),
                ('read_beyond_grants', True), ('poll_interval_us', 1), ('max_poll_gap_us', 257)):
            bad = copy.deepcopy(good); bad[key] = value
            assert not clean_pointer(bad, policy), (policy, key)
        for key in ('sym_rate', 'supervision_busy', 'core0_work_busy', 'supervision_cycles'):
            for value in (float('nan'), float('inf'), -float('inf')):
                bad = copy.deepcopy(good); bad[key] = value
                assert not clean_pointer(bad, policy), (policy, key, value)
    for key, value in (('min_credit_runway_words', 1359), ('required_credit_runway_words', 1359),
                       ('poll_issue_budget_words', 679), ('max_source_snapshot_span_us', 257),
                       ('source_cursor_deadline_us', 3133)):
        bad = copy.deepcopy(selected['pointer_new_128']); bad[key] = value
        assert not clean_pointer(bad, 'new'), key
    row = selected['pointer_new_128']
    path = (ROOT/row['_source']['path']).parent/'capture-128000-1-0-0.txt'
    lines = path.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith('@cap '))
    fields = lines[index].split(); fields[2] = f'{int(fields[2], 16)^1:08x}'
    lines[index] = ' '.join(fields)
    try:
        pointer_evidence(row, 'new', lines)
    except AssertionError:
        pass
    else:
        raise AssertionError('Corrupt raw pointer capture accepted')
    print('plot_waveforms self-test: failed/short/fault-injected/wrong-profile/NaN records, CRCs and pointer bound drift rejected; raw captures revalidated')


def style():
    import os
    import sys
    import tempfile
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir())/'yapogee-mpl-cache'))
    sys.path.insert(0, str(ROOT/'reference'))
    from plotstyle import BLUE, DARK, GREEN, MID, RED, plt
    COLORS.update(qpsk=BLUE, eight=RED, vcm=GREEN, teal=GREEN, gray=MID, bad=RED)
    return plt


def save(fig, out, name):
    from plotstyle import save_figure
    save_figure(fig, out/f'{name}.png')


def rates_figure(plt, s, out):
    from plotstyle import WIDTH, dots, lollipop
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(WIDTH, 2.8),
                                gridspec_kw={'width_ratios': [1.25, 1]})
    for kind, keys, label, marker, linestyle in (
            ('qpsk', ['qpsk_l8'], 'QPSK', 's', '-'),
            ('eight', ['eight_128', 'eight_150'], '8PSK', 'o', '--'),
            ('vcm', ['vcm_128', 'vcm_150'], 'VCM', 'D', ':')):
        rows = [s[key] for key in keys]
        dots(ax, [1.2*r['sym_rate']/1e6 for r in rows], [payload(r)/1e6 for r in rows],
             COLORS[kind], label, marker=marker, linestyle=linestyle)
    ax.set(xlim=(8.3, 21), ylim=(12, 29), xlabel='Nominal bandwidth (MHz)', ylabel='BB payload (Mb/s)')
    ax.set_title('(a) Payload rate')
    ax.grid(axis='y')
    ax.legend(loc='upper left')
    keys = ['qpsk_l8', 'vcm_150', 'eight_150']
    values = [payload(s[key])/(1.2*s[key]['sym_rate']) for key in keys]
    for y, value, kind in zip(range(3), values, ('qpsk', 'vcm', 'eight')):
        lollipop(bx, y, value, COLORS[kind], fmt='{:.3f}')
    bx.set(yticks=np.arange(3), yticklabels=['QPSK', 'VCM', '8PSK'],
           xlim=(0, 2.4), xlabel='Payload / bandwidth (bit/s/Hz)', ylim=(2.6, -.6))
    bx.set_title('(b) Nominal efficiency')
    bx.grid(axis='x')
    save(fig, out, 'waveform_capacity')
    plt.close(fig)


def component_figure(plt, s, out):
    from plotstyle import WIDTH, INK, lollipop
    fig, (ax, bx, cx) = plt.subplots(1, 3, figsize=(WIDTH, 2.8),
                                    gridspec_kw={'width_ratios': [.9, 1, 1.65]})
    memory = [table_bytes(s['qpsk_l9'])/1024, table_bytes(s['qpsk_l8'])/1024]
    for y, value in enumerate(memory):
        lollipop(ax, y, value, COLORS['qpsk'], fmt='{:g}')
    ax.set(yticks=[0, 1], yticklabels=['L9', 'L8'], xlim=(0, 165),
           xlabel='Table size (KiB)', ylim=(1.6, -.6))
    ax.set_title('(a) QPSK tables')
    ax.grid(axis='x')
    map_values = [s['map_old']['map_cyc']/1000, s['map_new']['map_cyc']/1000]
    bx.bar([0, 1], map_values, color=COLORS['qpsk'], edgecolor=INK, linewidth=.4, width=.55)
    for x, value in enumerate(map_values):
        bx.annotate(f'{value:.2f}', (x, value), xytext=(0, 4), textcoords='offset points', ha='center', fontsize=7.5)
    bx.set(xticks=[0, 1], xticklabels=['Initial', 'Packed'], ylim=(0, 112),
           ylabel='Cycles per frame (×10³)')
    bx.set_title('(b) 8PSK mapper')
    bx.grid(axis='y')
    bx.set_axisbelow(True)
    old_k = {k['name']: k for k in s['sixteen_l9']['kernels']}
    new_k = {k['name']: k for k in s['sixteen_l8']['kernels']}
    values = [old_k[k]['cyc_per_sym'] for k in ('reverse_bits', 'reverse_map', 'burst_map')]+[new_k['native_burst']['cyc_per_sym']]
    labels = ['L9 C\nbits', 'L9 C\nmap', 'L9 ASM\nbursts', 'L8 ASM\nnative']
    cx.bar(np.arange(4), values, color=COLORS['qpsk'], edgecolor=INK, linewidth=.4, width=.6)
    for x, value in enumerate(values):
        cx.annotate(f'{value:.2f}', (x, value), xytext=(0, 4), textcoords='offset points', ha='center', fontsize=7.5)
    cx.set(xticks=np.arange(4), xticklabels=labels, ylim=(0, 35), ylabel='Cycles per symbol')
    cx.set_title('(c) 16APSK shaper')
    cx.grid(axis='y')
    cx.set_axisbelow(True)
    save(fig, out, 'waveform_representation')
    plt.close(fig)


def capture_iq(row):
    from dual_trial import parse_capture
    path = (ROOT/row['_source']['path']).parent/'capture-128000.txt'
    raw = parse_capture(path.read_text().splitlines())
    assert len(raw) == row['cap_words'] == 4096
    assert zlib.crc32(raw.astype('<u4').tobytes()) == row['cap_crc']
    bus = raw.astype('<u4').view('<u2')
    global_slots = row['cap_global_bus_offset']+np.arange(len(bus), dtype=np.int64)
    flags = (bus >> 14) & 1
    assert np.all(flags == (1-(global_slots & 1))), 'physical IQ phase'
    values = ((bus.astype(np.int32) & 0x3fff) ^ 0x2000)-0x2000
    sample_index = global_slots//2
    ids, ii, qi = np.intersect1d(sample_index[flags == 1], sample_index[flags == 0], return_indices=True)
    i, q = values[flags == 1][ii], values[flags == 0][qi]
    relative = ids/2-row['cap_mode_boundary']
    return relative, i, q, path


def evidence_figure(plt, s, out):
    from plotstyle import WIDTH, dots
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(WIDTH, 2.8),
                                gridspec_kw={'width_ratios': [1, 1.9]})
    rows = [s[key] for key in ('a1', 'b', 'a2')]
    values = [r['encoder_stage_max_cyc']['bb_bch']/1000 for r in rows]
    dots(ax, range(3), values, COLORS['qpsk'])
    ax.plot([1], [values[1]], marker='x', color=COLORS['bad'], ms=5, linestyle='none')
    for x, value in enumerate(values):
        ax.annotate(f'{value:.1f}', (x, value), xytext=(0, 5), textcoords='offset points',
                    ha='center', fontsize=7.5)
    ax.set(xticks=range(3), xticklabels=['A1\nRAM', 'B (failed)\nScratch Y', 'A2\nRAM'],
           xlim=(-.35, 2.35), ylim=(100, 208), ylabel='Max. BB+BCH cycles (×10³)')
    ax.set_title('(a) Memory placement')
    ax.grid(axis='y')
    row = s['vcm_128']
    t, i, q, path = capture_iq(row)
    keep = (t >= -12) & (t <= 112)
    bx.plot(t[keep], i[keep]/1000, color=COLORS['qpsk'], lw=.65, label='I')
    bx.plot(t[keep], q[keep]/1000, color=COLORS['eight'], lw=.65, linestyle='--', label='Q')
    for x in (0, 26, 90):
        bx.axvline(x, color='#707070', ls=(0, (3, 3)), lw=.5)
    for x, label in ((-6, '8PSK'), (13, 'SOF'), (58, 'PLS'), (101, 'QPSK')):
        bx.text(x, 8.0, label, ha='center', fontsize=7)
    bx.set(xlim=(-12, 112), ylim=(-8.7, 8.7), xlabel='Symbol position', ylabel='Sample (×10³ LSB)')
    bx.set_title('(b) Modulation boundary')
    bx.legend(loc='lower right', ncol=2, handlelength=1.8)
    save(fig, out, 'waveform_bus_and_boundary')
    plt.close(fig)
    return path


def pointer_figure(plt, s, out):
    from plotstyle import WIDTH, INK
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(WIDTH, 2.8),
                                gridspec_kw={'width_ratios': [1.15, 1]})
    x = np.arange(2)
    for policy, offset, color, hatch, label in (
            ('old', -.18, COLORS['qpsk'], None, '24 µs'),
            ('new', .18, 'white', '///', '128 µs')):
        values = [100*s[f'pointer_{policy}_{clock}']['supervision_cycles']/
                  (s[f'pointer_{policy}_{clock}']['elapsed_us']*s[f'pointer_{policy}_{clock}']['clk_hz']/1e6)
                  for clock in (128, 150)]
        bars = ax.bar(x+offset, values, width=.32, color=color, edgecolor=INK,
                      linewidth=.5, hatch=hatch, label=label, zorder=3)
        for bar, value in zip(bars, values):
            ax.annotate(f'{value:.2f}', (bar.get_x()+bar.get_width()/2, value),
                        xytext=(0, 4), textcoords='offset points', ha='center', fontsize=7.5)
    ax.set(xticks=x, xticklabels=['128 MHz\n16 Msym/s', '150 MHz\n18.75 Msym/s'],
           ylim=(0, 22), ylabel='Supervision (%)')
    ax.set_title('(a) Poll interval')
    ax.grid(axis='y')
    ax.set_axisbelow(True)
    ax.legend(loc='upper right', ncol=2, handlelength=1.3, columnspacing=1)
    observed = [s[f'pointer_new_{clock}']['min_credit_runway_words'] for clock in (128, 150)]
    required = [s[f'pointer_new_{clock}']['required_credit_runway_words'] for clock in (128, 150)]
    for y, minimum, bound in zip(x, observed, required):
        bx.hlines(y, bound, minimum, color=COLORS['qpsk'], lw=.7)
        bx.plot([minimum], [y], marker='o', color=COLORS['qpsk'], linestyle='none',
                label='Observed' if y == 0 else None)
        bx.plot([bound], [y], marker='|', color=COLORS['eight'], ms=8, linestyle='none',
                label='Required' if y == 0 else None)
        bx.annotate(f'{minimum:,}', (minimum, y), xytext=(5, 0), textcoords='offset points', va='center', fontsize=7.5)
        bx.annotate(f'{bound:,}', (bound, y), xytext=(0, -10), textcoords='offset points', ha='center', va='top', fontsize=7.5)
    bx.set(yticks=x, yticklabels=['128 MHz', '150 MHz'], xlim=(0, 8500),
           ylim=(1.55, -.9), xlabel='Remaining credit (words)')
    bx.set_title('(b) Minimum input credit')
    bx.grid(axis='x')
    bx.legend(loc='upper left', ncol=2, handlelength=1.2, columnspacing=.8, fontsize=7)
    save(fig, out, 'waveform_pointer_supervision')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/'results/plots/waveforms-2026-10-15')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--only-pointer', action='store_true', help='Add figure4 while preserving the original three images byte-for-byte')
    args = parser.parse_args()
    if args.self_test:
        self_test(); return
    selected = select(); record = provenance(selected)
    if args.check:
        print(json.dumps(record, indent=2)); return
    args.out.mkdir(parents=True, exist_ok=True)
    original_names = ('waveform_capacity', 'waveform_representation', 'waveform_bus_and_boundary')
    original_files = [args.out/f'{name}.{suffix}' for name in original_names for suffix in ('png', 'svg', 'pdf')]
    preserved = {}
    prior = {}
    if args.only_pointer:
        assert all(p.exists() for p in original_files), '--only-pointer requires the existing three figures'
        prior = json.loads((args.out/'waveform_selection.json').read_text())
        preserved = {p.name: digest(p) for p in original_files}
        assert all(prior['output_sha256'][name] == value for name, value in preserved.items())
    plt = style()
    if not args.only_pointer:
        rates_figure(plt, selected, args.out)
        component_figure(plt, selected, args.out)
        evidence_figure(plt, selected, args.out)
    pointer_figure(plt, selected, args.out)
    capture = (ROOT/selected['vcm_128']['_source']['path']).parent/'capture-128000.txt'
    record['artifact_sha256'][str(capture.relative_to(ROOT))] = digest(capture)
    record['figure_script_sha256'] = {name: prior.get('figure_script_sha256', {}).get(name, prior.get('script_sha256'))
                                    if args.only_pointer else record['script_sha256'] for name in original_names}
    record['figure_script_sha256']['waveform_pointer_supervision'] = record['script_sha256']
    record['output_sha256'] = {p.name: digest(p) for p in sorted(args.out.glob('waveform_*')) if p.suffix in ('.png', '.svg', '.pdf')}
    assert all(record['output_sha256'][name] == value for name, value in preserved.items()), 'Preserved image changed'
    (args.out/'waveform_selection.json').write_text(json.dumps(record, indent=2)+'\n')
    print(f'Wrote {"figure4 (original three preserved)" if args.only_pointer else "four figures"} as PNG/SVG/PDF and exact selection/provenance to {args.out}')

if __name__ == '__main__':
    main()
