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
    import tempfile
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir())/'yapogee-mpl-cache'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    selected = {order: load(order) for order in SOURCES}
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.labelcolor': '#26394a',
                         'text.color': '#162b3e', 'xtick.color': '#405466', 'ytick.color': '#405466',
                         'axes.spines.top': False, 'axes.spines.right': False, 'axes.spines.left': False,
                         'axes.edgecolor': '#b8c4cc', 'svg.fonttype': 'none', 'savefig.facecolor': 'white'})
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 7), gridspec_kw={'width_ratios': [1.15, 1]})
    fig.subplots_adjust(left=.072, right=.98, bottom=.29, top=.765, wspace=.22)
    fig.text(.072, .94, 'APSK: move work out of the inner loop', fontsize=21, weight='semibold')
    fig.text(.072, .891, 'Exact waveform arithmetic · L8 / 2 SPS · cached Flash · isolated, interrupt-masked component timing',
             fontsize=10, color='#526677')
    fig.text(.072, .847, 'Both 128 and 150 MHz checked; bars show 128 MHz. Input conversion is charged separately.',
             fontsize=9, color='#526677')
    colors = ['#aebecb', '#587895', '#487d91', '#16867b', '#647799']
    for ax, order in zip(axes, (16, 32)):
        values = [selected[order][0]['kernels'][name] for name, _ in KERNELS[order]]
        x = np.arange(len(values))
        shape = np.array([v['shape'] for v in values]); conversion = np.array([v['conversion'] for v in values])
        barcolors = colors if order == 16 else ['#aebecb', '#587895', '#647799', '#16867b']
        ax.bar(x, shape, color=barcolors, width=.62, zorder=3)
        if order == 32:
            ax.bar(x, conversion, bottom=shape, color='#e9c27b', edgecolor='#bb9148', hatch='///',
                   linewidth=.5, width=.62, zorder=3)
        for index, (s, c) in enumerate(zip(shape, conversion)):
            ax.text(index, s+c+.65, f'{s+c:.2f}', ha='center', va='bottom', fontsize=10, weight='semibold')
            if c:
                ax.text(index, s+c/2, f'+{c:.2f}', ha='center', va='center', fontsize=9, color='#59451d')
                ax.text(index, s-1.0, f'{s:.2f}', ha='center', va='top', fontsize=9, color='white', weight='semibold')
        ax.set_xticks(x, [v['label'] for v in values], fontsize=9)
        ax.tick_params(axis='both', length=0, pad=8)
        ax.set_ylim(0, 53); ax.set_yticks(np.arange(0, 51, 10))
        ax.set_axisbelow(True); ax.grid(axis='y', color='#e4e9ed', linewidth=.8)
        ax.set_title(f'{"A" if order == 16 else "B"}   {order}APSK', loc='left', fontsize=14, weight='semibold', pad=18)
        for budget, linestyle, color in ((24, (0, (5, 3)), '#977555'), (32, (0, (2, 3)), '#78828c')):
            ax.axhline(budget, color=color, linestyle=linestyle, linewidth=1, zorder=2)
        if order == 16:
            improvement = 100*(1-values[3]['shape']/values[1]['shape'])
            ax.text(.03, .92, f'Paired schedule: {improvement:.1f}% less kernel work', transform=ax.transAxes,
                    color='#14796f', fontsize=10, weight='semibold', va='top')
            ax.text(.03, .85, 'Sparse history row + fewer loop branches\nSame canonical output; planar changes store order.',
                    transform=ax.transAxes, fontsize=9, color='#526677', linespacing=1.6, va='top')
    axes[0].set_ylabel('Measured CPU cycles / symbol', labelpad=14)
    from matplotlib.lines import Line2D
    fig.legend(handles=[Patch(facecolor='#587895', label='Shaper kernel'),
                        Patch(facecolor='#e9c27b', edgecolor='#bb9148', hatch='///', label='Input conversion'),
                        Line2D([0], [0], color='#977555', linestyle=(0, (5, 3)), label='24 cy/sym budget'),
                        Line2D([0], [0], color='#78828c', linestyle=(0, (2, 3)), label='32 cy/sym budget')],
               loc='lower center', bbox_to_anchor=(.52, .19), ncol=4, frameon=False, fontsize=8.5,
               handlelength=2, columnspacing=2.2)
    fig.text(.072, .16, '16APSK: γ=2.70; 136 KiB LUTs; 3+4+4 partial rounding. 32APSK: γ₁=2.64, γ₂=4.64; 115.1 KiB LUTs; 3+3+3 partial rounding.',
             fontsize=8.3, color='#405466')
    fig.text(.072, .12, 'Budgets: 150 MHz / 6.25 Msym/s = 24 cy/sym; 150 MHz / 4.6875 Msym/s = 32 cy/sym. Output/control/input/FEC work is additional.',
             fontsize=8.3, color='#405466')
    fig.text(.072, .08, 'Canonical waveform CRCs match; clock pairs agree within 0.01 cy/sym. No statistical error bars inferred. Component costs, without a net throughput claim.',
             fontsize=8.1, color='#405466')
    source_names = ', '.join(Path(SOURCES[o]).parent.name for o in (16, 32))
    fig.text(.072, .04, f'Sources: apsk-frontier/2026-10-15/{{{source_names}}}/runs.jsonl. Exact rows and hashes: selection.json. No AFE or RF claim.',
             fontsize=7.8, color='#6a7883')
    if Path(SOURCES[32]).parent.name == 'component-3':
        fig.text(.98, .805, '32APSK exploratory source; stack fix awaiting remeasurement',
                 ha='right', fontsize=7.6, color='#a3593e')
    out.mkdir(parents=True, exist_ok=True)
    for extension in ('svg', 'png'):
        fig.savefig(out/f'apsk_frontier.{extension}', dpi=220)
    plt.close(fig)
    (out/'selection.json').write_text(json.dumps(dict(sources=selected, display_clock_hz=128000000,
        clock_agreement_tolerance_cycles_per_symbol=.01, deadline_cycles_per_symbol=[24, 32],
        conversion_accounting='separate stacked measured cost; native kernels consume coordinate input directly',
        scope='isolated IRQ-masked component work; no inferred complete-modem or RF throughput'), indent=2)+'\n')
    print(json.dumps(dict(output=str(out), figures=['apsk_frontier.svg', 'apsk_frontier.png'],
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
