#!/usr/bin/env python3
"""Summary figures for the README, drawn from the recorded device runs.

Every plotted number is read from a results/ record (paths in selection.json). Capacity is the
record's bb_payload_bps, or (Kbch - 80) * Rs / frame symbols where a record has no such field.

  python3 host/plot_summary.py            # writes results/plots/summary-2026-10-16/
"""
from __future__ import annotations

import glob
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'reference'))
from plotstyle import DARK, INK, MID, WIDTH, plt, save_figure  # noqa: E402

OUT = ROOT / 'results/plots/summary-2026-10-16'
LIGHT, FILL = '#a8a8a8', '#d4d4d4'
KBCH = {5: 43040, 6: 48408, 8: 53840, 10: 58192}       # normal FECFRAME, EN 302 307-1 Table 5a
FRAME = {'QPSK': 33282, '8PSK': 22194}                  # PLFRAME symbols with pilots
selection = {}


def record(pattern, **want):
    """First fault-free record under pattern whose fields equal want."""
    for path in sorted(glob.glob(str(ROOT / pattern))):
        for line in open(path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get('faults') == [] and all(r.get(k) == v for k, v in want.items()):
                return Path(path).relative_to(ROOT).as_posix(), r
    raise SystemExit(f'no fault-free record in {pattern} with {want}')


def note(key, path, r, fields):
    selection[key] = dict(source=path, **{f: r.get(f) for f in fields})


def capacity(r, mod, code):
    return r.get('bb_payload_bps') or (KBCH[code] - 80) * r['sym_rate'] / FRAME[mod]


# ---- complete digital pipelines (SPI source -> FEC -> mapping -> shaping -> pins) ----
POINTS = []   # (label, modulation, clock MHz, Rs, capacity, complete)

def point(key, label, mod, code, pattern, complete=True, **want):
    path, r = record(pattern, **want)
    note(key, path, r, ('cmd', 'clk_hz', 'sym_rate', 'bb_payload_bps', 'label'))
    POINTS.append((label, mod, r['clk_hz'] / 1e6, r['sym_rate'], capacity(r, mod, code), complete))
    return r


point('qpsk16_910', 'QPSK 9/10', 'QPSK', 10, 'results/round2/2026-10-15/q1-*/runs.jsonl', cmd='dualptr', source=6)
point('qpsk16_56', 'QPSK 5/6', 'QPSK', 8, 'results/b2-8psk-live/2026-10-15/a1-16msym-60s-prio13/runs.jsonl',
      cmd='dualptr', source=6)
point('qpsk8_56', 'QPSK 5/6', 'QPSK', 8, 'results/dualptr/2026-10-15/a1-8msym-60s/runs.jsonl', cmd='dualptr', source=5)
point('8psk8_hw', '8PSK 5/6', '8PSK', 8, 'results/b7-live8/2026-10-15/live8-60s-prio13-1/runs.jsonl', cmd='bench4fec')
point('8psk150_cpu', '8PSK 5/6', '8PSK', 8, 'results/8psk-live/2026-10-15/*/runs.jsonl', cmd='bench8live', clk_hz=150000000)
point('vcm150', 'VCM', 'VCM', 8, 'results/vcm/2026-10-15/*/runs.jsonl', cmd='benchvcm', clk_hz=150000000)
for m in (16, 32):
    for clk in (128, 150):
        point(f'{m}apsk{clk}', f'{m}APSK 5/6', f'{m}APSK', 8,
              f'results/apsk-fec/2026-10-15/qualified-{m}-{clk}-1/runs.jsonl', cmd='benchapskfec')
b6 = point('8psk16_waveform', '8PSK 5/6', '8PSK', 8, 'results/b6-pio-producers/2026-10-15/live-60s/runs.jsonl',
           complete=False, cmd='bench4prod8live', cpw=1)

MARK = {'QPSK': 's', '8PSK': 'o', 'VCM': 'D', '16APSK': '^', '32APSK': 'v'}


def fig_capacity():
    fig, ax = plt.subplots(figsize=(WIDTH, 3.3))
    seen = set()
    for label, mod, clk, rs, cap, complete in POINTS:
        width = 1.2 * rs / 1e6
        face = (INK if clk == 128 else 'white') if complete else LIGHT
        edge = INK if complete else MID
        ax.plot(width, cap / 1e6, MARK[mod], ms=5.5, mfc=face, mec=edge, mew=0.8, ls='none',
                zorder=3, label=None)
        seen.add(mod)
    ax.axvline(20, color=MID, ls='--', lw=0.7)
    ax.text(19.8, 1.0, 'AFE7071 RF bandwidth', ha='right', va='bottom', fontsize=7.5, color=DARK)
    ax.axhline(9.81, color=MID, ls=':', lw=0.7)
    ax.text(19.8, 8.9, 'Pigeon Vision budget, 9.81 Mb/s', ha='right', va='top', fontsize=7.5, color=DARK)
    ax.annotate('8PSK 5/6 waveform only\n(FEC and input off-chip)', (19.2, POINTS[-1][4] / 1e6),
                xytext=(12.6, 37.0), fontsize=7.5, color=DARK, va='center',
                arrowprops=dict(arrowstyle='-', color=MID, lw=0.6))
    # One label per mode and clock pair; open/filled markers carry the clock.
    labels = {('QPSK', 16e6): ('QPSK 5/6, 9/10', -0.4, -1.0, 'right'),
              ('QPSK', 8e6): ('QPSK 5/6', 0.45, 0, 'left'),
              ('8PSK', 8e6): ('8PSK 5/6', -0.45, 0, 'right'),
              ('8PSK', 9375000.0): ('8PSK 5/6', 0.45, 0, 'left'),
              ('VCM', 9375000.0): ('VCM', 0.45, 0, 'left'),
              ('16APSK', None): ('16APSK 5/6', 0.45, 0, 'left'),
              ('32APSK', None): ('32APSK 5/6', -0.45, 0.9, 'right')}
    done = set()
    for label, mod, clk, rs, cap, complete in POINTS[:-1]:
        key = (mod, None) if 'APSK' in mod else (mod, round(rs))
        if key in done or key not in labels or ('APSK' in mod and clk != 150):
            continue
        done.add(key)
        text, dx, dy, ha = labels[key]
        ax.text(1.2 * rs / 1e6 + dx, cap / 1e6 + dy, text, fontsize=7.2, va='center', ha=ha, color=INK)
    ax.set_xlim(0, 21)
    ax.set_ylim(0, 42)
    ax.set_xticks(range(0, 21, 5))
    ax.set_xlabel('Nominal occupied bandwidth, 1.2 $R_s$ (MHz)')
    ax.set_ylabel('BB data-field capacity (Mb/s)')
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], marker=MARK[m], ls='none', mfc=INK, mec=INK, ms=5, label=m) for m in MARK]
    handles += [Line2D([], [], marker='o', ls='none', mfc=INK, mec=INK, ms=5, label='128 MHz'),
                Line2D([], [], marker='o', ls='none', mfc='white', mec=INK, ms=5, label='150 MHz')]
    ax.legend(handles=handles, loc='upper left', ncol=2, fontsize=7.5, handlelength=1.2)
    save_figure(fig, OUT / 'capacity')


# ---- CPU per core for complete pipelines ----
def fig_cpu():
    rows = []
    path, r = record('results/dualptr/2026-10-15/a1-8msym-60s/runs.jsonl', cmd='dualptr', source=5)
    note('cpu_qpsk8', path, r, ('core0_work_busy', 'core1_work_busy'))
    rows.append(('QPSK 5/6, 8 Msym/s, TS', r['core0_work_busy'], r['core1_work_busy']))
    path, r = record('results/round2/2026-10-15/q1-*/runs.jsonl', cmd='dualptr', source=6)
    note('cpu_qpsk16', path, r, ('core0_work_busy', 'core1_work_busy'))
    rows.append(('QPSK 9/10, 16 Msym/s, GCS', r['core0_work_busy'], r['core1_work_busy']))
    path, r = record('results/b7-live8/2026-10-15/live8-60s-prio13-1/runs.jsonl', cmd='bench4fec')
    note('cpu_8psk8', path, r, ('core0_work_busy', 'encoder_busy'))
    rows.append(('8PSK 5/6, 8 Msym/s, GCS', r['core0_work_busy'], r['encoder_busy']))
    for m, clk in ((16, 150), (32, 150)):
        path, r = record(f'results/apsk-fec/2026-10-15/qualified-{m}-{clk}-1/runs.jsonl', cmd='benchapskfec')
        note(f'cpu_{m}apsk', path, r, ('producer_work_busy', 'encoder_busy', 'sym_rate'))
        rows.append((f'{m}APSK 5/6, {r["sym_rate"] / 1e6:.3f} Msym/s, 150 MHz',
                     r['producer_work_busy'], r['encoder_busy']))
    fig, ax = plt.subplots(figsize=(WIDTH, 2.6))
    h = 0.36
    for i, (label, c0, c1) in enumerate(rows):
        y = len(rows) - 1 - i
        ax.barh(y + h / 2, 100 * c0, h, color=FILL, edgecolor=INK, lw=0.5)
        ax.barh(y - h / 2, 100 * c1, h, color=DARK, edgecolor=INK, lw=0.5)
        ax.text(100 * c0 + 1, y + h / 2, f'{100 * c0:.1f}', va='center', fontsize=7)
        ax.text(100 * c1 + 1, y - h / 2, f'{100 * c1:.1f}', va='center', fontsize=7)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows][::-1], fontsize=7.5)
    ax.set_xlim(0, 110)
    ax.set_xlabel('Measured work (% of core)')
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(facecolor=FILL, edgecolor=INK, lw=0.5, label='core 0'),
                       Patch(facecolor=DARK, edgecolor=INK, lw=0.5, label='core 1')],
              loc='upper right', fontsize=7.5)
    save_figure(fig, OUT / 'cpu')


# ---- DMA row loop against the per-chain deadline ----
def loop_cycles(fn, chains, words, sink, prio=13):
    vals = []
    for line in open(ROOT / 'results/dmaloop/2026-10-15' / fn):
        r = json.loads(line)
        if (r.get('cmd') == 'dmaloop' and r.get('chains') == chains and r.get('row_words') == words and
                r.get('sink') == sink and r.get('bus_priority') == prio and not r.get('hammer') and
                r.get('load_div') == 0):
            vals += r['cycles_per_iteration']
    if not vals:
        raise SystemExit(f'no dmaloop record {fn} {chains} {words} {sink}')
    selection[f'loop_{chains}_{words}_{sink}'] = dict(source=f'results/dmaloop/2026-10-15/{fn}',
                                                      cycles=sum(vals) / len(vals))
    return sum(vals) / len(vals)


def fig_loop():
    cases = [  # label, measured loop, deadline (cycles per row per chain), outcome
        ('B4: 8-bit, PIO address, 1 chain/axis, 16 Msym/s', loop_cycles('pio-pointers.jsonl', 2, 1, 'pio_pointers'), 8, 'stall in 20 µs'),
        ('14-bit, SRAM address, 1 chain/axis, 16 Msym/s', loop_cycles('pio-pointers.jsonl', 2, 2, 'pio'), 8, 'always stalled'),
        ('B5: 14-bit, SRAM address, 2 chains/axis, 16 Msym/s', loop_cycles('pio-pointers.jsonl', 4, 2, 'pio'), 16, '5/6 runs clean'),
        ('B2: 14-bit, PIO address, 1 chain/axis, 8 Msym/s', loop_cycles('pio-pointers.jsonl', 2, 2, 'pio_pointers'), 16, 'rare stalls'),
        ('B6: 8-bit, PIO address, 2 chains/axis, 16 Msym/s', loop_cycles('pio-pointers.jsonl', 4, 1, 'pio_pointers'), 16, '12/12 clean'),
        ('B4/B7: 8-bit, PIO address, 1 chain/axis, 8 Msym/s', loop_cycles('pio-pointers.jsonl', 2, 1, 'pio_pointers'), 16, 'clean'),
        ('B5: 8-bit, SRAM address, 2 chains/axis, 16 Msym/s', loop_cycles('pio-pointers.jsonl', 4, 1, 'pio'), 16, '12/12 clean'),
    ]
    fig, ax = plt.subplots(figsize=(WIDTH, 2.9))
    for i, (label, loop, deadline, outcome) in enumerate(cases):
        y = len(cases) - 1 - i
        ax.plot([loop, deadline], [y, y], color=LIGHT, lw=1.6, solid_capstyle='butt', zorder=1)
        ax.plot(deadline, y, '|', color=INK, ms=9, mew=1.2, zorder=2)
        ax.plot(loop, y, 'o', mfc=INK if loop > deadline - 0.05 else 'white', mec=INK, ms=5, zorder=3)
        ax.text(19.6, y, outcome, va='center', fontsize=7.2, color=DARK)
    ax.set_yticks(range(len(cases)))
    ax.set_yticklabels([c[0] for c in cases][::-1], fontsize=7.2)
    ax.set_xlim(6, 19.3)
    ax.set_xlabel('SYS cycles per table row per DMA chain')
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([], [], marker='o', ls='none', mfc='white', mec=INK, ms=5, label='loop, with margin'),
                       Line2D([], [], marker='o', ls='none', mfc=INK, mec=INK, ms=5, label='loop, no margin'),
                       Line2D([], [], marker='|', ls='none', color=INK, ms=9, mew=1.2, label='deadline')],
              loc='lower left', fontsize=7.2, handlelength=1.2)
    save_figure(fig, OUT / 'dma_loop')


# ---- two-core FEC per frame against the 16 Msym/s budget ----
def fig_fec():
    def job(fn, label, who, options, layout=0):
        for line in open(ROOT / 'results/ldpc-banks/2026-10-15' / fn):
            r = json.loads(line)
            if r.get('cmd') == 'c3pair' and r.get('code_index') == 5 and r.get('who') == who and \
                    r.get('options') == options and r.get('layout') == layout:
                j = r['jobs'][0]
                selection[f'fec_{label}'] = dict(source=f'results/ldpc-banks/2026-10-15/{fn}',
                                                 command=r['label'], avg_stage=j['avg_stage'])
                return j['avg_stage']
        raise SystemExit(f'no c3pair record {fn} {who} {options}')
    rows = [('one core', job('pair-bch.jsonl', 'solo', 0, 31)),
            ('both cores, BCH code in SCRATCH_Y', job('pair-bch.jsonl', 'both_scratch', 2, 31)),
            ('both cores, BCH code in striped SRAM', job('pair-bch.jsonl', 'both_striped', 2, 27))]
    stages = (('gcs', 'framing', 'white', ''), ('bch', 'BCH', FILL, ''), ('ldpc', 'LDPC', MID, ''),
              ('map', '8PSK map', 'white', '////'))
    fig, ax = plt.subplots(figsize=(WIDTH, 2.4))
    for i, (label, st) in enumerate(rows):
        y, left = len(rows) - 1 - i, 0
        for key, name, color, hatch in stages:
            ax.barh(y, st[key] / 1e3, 0.55, left=left, color=color, hatch=hatch, edgecolor=INK, lw=0.5,
                    label=name if i == 0 else None)
            left += st[key] / 1e3
        ax.text(max(left, 355.104) + 5, y, f'{left:.0f}k', va='center', fontsize=7.5)
    ax.axvline(355.104, color=INK, ls='--', lw=0.8)
    ax.text(359, 2.62, '16 Msym/s budget, 355k cycles', ha='left', va='center', fontsize=7.2, color=DARK)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows][::-1], fontsize=7.5)
    ax.set_xlim(0, 540)
    ax.set_ylim(-0.55, 2.9)
    ax.set_xlabel('SYS cycles per 8PSK 2/3 frame on one core, sum of stages (thousands)')
    ax.legend(loc='upper center', ncol=4, fontsize=7.2, bbox_to_anchor=(0.5, -0.36), handlelength=1.5)
    save_figure(fig, OUT / 'fec_cycles')


if __name__ == '__main__':
    fig_capacity()
    fig_cpu()
    fig_loop()
    fig_fec()
    style = ROOT / 'reference/plotstyle.py'
    (OUT / 'selection.json').write_text(json.dumps(dict(
        generator='host/plot_summary.py', style=dict(path='reference/plotstyle.py',
        sha256=hashlib.sha256(style.read_bytes()).hexdigest()), points=selection), indent=2) + '\n')
    print(f'wrote {OUT.relative_to(ROOT)}')
