"""Scientific component-work comparison from pinned, CRC-checked APSK trials.

python3 host/plot_apsk_frontier.py --self-test
python3 host/plot_apsk_frontier.py --out results/plots/apsk-frontier-2026-10-15

No Pico access. Both stock clocks must pass all profile/CRC/accounting checks.
Bars use the128MHz measurements;150MHz must agree within0.01cycle/symbol.
Conversion work is charged separately, rather than hidden in pre-keyed timings.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    16: 'results/apsk-frontier/2026-10-15/component-4/runs.jsonl',
    32: 'results/apsk-frontier/2026-10-15/component-4/runs.jsonl',
}
COEFFICIENT_SHA = '16936cb50f5de62e68b10367624b868afea4ad49f8dfc4481526de4712fda5ae'
PROFILES = {
    16: dict(cmd='bench16native8', row_profile='apsk16_l8_n2_hold_m010_far2_native344',
             quantizer='native_3_4_4', table_bytes=139264, input_crc=787286031,
             old_crc=1009880281, middle_crc=2648433961, future_crc=3030949170,
             gamma100=270),
    32: dict(cmd='bench32native8', row_profile='apsk32_l8_n2_native333',
             quantizer='native_3_3_3', table_bytes=117912, input_crc=1106835987,
             old_crc=3255586384, middle_crc=916828675, future_crc=1817262751,
             gamma1_100=264, gamma2_100=464, outer_radius=1),
}
KERNELS = {
    16: [('native_c', 'Native\nC'), ('native_burst', 'Native\nASM'),
         ('sparse_burst', 'Sparse\nASM'), ('pair_burst', 'Paired\nASM'),
         ('planar_burst', 'Planar\nASM')],
    32: [('native_c', 'Native\nC'), ('native_burst', 'Native\nASM'),
         ('pair_burst', 'Packed-pair\nkeys'), ('keys_burst', 'Rolling\nkeys')],
}
CRCS = {16: (2596089319, 4290221674), 32: (2048936984, 1960087962)}


def require(condition, message):
    if not condition: raise ValueError(message)


def validate(row, order):
    expected = dict(PROFILES[order], L=8, sps=2, code_placement='flash_cached',
                    pulse_profile='psk8_l8_n2_hold_m010_far2', coefficient_sha256=COEFFICIENT_SHA)
    require(all(row.get(key) == value for key, value in expected.items()), f'{order}APSK profile mismatch')
    require(row.get('clk_hz') in (128000000, 150000000) and row.get('reps', 0) >= 32,
            'requires both stock clocks and at least32 repetitions')
    require(not row.get('error') and not row.get('faults') and
            not any(row.get(k, 0) for k in ('errors', 'underruns', 'own_errors', 'txstalls')) and
            row.get('passed') is not False and
            row.get('classification') in (None, 'clean'), 'failed component row')
    require(isinstance(row.get('git'), str) and bool(row['git']), 'firmware identity missing')
    kernels = row.get('kernels', [])
    names = [k.get('name') for k in kernels]
    require(len(names) == len(set(names)), 'duplicate kernel records')
    selected = {}
    for name, label in KERNELS[order]:
        require(name in names, f'missing kernel {name}')
        k = kernels[names.index(name)]
        require(k.get('crc_ok') is True and (k.get('raw_crc'), k.get('packed_crc')) == CRCS[order],
                f'canonical waveform CRC mismatch: {name}')
        shape, conversion = k.get('cyc_per_sym'), k.get('convert_cyc_per_sym', 0)
        require(all(type(v) in (int, float) and math.isfinite(v) for v in (shape, conversion)) and
                shape > 0 and conversion >= 0, f'invalid measured work: {name}')
        require(math.isfinite(k.get('worst_block_cyc_per_sym', float('nan'))) and
                k['worst_block_cyc_per_sym']+.0002 >= shape, f'average exceeds worst block: {name}')
        if order == 16:
            require(k.get('output_layout') == ('planar4' if name == 'planar_burst' else 'raw_pairs'),
                    f'wrong16APSK output layout: {name}')
            require(conversion == 0, 'unexpected16APSK conversion work')
        else:
            require(row.get('output_layout') == 'raw_pairs' and
                    k.get('input_bytes_per_sym') == (4 if name == 'keys_burst' else 2), '32APSK format mismatch')
            require(math.isclose(k.get('convert_plus_shape_cyc_per_sym', -1), shape+conversion, abs_tol=.0002),
                    f'conversion/shape total mismatch: {name}')
            require((conversion > 0) == (name in ('pair_burst', 'keys_burst')), 'conversion cost missing or unexpected')
        selected[name] = dict(label=label, shape=shape, conversion=conversion, total=shape+conversion,
                              input_bytes_per_sym=k.get('input_bytes_per_sym'),
                              output_layout=k.get('output_layout', row.get('output_layout')))
    return selected


def load(order):
    path = ROOT/SOURCES[order]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = []
    for line, text in enumerate(path.read_text().splitlines(), 1):
        row = json.loads(text)
        if row.get('cmd') == PROFILES[order]['cmd']:
            checked = validate(row, order)
            rows.append(dict(clock_hz=row['clk_hz'], git=row['git'], kernels=checked,
                             source=dict(path=SOURCES[order], line=line, sha256=digest)))
    require(len(rows) == 2 and {r['clock_hz'] for r in rows} == {128000000, 150000000},
            f'{order}APSK requires exactly one checked row at each clock')
    rows.sort(key=lambda row: row['clock_hz'])
    require(rows[0]['git'] == rows[1]['git'], 'clock comparison uses different firmware identities')
    for name, _ in KERNELS[order]:
        for measure in ('shape', 'conversion'):
            require(abs(rows[0]['kernels'][name][measure]-rows[1]['kernels'][name][measure]) <= .01,
                    f'clock comparison differs by >0.01cycle/symbol: {name}')
    return rows


def self_test():
    selected = {order: load(order) for order in SOURCES}
    controls = 0
    for order, pairs in selected.items():
        raw = [json.loads(line) for line in (ROOT/SOURCES[order]).read_text().splitlines()]
        row = next(r for r in raw if r.get('cmd') == PROFILES[order]['cmd'])
        for mutate in ('crc', 'profile', 'clock', 'identity', 'work', 'duplicate', 'total', 'fault'):
            bad = copy.deepcopy(row)
            if mutate == 'crc': bad['kernels'][0]['raw_crc'] ^= 1
            elif mutate == 'profile': bad['quantizer'] = 'different'
            elif mutate == 'clock': bad['clk_hz'] = 200000000
            elif mutate == 'identity': del bad['git']
            elif mutate == 'work': bad['kernels'][0]['cyc_per_sym'] = float('nan')
            elif mutate == 'duplicate': bad['kernels'].append(copy.deepcopy(bad['kernels'][0]))
            elif mutate == 'fault': bad['errors'] = 1
            elif order == 32: bad['kernels'][0]['convert_plus_shape_cyc_per_sym'] += 1
            else: bad['kernels'][0]['output_layout'] = 'planar4'
            try: validate(bad, order)
            except ValueError: controls += 1
            else: raise AssertionError(f'accepted {order}APSK {mutate} control')
    print(json.dumps(dict(self_test='passed', checked_clock_profiles=4, negative_controls=controls,
                          kernels={str(o): len(KERNELS[o]) for o in SOURCES},
                          clocks_hz=[128000000, 150000000], performance_values='read from CRC-checked raw JSON')))


def render(out):
    import os
    import sys
    import tempfile
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir())/'yapogee-mpl-cache'))
    sys.path.insert(0, str(ROOT/'reference'))
    from plotstyle import BLUE, GREEN, INK, WIDTH, plt, save_figure
    from matplotlib.patches import Patch

    selected = {order: load(order) for order in SOURCES}
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.8), sharey=True,
                             gridspec_kw={'width_ratios': [1.15, 1]})
    for ax, order in zip(axes, (16, 32)):
        values = [selected[order][0]['kernels'][name] for name, _ in KERNELS[order]]
        x = np.arange(len(values))
        shape = np.array([v['shape'] for v in values])
        conversion = np.array([v['conversion'] for v in values])
        ax.bar(x, shape, color=BLUE, edgecolor=INK, linewidth=.4, width=.62, zorder=3)
        if order == 32:
            ax.bar(x, conversion, bottom=shape, facecolor='white', edgecolor=GREEN,
                   hatch='///', linewidth=.6, width=.62, zorder=3)
        for index, (shaping, converted) in enumerate(zip(shape, conversion)):
            ax.annotate(f'{shaping+converted:.2f}', (index, shaping+converted),
                        xytext=(0, 4), textcoords='offset points', ha='center', fontsize=7.5)
            if converted:
                ax.text(index, shaping/2, f'{shaping:.2f}', color='white',
                        ha='center', va='center', fontsize=7.5)
        ax.set_xticks(x, [v['label'] for v in values])
        ax.set_ylim(0, 53)
        ax.set_yticks(np.arange(0, 51, 10))
        ax.set_axisbelow(True)
        ax.grid(axis='y')
        ax.set_title(f'({"a" if order == 16 else "b"}) {order}APSK')
    axes[0].set_ylabel('Cycles per symbol')
    axes[1].legend(handles=[Patch(facecolor=BLUE, edgecolor=INK, linewidth=.4, label='Shaping'),
                            Patch(facecolor='white', edgecolor=GREEN, hatch='///', label='Conversion')],
                   loc='upper right', fontsize=7)
    out.mkdir(parents=True, exist_ok=True)
    save_figure(fig, out/'apsk_frontier.png')
    (out/'selection.json').write_text(json.dumps(dict(sources=selected, display_clock_hz=128000000,
        clock_agreement_tolerance_cycles_per_symbol=.01, deadline_cycles_per_symbol=[24, 32],
        conversion_accounting='separate stacked measured cost; native kernels consume coordinate input directly',
        waveform_profiles=PROFILES, pulse_profile='psk8_l8_n2_hold_m010_far2',
        coefficient_sha256=COEFFICIENT_SHA,
        scope='isolated IRQ-masked component work; no inferred complete-modem or RF throughput',
        style=dict(path='reference/plotstyle.py', sha256=hashlib.sha256((ROOT/'reference/plotstyle.py').read_bytes()).hexdigest()),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        output_sha256={f'apsk_frontier.{ext}': hashlib.sha256((out/f'apsk_frontier.{ext}').read_bytes()).hexdigest()
                       for ext in ('png', 'svg', 'pdf')}), indent=2)+'\n')
    print(json.dumps(dict(output=str(out), figures=['apsk_frontier.svg', 'apsk_frontier.png', 'apsk_frontier.pdf'],
                          provenance='selection.json')))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test', action='store_true')
    p.add_argument('--component32', type=Path, help='override the32APSK runs.jsonl source, e.g. the final component-4 repeat')
    p.add_argument('--out', type=Path, default=ROOT/'results/plots/apsk-frontier-2026-10-15')
    a = p.parse_args()
    if a.component32 is not None: SOURCES[32] = str(a.component32)
    if a.self_test: self_test()
    else: render(a.out)


if __name__ == '__main__': main()
