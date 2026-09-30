"""Figures from results/optimization-log.jsonl -> results/plots/progress_*.png.

Only runs without faults (host/iqbench.py stream_faults) are plotted.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
sys.path.insert(0, str(ROOT / "host"))
from plotstyle import (FOAM, GOLD, IRIS, LOVE, ROSE, SUBTLE, WIDTH, dots, lollipop,  # noqa: E402
                       matplotx, plt)
from iqbench import stream_faults  # noqa: E402

LOG, PLOTS = ROOT / "results" / "optimization-log.jsonl", ROOT / "results" / "plots"
RS, CLK = 8e6, 128_000_000


def load():
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


def latest(rows, pred):
    hit = [r for r in rows if pred(r)]
    return hit[-1] if hit else None


def clean(r):
    return not stream_faults(r, r.get("cap_words", 0)) and not r.get("faults")


def note(r, s):
    return s in (r.get("note") or "")


def budget(ax, y, x, text):
    ax.axhline(y, color=LOVE, lw=1.2, ls=(0, (5, 4)), zorder=1)
    ax.annotate(text, (x, y), xytext=(0, 5), textcoords="offset points", color=LOVE, fontsize=9.5)


def pct_axis(ax, n):
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100 %"])
    ax.set_ylim(-0.6, n - 0.4)
    ax.grid(axis="x"), ax.grid(axis="y", visible=False)


def title(fig, text):
    fig.suptitle(text, x=0.0, ha="left", fontsize=13, fontweight="semibold")


def save(fig, name):
    fig.tight_layout()
    fig.savefig(PLOTS / name)
    plt.close(fig)


def kernel_progress(rows):
    steps = ["conv", "lut_shift", "lut_win", "lut_pair", "lut_asm", "lut_asm_p"]

    def cost(k, sps):
        r = latest(rows, lambda r: r["cmd"] == "bench" and r["kernel"] == k and r["sps"] == sps
                   and r["L"] == 10 and r["clk_hz"] == CLK and r.get("crc_ok")
                   and not note(r, "-O2") and not note(r, "-Os"))
        return r["cyc_per_sym"] if r else None

    fig, ax = plt.subplots(figsize=(WIDTH, 3.8))
    for sps, col in ((4, FOAM), (2, IRIS)):
        pts = [(i, cost(k, sps)) for i, k in enumerate(steps) if cost(k, sps)]
        dots(ax, [p[0] for p in pts], [p[1] for p in pts], col, f"{sps} samples/symbol  {pts[-1][1]:.1f}")
    budget(ax, 16, -0.1, "budget")
    ax.set_yscale("log")
    ax.set_yticks([10, 100, 1000], ["10", "100", "1000"])
    ax.minorticks_off()
    ax.set_ylim(5, 1500)
    ax.set_xticks(range(len(steps)), [f"v{i}" for i in range(len(steps))])
    ax.set_xlim(-0.25, 5.25)
    title(fig, "Shaper kernel")
    matplotx.ylabel_top("cycles / symbol")
    matplotx.line_labels(fontsize=9.5)
    save(fig, "progress_kernels.png")


def stream_load(rows):
    def st(r, **kw):
        return r["cmd"] == "stream" and "s2_code" not in r and all(r.get(k) == v for k, v in kw.items())
    cases = [
        ("v4, code in SRAM0-3", lambda r: st(r, kernel="lut_asm", sps=4, L=8, cores="0", lanes=4) and note(r, "placement")),
        ("v4, code in SRAM8", lambda r: st(r, kernel="lut_asm", sps=4, L=10, cores="0", lanes=4) and note(r, "placement")),
        ("v5", lambda r: st(r, kernel="lut_asm_p", sps=4, L=10, cores="0", lanes=4) and note(r, "v5")),
        ("v4, two cores", lambda r: st(r, kernel="lut_asm", sps=4, L=10, cores="01", lanes=4)),
        ("1 Msym/s", lambda r: st(r, kernel="lut_asm", sps=8, L=10, cores="0", lanes=1)),
    ]
    cases = [(n, latest(rows, lambda r, p=p: p(r) and clean(r))) for n, p in cases]
    cases = [(n, r) for n, r in cases if r][::-1]
    fig, ax = plt.subplots(figsize=(WIDTH, 3.0))
    for y, (n, r) in enumerate(cases):
        busy = [c["busy"] * 100 for c in r["core"]]
        if len(busy) == 1:
            lollipop(ax, y, busy[0], FOAM)
        else:                                   # one dot per core, joined
            ax.hlines(y, 0, min(busy), color=IRIS, lw=1.6, alpha=0.55)
            ax.hlines(y, min(busy), max(busy), color=IRIS, lw=5, alpha=0.9)
            ax.plot(busy, [y] * len(busy), "o", color=IRIS)
            ax.annotate(" / ".join(f"{b:.0f}" for b in busy) + " %", (max(busy), y), xytext=(9, 0),
                        textcoords="offset points", va="center", fontsize=9.5)
    ax.set_yticks(range(len(cases)), [n for n, _ in cases])
    pct_axis(ax, len(cases))
    title(fig, "Shaper load")
    save(fig, "progress_streaming.png")


def dvbs2_progress(rows):
    recs = [latest(rows, lambda r, k=k: r["cmd"] == "dvbs2" and r["code"] == "normal 2/3" and r.get("crc_ok")
                   and (r.get("note") or "").startswith(k))
            for k in ("DVB-S2 encoder v1", "DVB-S2 v2", "DVB-S2 v3", "DVB-S2 v4")]
    recs = [r for r in recs if r]
    if not recs:
        return
    x = range(len(recs))
    frame = [r["cyc_frame"] / 1e3 for r in recs]
    bch = [r["cyc_bch"] / 1e3 for r in recs]
    ldpc = [r["cyc_ldpc"] / 1e3 for r in recs]
    rest = [f - b - l for f, b, l in zip(frame, bch, ldpc)]
    fig, ax = plt.subplots(figsize=(WIDTH, 3.8))
    dots(ax, x, frame, FOAM, "frame", lw=2.4, ms=8.5)
    for v, col, lab in ((ldpc, GOLD, "LDPC"), (rest, ROSE, "framing"), (bch, IRIS, "BCH")):
        dots(ax, x, v, col, lab, lw=1.5, ms=6)
    limit = recs[-1]["clk_hz"] * recs[-1]["syms"] / RS / 1e3
    for i, f in enumerate(frame):                       # value above the dot unless the budget line is there
        below = 0 < limit - f < 100
        ax.annotate(f"{f:.0f}k", (i, f), xytext=(0, -12 if below else 9), textcoords="offset points",
                    ha="center", va="top" if below else "bottom", fontsize=9.5)
    budget(ax, limit, -0.1, "budget, 8 Msym/s")
    ax.set_ylim(0, 880)
    ax.set_xticks(x, [f"v{i + 1}" for i in x])
    ax.set_xlim(-0.2, len(recs) - 0.8)
    title(fig, "DVB-S2 encoder")
    matplotx.ylabel_top("k cycles / frame")
    matplotx.line_labels(fontsize=9.5)
    save(fig, "progress_dvbs2.png")


def full_tx(rows):
    def tx(code, rate, sps, pv):
        runs = [r for r in rows if r["cmd"] == "stream" and r.get("s2_code") == code and ("pv" in r) == pv
                and abs(r["sym_rate"] - rate) < 1 and r["sps"] == sps and clean(r)]
        return max(runs, key=lambda r: r["ms"]) if runs else None
    cases = [("PigeonVision, SPI input", tx("normal 2/3", 8e6, 4, True)),
             ("PigeonVision", tx("normal 2/3", 8e6, 4, False)),
             ("Short frames", tx("short 1/2", 8e6, 4, False)),
             ("SATS, 1 Msym/s", tx("normal 1/2", 1e6, 8, False))]
    cases = [(n, r) for n, r in cases if r][::-1]
    if not cases:
        return
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.9), sharey=True)
    for ax, head, col, busy in ((axes[0], "Shaper, core 0", FOAM, lambda r: r["core"][0]["busy"]),
                                 (axes[1], "Encoder, core 1", IRIS, lambda r: r["s2_busy"])):
        for y, (_, r) in enumerate(cases):
            lollipop(ax, y, busy(r) * 100, col)
        pct_axis(ax, len(cases))
        ax.set_title(head, fontsize=11, color=SUBTLE, fontweight="medium", pad=10)
    axes[0].set_yticks(range(len(cases)), [n for n, _ in cases])
    title(fig, "Transmitter load")
    save(fig, "progress_full_tx.png")


if __name__ == "__main__":
    PLOTS.mkdir(parents=True, exist_ok=True)
    rows = load()
    kernel_progress(rows)
    stream_load(rows)
    dvbs2_progress(rows)
    full_tx(rows)
    print(f"{len(rows)} log records -> {PLOTS.relative_to(ROOT)}")
