"""Figures from results/optimization-log.jsonl -> results/plots/progress_* (PNG/SVG/PDF).

Only runs without faults (host/iqbench.py stream_faults) are plotted.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
sys.path.insert(0, str(ROOT / "host"))
from plotstyle import BLUE, DARK, GREEN, INK, MID, RED, WIDTH, dots, lollipop, plt, save_figure  # noqa: E402
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
    ax.axhline(y, color=MID, lw=0.7, ls=(0, (6, 3)), zorder=1)
    ax.annotate(text, (x, y), xytext=(0, 4), textcoords="offset points", color=DARK, fontsize=8)


def pct_axis(ax, n):
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("Processing load (%)")
    ax.set_ylim(-0.6, n - 0.4)
    ax.grid(axis="x"), ax.grid(axis="y", visible=False)


def title(fig, text):
    fig.suptitle(text, fontsize=10, fontweight="normal")


def save(fig, name, caption=None):
    save_figure(fig, PLOTS / name, caption)


def kernel_progress(rows):
    steps = ["conv", "lut_shift", "lut_win", "lut_pair", "lut_asm", "lut_asm_p"]

    def cost(k, sps):
        r = latest(rows, lambda r: r["cmd"] == "bench" and r["kernel"] == k and r["sps"] == sps
                   and r["L"] == 10 and r["clk_hz"] == CLK and r.get("crc_ok")
                   and not note(r, "-O2") and not note(r, "-Os"))
        return r["cyc_per_sym"] if r else None

    fig, ax = plt.subplots(figsize=(WIDTH, 2.7))
    for sps, col, marker, ls in ((4, BLUE, "o", "-"), (2, RED, "s", "--")):
        pts = [(i, cost(k, sps)) for i, k in enumerate(steps) if cost(k, sps)]
        dots(ax, [p[0] for p in pts], [p[1] for p in pts], col,
             f"{sps} samples/symbol", marker=marker, linestyle=ls)
        ax.annotate(f"{pts[-1][1]:.1f}", pts[-1], xytext=(0, -6), textcoords="offset points",
                    ha="center", va="top", fontsize=8)
    budget(ax, 16, 0.1, "16-cycle budget")
    ax.set_yscale("log")
    ax.set_yticks([10, 100, 1000], ["10", "100", "1000"])
    ax.minorticks_off()
    ax.set_ylim(3, 1500)
    ax.set_xticks(range(len(steps)), ["v0\nConvolution", "v1\nShift LUT", "v2\nWindow LUT",
                                    "v3\nPIO interleave", "v4\nAssembly", "v5\nPipelined"])
    ax.set_xlim(-0.25, 5.25)
    title(fig, "Shaper kernel")
    ax.set_ylabel("Cycles per symbol")
    ax.legend(loc="upper right")
    save(fig, "progress_kernels.png", "RP2350, 128 MHz; filter span L = 10 symbols. Budget: one core at 8 Msym/s.")


def stream_load(rows):
    def st(r, **kw):
        return r["cmd"] == "stream" and "s2_code" not in r and all(r.get(k) == v for k, v in kw.items())
    cases = [
        ("v4, SRAM0–3 (L = 8)", lambda r: st(r, kernel="lut_asm", sps=4, L=8, cores="0", lanes=4) and note(r, "placement")),
        ("v4, SRAM8 (L = 10)", lambda r: st(r, kernel="lut_asm", sps=4, L=10, cores="0", lanes=4) and note(r, "placement")),
        ("v5", lambda r: st(r, kernel="lut_asm_p", sps=4, L=10, cores="0", lanes=4) and note(r, "v5")),
        ("v4, two cores", lambda r: st(r, kernel="lut_asm", sps=4, L=10, cores="01", lanes=4)),
        ("1 Msym/s", lambda r: st(r, kernel="lut_asm", sps=8, L=10, cores="0", lanes=1)),
    ]
    cases = [(n, latest(rows, lambda r, p=p: p(r) and clean(r))) for n, p in cases]
    cases = [(n, r) for n, r in cases if r][::-1]
    fig, ax = plt.subplots(figsize=(WIDTH, 2.6))
    for y, (n, r) in enumerate(cases):
        busy = [c["busy"] * 100 for c in r["core"]]
        if len(busy) == 1:
            lollipop(ax, y, busy[0], BLUE)
        else:                                   # one dot per core, joined
            for c, (value, marker, col) in enumerate(zip(busy, ("o", "s"), (BLUE, RED))):
                yy = y + (c - 0.5) * 0.16
                ax.hlines(yy, 0, value, color=col, lw=0.6)
                ax.plot([value], [yy], marker=marker, color=col, ms=3.2)
            ax.annotate(" / ".join(f"{b:.0f}" for b in busy) + " %", (max(busy), y), xytext=(5, 0),
                        textcoords="offset points", va="center", fontsize=8)
    ax.set_yticks(range(len(cases)), [n for n, _ in cases])
    pct_axis(ax, len(cases))
    title(fig, "Shaper load")
    save(fig, "progress_streaming.png", "128 MHz; 8 Msym/s at 4 samples/symbol, except the 1 Msym/s row (8 samples/symbol).\n"
         "Shaper only; the two-core row reports core 0 / core 1. Filter span differs in the placement rows.")


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
    fig, ax = plt.subplots(figsize=(WIDTH, 2.7))
    dots(ax, x, frame, INK, "Frame total")
    for v, col, lab, marker, ls in ((ldpc, BLUE, "LDPC", "^", "-."),
                                    (rest, GREEN, "Other framing", "D", ":"),
                                    (bch, RED, "BCH", "s", "--")):
        dots(ax, x, v, col, lab, marker=marker, linestyle=ls)
    limit = recs[-1]["clk_hz"] * recs[-1]["syms"] / RS / 1e3
    for i, f in enumerate(frame):                       # value above the dot unless the budget line is there
        below = 0 < limit - f < 100
        ax.annotate(f"{f:.0f}k", (i, f), xytext=(0, -12 if below else 9), textcoords="offset points",
                    ha="center", va="top" if below else "bottom", fontsize=8)
    budget(ax, limit, 1.45, "One-core budget at 8 Msym/s")
    ax.set_ylim(0, 960)
    ax.set_xticks(x, [f"v{i + 1}" for i in x])
    ax.set_xlim(-0.2, len(recs) - 0.8)
    title(fig, "DVB-S2 encoder")
    ax.set_ylabel("Cycles per frame (×10³)")
    ax.set_xlabel("Encoder revision")
    ax.legend(loc="upper right", ncol=2)
    save(fig, "progress_dvbs2.png", "RP2350, 128 MHz; DVB-S2 normal QPSK 2/3 with pilots. CRC-verified benchmarks.")


def full_tx(rows):
    def tx(code, rate, sps, pv):
        runs = [r for r in rows if r["cmd"] == "stream" and r.get("s2_code") == code and ("pv" in r) == pv
                and abs(r["sym_rate"] - rate) < 1 and r["sps"] == sps and clean(r)]
        return max(runs, key=lambda r: (r["ms"], r["time"])) if runs else None   # newest of the longest
    cases = [("PigeonVision, SPI input", tx("normal 2/3", 8e6, 4, True)),
             ("PigeonVision", tx("normal 2/3", 8e6, 4, False)),
             ("Short frames", tx("short 1/2", 8e6, 4, False)),
             ("SATS, 1 Msym/s", tx("normal 1/2", 1e6, 8, False))]
    cases = [("PigeonVision, SPI self-test" if r.get("pv", {}).get("selftest") else n, r)
             for n, r in cases if r][::-1]
    if not cases:
        return
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.5), sharey=True)
    for ax, head, col, busy in ((axes[0], "Shaper, core 0", BLUE, lambda r: r["core"][0]["busy"]),
                                 (axes[1], "Encoder, core 1", RED, lambda r: r["s2_busy"])):
        for y, (_, r) in enumerate(cases):
            lollipop(ax, y, busy(r) * 100, col)
        pct_axis(ax, len(cases))
        ax.set_title(head)
    axes[0].set_yticks(range(len(cases)), [n for n, _ in cases])
    title(fig, "Transmitter load")
    save(fig, "progress_full_tx.png", "128 MHz; 8 Msym/s at 4 samples/symbol, except SATS (1 Msym/s, 8 samples/symbol). SPI input uses the\n"
         "L = 12 windowed shaper, the others L = 10. Newest of the longest clean runs; encoding includes TS assembly when used.")


if __name__ == "__main__":
    PLOTS.mkdir(parents=True, exist_ok=True)
    rows = load()
    kernel_progress(rows)
    stream_load(rows)
    dvbs2_progress(rows)
    full_tx(rows)
    print(f"{len(rows)} log records -> {PLOTS.relative_to(ROOT)}")
