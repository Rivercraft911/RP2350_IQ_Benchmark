#!/usr/bin/env python3
"""Progress over the project's versions, and a map of what was tried.

Values come from docs/benchmark-history.md (versions before 2026-10-14) and the results
folders named in SOURCES; dates are each folder's first commit. Writes
results/plots/summary-2026-10-16/{progress,experiment_map}.{png,svg,pdf} and history.json.

  python3 host/plot_history.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'reference'))
from plotstyle import DARK, INK, MID, WIDTH, plt, save_figure  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

OUT = ROOT / 'results/plots/summary-2026-10-16'
LIGHT = '#a8a8a8'

# Shaper on the CPU, QPSK N=4, L=10, cycles per symbol (docs/benchmark-history.md, optimization record)
SHAPER = [('convolution', 917.7), ('LUT', 58.6), ('windows', 29.1), ('PIO\ninterleave', 13.8),
          ('assembly', 11.55), ('SRAM\nplacement', 11.05), ('pipelined', 10.80),
          # Table rows copied by DMA, addresses built by PIO: descriptor_gen_busy and
          # sample_math_busy are 0 in results/dualptr/2026-10-15 and results/b6-pio-producers.
          ('PIO +\nDMA', 0.0)]

# BCH + LDPC, thousands of cycles per normal 2/3 frame on one core
FEC = [('bit-serial', 1356 + 2771, 'docs/benchmark-history.md'),
       ('v1', 204 + 172, 'docs/benchmark-history.md'),
       ('v2', 110 + 163, 'docs/benchmark-history.md'),
       ('v3', 110 + 144, 'docs/benchmark-history.md'),
       ('v4', 110 + 127, 'docs/benchmark-history.md'),
       ('two-core\n(C3)', 85.9 + 156.7, 'results/c3-8psk-fec16/2026-10-15/gap4096.jsonl'),
       ('C4', 84.3 + 142.0, 'results/ldpc-banks/2026-10-15/pair-bch.jsonl (who 0, options 27)'),
       ('C5\nLDPC', 85.0 + 84.9, 'results/c8-fec-combined/2026-10-16/pair.jsonl (c3pair 5 40 0 0 123, job A)'),
       ('C7\nBCH', 65.7 + 87.0, 'results/c8-fec-combined/2026-10-16/pair.jsonl (c3pair 5 40 4 0 best, job A)')]

# Core-0 work for complete QPSK pipelines, % of core: (engine, 8 Msym/s, 16 Msym/s, source)
CORE0 = [('CPU shaper', 72.0, None, 'docs/benchmark-history.md (QPSK 2/3, L=10)'),
         ('LUT rows by DMA,\nCPU addresses', 47.2, 81.4,
          'results/dualptr/2026-10-15 (5/6, 8 Msym/s); results/b2-8psk-live/2026-10-15 (5/6, 16 Msym/s)'),
         ('PIO-computed\naddresses', 25.7, 47.7,
          'results/dualptr/2026-10-15; results/b2-8psk-live/2026-10-15 (priority 13)')]


def fig_progress():
    fig, axes = plt.subplots(1, 3, figsize=(WIDTH * 1.45, 2.9), gridspec_kw=dict(width_ratios=[1.25, 1.15, 0.8]))
    ax = axes[0]
    x = range(len(SHAPER))
    # symlog: logarithmic above 1 cycle, linear below, so the PIO + DMA point can sit at 0.
    ax.set_yscale('symlog', linthresh=1, linscale=0.6)
    ax.plot(list(x)[:-1], [v for _, v in SHAPER][:-1], 'o-', color=INK, mfc='white', ms=4)
    ax.plot([len(SHAPER) - 2, len(SHAPER) - 1], [SHAPER[-2][1], 0], ':', color=INK, lw=0.9)
    ax.plot([len(SHAPER) - 1], [0], 'o', color=INK, ms=4.5)
    for i, (_, v) in enumerate(SHAPER):
        last = i == len(SHAPER) - 1
        ax.annotate(f'{v:g}', (i, v), xytext=(7, 0) if last else (0, 5), textcoords='offset points',
                    ha='left' if last else 'center', va='center' if last else 'bottom', fontsize=6.5)
    ax.text(len(SHAPER) - 1, 1600, 'no CPU\nwork', ha='center', va='top', fontsize=6.3,
            color=DARK)
    ax.axvline(len(SHAPER) - 1.5, color=MID, lw=0.5, ls='--')
    ax.text(len(SHAPER) - 3.6, 1600, 'CPU computes samples', ha='center', va='top', fontsize=6.3, color=DARK)
    ax.set_xticks(list(x))
    ax.set_xlim(-0.6, len(SHAPER) - 0.4)
    ax.set_xticklabels([n for n, _ in SHAPER], fontsize=6, rotation=0)
    ax.set_ylim(-0.3, 3000)
    ax.set_yticks([0, 1, 10, 100, 1000])
    ax.set_yticklabels(['0', '1', '10', '100', '1000'])
    ax.set_ylabel('CPU cycles per symbol')
    ax.set_title('(a) Pulse shaping, QPSK, 4 SPS', loc='left', fontsize=8.5)

    ax = axes[1]
    x = range(len(FEC))
    ax.semilogy(x, [v for _, v, _ in FEC], 's-', color=INK, mfc='white', ms=4)
    for i, (_, v, _) in enumerate(FEC):
        ax.annotate(f'{v:.0f}k', (i, v), xytext=(0, 6), textcoords='offset points', ha='center', va='bottom',
                    fontsize=6.5)
    ax.axhline(355.104, color=MID, ls='--', lw=0.7)
    ax.text(len(FEC) - 1, 420, '8PSK 16 Msym/s budget\nfor a whole frame', ha='right', fontsize=6.3, color=DARK)
    ax.set_xticks(list(x))
    ax.set_xlim(-0.6, len(FEC) - 0.4)
    ax.set_xticklabels([n for n, _, _ in FEC], fontsize=6)
    ax.set_ylim(100, 12000)
    ax.set_ylabel('BCH + LDPC, thousands of cycles')
    ax.set_title('(b) FEC per normal 2/3 frame', loc='left', fontsize=8.5)

    ax = axes[2]
    y = range(len(CORE0))
    h = 0.34
    for i, (name, c8, c16, _) in enumerate(CORE0):
        yy = len(CORE0) - 1 - i
        ax.barh(yy + h / 2, c8, h, color='#d4d4d4', edgecolor=INK, lw=0.5)
        ax.text(c8 + 1.5, yy + h / 2, f'{c8:.0f}', va='center', fontsize=6.5)
        if c16 is not None:
            ax.barh(yy - h / 2, c16, h, color=DARK, edgecolor=INK, lw=0.5)
            ax.text(c16 + 1.5, yy - h / 2, f'{c16:.0f}', va='center', fontsize=6.5)
        else:
            ax.text(1.5, yy - h / 2, 'cannot reach 16 Msym/s', va='center', fontsize=6.2, color=DARK)
    ax.set_yticks(list(y))
    ax.set_yticklabels([c[0] for c in CORE0][::-1], fontsize=6.5)
    ax.set_xlim(0, 100)
    ax.set_xlabel('Core 0 work (%)')
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(facecolor='#d4d4d4', edgecolor=INK, lw=0.5, label='8 Msym/s'),
                       Patch(facecolor=DARK, edgecolor=INK, lw=0.5, label='16 Msym/s')],
              loc='lower right', fontsize=6.5, handlelength=1.2)
    ax.set_title('(c) QPSK, complete pipeline', loc='left', fontsize=8.5)
    save_figure(fig, OUT / 'progress')


# ---- hero: CPU time spent on pulse shaping, from the CPU shaper to PIO/DMA ----
def _rec(path, **want):
    for line in open(ROOT / path):
        r = json.loads(line)
        if r.get('faults') == [] and all(r.get(k) == v for k, v in want.items()):
            return r
    raise SystemExit(f'no fault-free record in {path} with {want}')


def fig_offload():
    """Share of one core spent producing samples or their table addresses, by engine generation.

    CPU shaper: docs/benchmark-history.md (QPSK 2/3, L=10, 8 Msym/s, 72.0%); at 16 Msym/s it is
    computed, 10.80 cycles/symbol * 16e6 / 128e6 = 135%, more than one core. CPU-built address
    lists: gen_busy + control_busy of the dualstream controls. PIO-built addresses: the dualptr
    and bench4prod8live supervision_busy (descriptor_gen_busy and sample_math_busy are 0)."""
    d8 = _rec('results/dualptr/2026-10-15/a1-8msym-60s/runs.jsonl', cmd='dualstream', source=6)
    d16 = _rec('results/b2-8psk-live/2026-10-15/a1-16msym-60s-prio13/runs.jsonl', cmd='dualstream', source=6)
    p8 = _rec('results/dualptr/2026-10-15/a1-8msym-60s/runs.jsonl', cmd='dualptr', source=6)
    p16 = _rec('results/b2-8psk-live/2026-10-15/a1-16msym-60s-prio13/runs.jsonl', cmd='dualptr', source=6)
    b8 = _rec('results/b6-pio-producers/2026-10-15/live-60s/runs.jsonl', cpw=2, bus_priority=13)
    b16 = _rec('results/b6-pio-producers/2026-10-15/live-60s/runs.jsonl', cpw=1, bus_priority=13)
    groups = [('CPU shaper\nSep 30', 72.0, 100 * 10.80 * 16e6 / 128e6),
              ('CPU-built DMA\naddress lists\nOct 14', 100 * (d8['gen_busy'] + d8['control_busy']),
               100 * (d16['gen_busy'] + d16['control_busy'])),
              ('PIO-built\naddresses, QPSK\nOct 15', 100 * p8['supervision_busy'], 100 * p16['supervision_busy']),
              ('PIO-built\naddresses, 8PSK\nOct 15', 100 * b8['supervision_busy'], 100 * b16['supervision_busy'])]
    OFFLOAD.extend(groups)
    fig, ax = plt.subplots(figsize=(WIDTH, 2.8))
    w = 0.34
    for i, (name, v8, v16) in enumerate(groups):
        ax.bar(i - w / 2, v8, w, color='#d4d4d4', edgecolor=INK, lw=0.5)
        computed = i == 0
        ax.bar(i + w / 2, v16, w, color='white' if computed else DARK, edgecolor=INK, lw=0.5,
               hatch='////' if computed else '')
        ax.text(i - w / 2, v8 + 2, f'{v8:.1f}', ha='center', fontsize=7)
        ax.text(i + w / 2, v16 + 2, f'{v16:.0f}' if computed else f'{v16:.1f}', ha='center', fontsize=7)
    ax.axhline(100, color=MID, ls='--', lw=0.7)
    ax.text(3.55, 102, 'one whole core', ha='right', fontsize=7, color=DARK)
    ax.annotate('16 Msym/s needs 1.35 cores\n(computed; never ran)', (w, 118), xytext=(0.75, 120),
                fontsize=7, color=DARK, va='center', arrowprops=dict(arrowstyle='-', color=MID, lw=0.6))
    ax.annotate('about 3%: supervision only,\nno CPU work per sample', (2.5, 6), xytext=(2.0, 45),
                fontsize=7, color=DARK, ha='center', arrowprops=dict(arrowstyle='-', color=MID, lw=0.6))
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels([g[0] for g in groups], fontsize=7)
    ax.set_xlim(-0.6, len(groups) - 0.4)
    ax.set_ylim(0, 150)
    ax.set_ylabel('Core time spent on\npulse shaping (%)')
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(facecolor='#d4d4d4', edgecolor=INK, lw=0.5, label='8 Msym/s'),
                       Patch(facecolor=DARK, edgecolor=INK, lw=0.5, label='16 Msym/s')],
              loc='upper right', fontsize=7, bbox_to_anchor=(1.0, 0.9))
    save_figure(fig, OUT / 'offload')


OFFLOAD = []


# ---- map of what was tried ----
STAGES = [('Sep 29–Oct 2', 'CPU transmitter'), ('Oct 14', 'limits, LUT DMA'),
          ('Oct 15 a.m.', '16 Msym/s, 8PSK'), ('Oct 15 midday', 'APSK'),
          ('Oct 15 p.m.', 'R&D round 1'), ('Oct 15 eve.', 'round 2'), ('Oct 15–16', 'round 3'),
          ('Oct 16', 'round 4: combine')]
THREADS = ['Pulse shaping', 'DMA/PIO output', 'FEC and mapping', 'Memory, clock, bus', 'Higher order', 'Input']
# (thread, stage, status, label, source). Status: best = current best, done = worked but
# superseded or not adopted, found = measurement that explained something, no = failed or ruled out.
ITEMS = [
    (0, 0, 'done', 'LUT kernels, 918 → 10.8 cycles', 'docs/benchmark-history.md'),
    (0, 2, 'done', '2 SPS (images −18 dBc)', 'docs/frontier/strategy-report.md'),
    (0, 2, 'done', 'signed-plane 8PSK / VCM', 'results/waveforms/2026-10-15'),
    (0, 3, 'done', 'APSK on the CPU', 'results/apsk-fec/2026-10-15'),
    (0, 4, 'best', 'four-level 8PSK table', 'results/b1-four-level/2026-10-15'),
    (0, 4, 'no', 'APSK table in hardware (MiB)', 'docs/frontier/frontier.md'),
    (0, 6, 'best', '8-bit table rows', 'results/b4-8bit-rows/2026-10-15'),
    (1, 1, 'done', 'LUT rows by DMA', 'results/dual-dma/2026-10-14'),
    (1, 2, 'done', 'PIO addresses, cyclic', 'results/quad-pointers'),
    (1, 4, 'best', 'PIO addresses, live, 8 Msym/s', 'results/dualptr/2026-10-15'),
    (1, 4, 'no', 'same at 16 Msym/s: stalls', 'results/dualptr/2026-10-15'),
    (1, 5, 'best', 'DMA read priority: 16 Msym/s', 'results/b2-8psk-live/2026-10-15'),
    (1, 5, 'no', 'four-writer router (A2)', 'results/round2/2026-10-15'),
    (1, 6, 'no', '1 chain per axis at 16 Msym/s', 'results/b4-8bit-rows/2026-10-15'),
    (1, 6, 'done', '4 chains, SRAM addresses', 'results/b5-quad-rows/2026-10-15'),
    (1, 6, 'best', '4 PIO producers, live', 'results/b6-pio-producers/2026-10-15'),
    (2, 0, 'best', 'encoder v1 → v4, 4.1M → 237k', 'docs/benchmark-history.md'),
    (2, 4, 'best', 'streaming LDPC packer', 'results/optimizations/2026-10-15'),
    (2, 4, 'no', 'scheduled LDPC: slower live', 'archive/rd/c1-fec-throughput'),
    (2, 5, 'best', 't=8 BCH, QPSK 9/10', 'results/c2-qpsk-910/2026-10-15'),
    (2, 6, 'done', 'two-core frame-parallel FEC', 'results/c3-8psk-fec16/2026-10-15'),
    (2, 6, 'done', 'C4 kernels, −11 to −14%', 'results/c4-fec-speed/2026-10-15'),
    (2, 6, 'no', '8PSK FEC at 16 Msym/s', 'results/c4-fec-speed/2026-10-15'),
    (3, 2, 'done', 'BCH in SCRATCH_Y, −31%', 'results/memory-placement'),
    (3, 4, 'best', 'GPIO28 DDR clock', 'results/dac-clock/2026-10-15'),
    (3, 5, 'best', 'core-0 stack overflow fixed', 'results/round2/2026-10-15/STACK.md'),
    (3, 6, 'found', 'DMA row loop 10–18 cycles', 'results/dmaloop/2026-10-15'),
    (3, 6, 'found', 'two cores clash in SCRATCH_Y', 'results/ldpc-banks/2026-10-15'),
    (3, 6, 'done', 'self-re-arming DMA grants', 'results/r2-hw-offload/2026-10-15'),
    (3, 6, 'no', '256 MHz overclock', 'not pursued'),
    (4, 2, 'done', '8PSK and VCM at 150 MHz', 'results/waveforms/2026-10-15'),
    (4, 3, 'done', '16APSK / 32APSK complete', 'results/apsk-fec/2026-10-15'),
    (4, 4, 'found', 'APSK below QPSK-16', 'docs/frontier/frontier.md'),
    (4, 6, 'best', '8PSK complete, 8 Msym/s', 'results/b7-live8/2026-10-15'),
    (4, 6, 'done', '8PSK waveform, 16 Msym/s', 'results/b6-pio-producers/2026-10-15'),
    (5, 0, 'best', 'PV-SPI from a CM5, 210 s', 'results/cm5-spi'),
    (5, 2, 'done', 'PIO/DMA SPI ingress, 32 MHz', 'results/dual-dma/2026-10-15'),
    (5, 6, 'no', 'USB input, hardware FEC', 'docs/rd/r2-hw-offload.md'),
    (0, 7, 'best', '5 SPS: 12.8 Msym/s', 'results/n5-12m8/2026-10-16'),
    (1, 7, 'best', 'IRQ grant re-arm', 'results/b9-8psk16/2026-10-16'),
    (2, 7, 'best', 'C5–C8: 2/3 at 16 Msym/s', 'results/c8-fec-combined/2026-10-16'),
    (2, 7, 'done', 'C9: 5/6 −14k cycles', 'results/c9-fec-fit/2026-10-16'),
    (3, 7, 'found', 'XIP cache-set clash', 'results/b10-8psk16-rates/2026-10-16'),
    (3, 7, 'found', 'gate timed startup', 'results/c8-fec-combined/2026-10-16'),
    (4, 7, 'best', '8PSK 2/3, 16 Msym/s', 'results/b9-8psk16/2026-10-16'),
    (4, 7, 'best', '8PSK 5/6, 16 Msym/s*', 'results/b10-8psk16-rates/2026-10-16'),
    (4, 7, 'no', '8PSK 3/4: dry at 46 s', 'results/b10-8psk16-rates/2026-10-16'),
    (4, 7, 'best', 'APSK mapper +14/+33%', 'results/a3-apsk/2026-10-16'),
    (5, 7, 'best', '4-bit ingress, 47.8 Mb/s', 'results/ingress-wide/2026-10-16'),
    (5, 7, 'best', 'TS adapter, core 1 −25 pts', 'results/q1-qpsk/2026-10-16'),
]
STYLE = {'best': dict(marker='o', mfc=INK, mec=INK), 'done': dict(marker='o', mfc='white', mec=INK),
         'found': dict(marker='D', mfc='white', mec=DARK), 'no': dict(marker='x', mfc='none', mec=INK)}
LEGEND = {'best': 'current best', 'done': 'worked, superseded or not adopted',
          'found': 'measurement that explained a limit', 'no': 'failed or ruled out'}


def fig_map():
    ncol, nrow = len(STAGES), len(THREADS)
    fig, ax = plt.subplots(figsize=(WIDTH * 1.6, 5.6))
    for c in range(ncol + 1):
        ax.axvline(c, color='#e2e2e2', lw=0.6, zorder=0)
    for r in range(nrow + 1):
        ax.axhline(r, color=LIGHT, lw=0.5, zorder=0)
    cells = {}
    for t, s, *_ in ITEMS:
        cells[(t, s)] = cells.get((t, s), 0) + 1
    placed = {}
    for t, s, status, label, _ in ITEMS:
        k, n = placed.get((t, s), 0), cells[(t, s)]
        placed[(t, s)] = k + 1
        y = nrow - 1 - t + (n - k) / (n + 1)
        x = s + 0.07
        ax.plot(x, y, ls='none', ms=4.2, mew=0.8, color=INK, **STYLE[status])
        ax.text(x + 0.07, y, label, va='center', fontsize=6.0, color=INK if status != 'no' else DARK)
    ax.set_xlim(0, ncol)
    ax.set_ylim(0, nrow)
    ax.set_xticks([c + 0.5 for c in range(ncol)])
    ax.set_xticklabels([f'{d}\n{w}' for d, w in STAGES], fontsize=6.8)
    ax.xaxis.tick_top()
    ax.tick_params(axis='x', length=0, pad=4)
    ax.set_yticks([nrow - 1 - r + 0.5 for r in range(nrow)])
    ax.set_yticklabels(THREADS, fontsize=7.5)
    ax.tick_params(axis='y', length=0)
    ax.legend(handles=[Line2D([], [], ls='none', ms=4.5, mew=0.8, color=INK, label=LEGEND[k], **STYLE[k])
                       for k in STYLE], loc='upper center', bbox_to_anchor=(0.5, -0.02), ncol=4, fontsize=7)
    ax.text(len(STAGES), -0.62, '* own image (tag archive/b10-8psk16-rates); not yet in the combined main image',
            ha='right', va='top', fontsize=6.4, color=DARK)
    save_figure(fig, OUT / 'experiment_map')


if __name__ == '__main__':
    fig_offload()
    fig_progress()
    fig_map()
    (OUT / 'history.json').write_text(json.dumps(dict(
        generator='host/plot_history.py',
        offload_percent_of_core=OFFLOAD, shaper_cycles_per_symbol=SHAPER, fec_kcycles=FEC, core0=CORE0,
        items=[dict(thread=THREADS[t], stage=' '.join(STAGES[s]), status=st, label=l, source=src)
               for t, s, st, l, src in ITEMS]), indent=2, ensure_ascii=False) + '\n')
    print(f'wrote {OUT.relative_to(ROOT)}')
