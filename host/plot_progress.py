"""Plot optimization progress from results/optimization-log.jsonl into results/plots/.

Kernel cost is shown against the per-core budget  B = f_clk / R_s  cycles per symbol
(16 at 128 MHz and 8 Msym/s); a kernel under B keeps up on one core, ignoring I/O overhead.
"""
import json
from collections import OrderedDict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
LOG, PLOTS = ROOT / "results" / "optimization-log.jsonl", ROOT / "results" / "plots"
RS = 8e6


def load():
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


def kernel_progress(rows):
    bench = [r for r in rows if r["cmd"] == "bench" and r.get("crc_ok")]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=False)
    for ax, sps in zip(axes, (2, 4)):
        latest = OrderedDict()                  # kernel -> last measurement, in first-seen order
        for r in bench:
            if r["sps"] == sps and r["L"] == 10:
                latest.setdefault(r["kernel"], r)
                latest[r["kernel"]] = r
        names = list(latest)
        vals = [latest[k]["cyc_per_sym"] for k in names]
        bars = ax.bar(names, vals, color="0.55")
        clk = latest[names[-1]]["clk_hz"] if names else 128e6
        budget = clk / RS
        ax.axhline(budget, color="C3", lw=1)
        ax.text(len(names) - 0.5, budget, f" 1-core budget {budget:.0f}", color="C3",
                va="bottom", ha="right", fontsize=8)
        ax.set_yscale("log")
        ax.bar_label(bars, fmt="%.1f", fontsize=8)
        ax.set(title=f"sps={sps}, L=10, {clk / 1e6:.0f} MHz", ylabel="cycles / symbol (measured)")
        ax.tick_params(axis="x", rotation=20)
    fig.suptitle("Kernel cost by optimization step (lower is better)")
    fig.tight_layout()
    fig.savefig(PLOTS / "progress_kernels.png", dpi=130)


def stream_load(rows):
    st = [r for r in rows if r["cmd"] == "stream"]
    if not st:
        return
    fig, ax = plt.subplots(figsize=(8, 3.5))
    labels, c0, c1 = [], [], []
    for r in st:
        busy = {c["id"]: c["busy"] * 100 for c in r["core"]}
        ok = (r["underruns"] == 0 and r["own_errors"] == 0 and r["txstalls"] == 0
              and r.get("cap_mismatches", 0) == 0)
        labels.append(f"{r['kernel']} sps{r['sps']} L{r['L']}\ncores {r['cores']} "
                      f"{r['ms'] / 1e3:g}s{'' if ok else ' FAIL'}")
        c0.append(busy.get(0, 0))
        c1.append(busy.get(1, 0))
    x = range(len(labels))
    ax.bar([i - 0.2 for i in x], c0, 0.4, label="core 0")
    ax.bar([i + 0.2 for i in x], c1, 0.4, label="core 1")
    ax.axhline(100, color="C3", lw=1)
    ax.set_xticks(list(x), labels, fontsize=7)
    ax.set(ylabel="core busy in waveform generation (%)",
           title="Continuous streaming at 8 Msym/s (DMA + PIO running)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(PLOTS / "progress_streaming.png", dpi=130)


def dvbs2_progress(rows):
    """Per-frame encoder cycles for normal QPSK 2/3 + pilots, one bar per logged optimization step."""
    recs = [r for r in rows if r["cmd"] == "dvbs2" and r["code"] == "normal 2/3" and r["crc_ok"]]
    if not recs:
        return
    steps = OrderedDict()
    for r in recs:
        steps[(r.get("note") or "").split(":")[0]] = r      # last record per step
    labels = list(steps)
    bch = [steps[k]["cyc_bch"] / 1e3 for k in labels]
    ldpc = [steps[k]["cyc_ldpc"] / 1e3 for k in labels]
    rest = [steps[k]["cyc_frame"] / 1e3 - b - l for k, b, l in zip(labels, bch, ldpc)]
    clk, syms = recs[-1]["clk_hz"], recs[-1]["syms"]
    budget = clk * syms / RS / 1e3                          # one core at 8 Msym/s, k cycles/frame
    fig, ax = plt.subplots(figsize=(8, 4))
    x = range(len(labels))
    ax.bar(x, bch, label="BCH")
    ax.bar(x, ldpc, bottom=bch, label="LDPC")
    ax.bar(x, rest, bottom=[b + l for b, l in zip(bch, ldpc)], label="scramble, map, PL framing")
    for i, k in enumerate(labels):
        ax.text(i, steps[k]["cyc_frame"] / 1e3, f"{steps[k]['cyc_frame'] / 1e3:.0f}k", ha="center",
                va="bottom", fontsize=8)
    ax.axhline(budget, color="C3", lw=1)
    ax.text(len(labels) - 0.5, budget, f" 1 core @ 8 Msym/s = {budget:.0f}k", color="C3",
            ha="right", va="bottom", fontsize=8)
    ax.set_xticks(list(x), labels, fontsize=8)
    ax.set(ylabel="cycles per PLFRAME (thousands)",
           title=f"DVB-S2 encoder on RP2350, normal QPSK 2/3 + pilots, {clk / 1e6:.0f} MHz")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(PLOTS / "progress_dvbs2.png", dpi=130)


def full_tx(rows):
    recs = [r for r in rows if r["cmd"] == "stream" and "s2_code" in r and r.get("cap_mismatches") == 0
            and r["underruns"] == 0]
    if not recs:
        return
    latest = OrderedDict()
    for r in recs:
        latest[(r["s2_code"], round(r["sym_rate"]), r["sps"])] = r
    fig, ax = plt.subplots(figsize=(7, 3.5))
    labels = [f"{c}\n{rate / 1e6:g} Msym/s, N={sps}" for c, rate, sps in latest]
    enc = [r["s2_busy"] * 100 for r in latest.values()]
    shp = [r["core"][0]["busy"] * 100 for r in latest.values()]
    x = range(len(labels))
    ax.bar([i - 0.2 for i in x], shp, 0.4, label="core 0: shaper + DMA/PIO")
    ax.bar([i + 0.2 for i in x], enc, 0.4, label="core 1: DVB-S2 encoder")
    ax.axhline(100, color="C3", lw=1)
    ax.set_xticks(list(x), labels, fontsize=8)
    ax.set(ylabel="core busy (%)", title="Full on-chip DVB-S2 transmitter (measured, capture exact)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(PLOTS / "progress_full_tx.png", dpi=130)


if __name__ == "__main__":
    PLOTS.mkdir(parents=True, exist_ok=True)
    rows = load()
    kernel_progress(rows)
    stream_load(rows)
    dvbs2_progress(rows)
    full_tx(rows)
    print(f"{len(rows)} log records -> {PLOTS.relative_to(ROOT)}")
