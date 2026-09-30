"""Figures from results/optimization-log.jsonl -> results/plots/progress_*.png.

Only runs without faults (host/iqbench.py stream_faults) are plotted.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
sys.path.insert(0, str(ROOT / "host"))
from plotstyle import CYAN, PINK, VIOLET, bar_values, budget, plt  # noqa: E402
from iqbench import stream_faults  # noqa: E402

LOG, PLOTS = ROOT / "results" / "optimization-log.jsonl", ROOT / "results" / "plots"
RS = 8e6


def load():
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


def latest(rows, pred):
    hit = [r for r in rows if pred(r)]
    return hit[-1] if hit else None


def clean(r):
    return not stream_faults(r, r.get("cap_words", 0))


def note(r, s):
    return s in (r.get("note") or "")


def pair(ax, labels, a, b, la, lb):
    x = range(len(labels))
    b1 = ax.bar([i - 0.19 for i in x], a, 0.36, color=CYAN, label=la)
    b2 = ax.bar([i + 0.19 for i in x], b, 0.36, color=VIOLET, label=lb)
    bar_values(ax, b1), bar_values(ax, b2)
    ax.set_xticks(list(x), labels)
    ax.set_ylim(0, 105)
    ax.set_ylabel("core busy (%)")
    ax.legend(loc="upper right", ncol=2)


def kernel_progress(rows):
    steps = ["conv", "lut_shift", "lut_win", "lut_pair", "lut_asm", "lut_asm_p"]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), sharey=True)
    for ax, sps in zip(axes, (2, 4)):
        names, vals = [], []
        for i, k in enumerate(steps):
            r = latest(rows, lambda r: r["cmd"] == "bench" and r["kernel"] == k and r["sps"] == sps
                       and r["L"] == 10 and r["clk_hz"] == 128_000_000 and r.get("crc_ok")
                       and not note(r, "-O2") and not note(r, "-Os"))
            if r:
                names.append(f"v{i}")
                vals.append(r["cyc_per_sym"])
        best = vals.index(min(vals))
        bars = ax.bar(names, vals, 0.62, color=[PINK if i == best else CYAN for i in range(len(vals))])
        bar_values(ax, bars, "{:.1f}")
        budget(ax, 16, "budget")
        ax.set_yscale("log")
        ax.set_ylim(4, 3000)
        ax.set_title(f"shaper  N = {sps}")
    axes[0].set_ylabel("cycles / symbol")
    fig.tight_layout(w_pad=3)
    fig.savefig(PLOTS / "progress_kernels.png")
    plt.close(fig)


def stream_load(rows):
    def st(r, **kw):
        return r["cmd"] == "stream" and "s2_code" not in r and all(r.get(k) == v for k, v in kw.items())
    cases = [
        ("v4\nSRAM0-3", lambda r: st(r, kernel="lut_asm", sps=4, L=8, cores="0", lanes=4) and note(r, "placement")),
        ("v4\nSRAM8", lambda r: st(r, kernel="lut_asm", sps=4, L=10, cores="0", lanes=4) and note(r, "placement")),
        ("v5\nSRAM8", lambda r: st(r, kernel="lut_asm_p", sps=4, L=10, cores="0", lanes=4) and note(r, "v5")),
        ("v4\n2 cores", lambda r: st(r, kernel="lut_asm", sps=4, L=10, cores="01", lanes=4)),
        ("N = 8\n1 Msym/s", lambda r: st(r, kernel="lut_asm", sps=8, L=10, cores="0", lanes=1)),
    ]
    cases = [(n, latest(rows, lambda r, p=p: p(r) and clean(r))) for n, p in cases]
    cases = [(n, r) for n, r in cases if r]
    busy = [{c["id"]: c["busy"] * 100 for c in r["core"]} for _, r in cases]
    fig, ax = plt.subplots(figsize=(7, 3.4))
    pair(ax, [n for n, _ in cases], [b.get(0, 0) for b in busy], [b.get(1, 0) for b in busy], "core 0", "core 1")
    ax.set_title("streaming load  8 Msym/s")
    fig.tight_layout()
    fig.savefig(PLOTS / "progress_streaming.png")
    plt.close(fig)


def dvbs2_progress(rows):
    recs = [latest(rows, lambda r, k=k: r["cmd"] == "dvbs2" and r["code"] == "normal 2/3" and r.get("crc_ok")
                   and (r.get("note") or "").startswith(k))
            for k in ("DVB-S2 encoder v1", "DVB-S2 v2", "DVB-S2 v3", "DVB-S2 v4")]
    recs = [r for r in recs if r]
    if not recs:
        return
    bch = [r["cyc_bch"] / 1e3 for r in recs]
    ldpc = [r["cyc_ldpc"] / 1e3 for r in recs]
    rest = [r["cyc_frame"] / 1e3 - b - l for r, b, l in zip(recs, bch, ldpc)]
    x = [f"v{i + 1}" for i in range(len(recs))]
    fig, ax = plt.subplots(figsize=(6, 3.6))
    ax.bar(x, bch, 0.6, color=CYAN, label="BCH")
    ax.bar(x, ldpc, 0.6, bottom=bch, color=VIOLET, label="LDPC")
    top = ax.bar(x, rest, 0.6, bottom=[b + l for b, l in zip(bch, ldpc)], color=PINK, label="framing")
    for b, r in zip(top, recs):
        ax.annotate(f"{r['cyc_frame'] / 1e3:.0f}k", (b.get_x() + b.get_width() / 2, r["cyc_frame"] / 1e3),
                    xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontsize=7.5)
    budget(ax, recs[-1]["clk_hz"] * recs[-1]["syms"] / RS / 1e3, "1 core @ 8 Msym/s")
    ax.set_ylim(0, 900)
    ax.set_ylabel("k cycles / frame")
    ax.set_title("DVB-S2 encoder  normal 2/3")
    ax.legend(loc="upper right", ncol=3, bbox_to_anchor=(1, 1.02))
    fig.tight_layout()
    fig.savefig(PLOTS / "progress_dvbs2.png")
    plt.close(fig)


def full_tx(rows):
    cases = []
    for code, rate, sps in [("normal 2/3", 8e6, 4), ("short 1/2", 8e6, 4), ("normal 1/2", 1e6, 8),
                            ("short 1/2", 1e6, 8)]:
        runs = [r for r in rows if r["cmd"] == "stream" and r.get("s2_code") == code
                and abs(r["sym_rate"] - rate) < 1 and r["sps"] == sps and clean(r)]
        if runs:
            cases.append((f"{code}\n{rate / 1e6:g} Msym/s", max(runs, key=lambda r: r["ms"])))
    if not cases:
        return
    fig, ax = plt.subplots(figsize=(7, 3.4))
    pair(ax, [n for n, _ in cases], [r["core"][0]["busy"] * 100 for _, r in cases],
         [r["s2_busy"] * 100 for _, r in cases], "shaper", "encoder")
    ax.set_title("full DVB-S2 transmitter")
    fig.tight_layout()
    fig.savefig(PLOTS / "progress_full_tx.png")
    plt.close(fig)


if __name__ == "__main__":
    PLOTS.mkdir(parents=True, exist_ok=True)
    rows = load()
    kernel_progress(rows)
    stream_load(rows)
    dvbs2_progress(rows)
    full_tx(rows)
    print(f"{len(rows)} log records -> {PLOTS.relative_to(ROOT)}")
