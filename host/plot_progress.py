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


if __name__ == "__main__":
    PLOTS.mkdir(parents=True, exist_ok=True)
    rows = load()
    kernel_progress(rows)
    stream_load(rows)
    print(f"{len(rows)} log records -> {PLOTS.relative_to(ROOT)}")
