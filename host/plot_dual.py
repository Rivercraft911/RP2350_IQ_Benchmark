"""Plot the isolated direct-DMA experiments without changing historical progress figures.

Examples:
  python3 host/plot_dual.py --root results/dual-dma/2026-10-14 --check
  python3 host/plot_dual.py --runs results/dual-dma/2026-10-14/soak-1/runs.jsonl

Writes dual_dma_load, dual_dma_rates, dual_dma_onecore and dual_dma_pipeline as PNG/SVG/PDF
in --out (results/plots by default), plus selected records in dual_dma_selection.json. --check only
prints selection/provenance; --self-test checks the selection rules with synthetic data.
The rate panel contains real repeated DVB-S2 encoding, never precomputed descriptors.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))

DEFAULT_ROOT = ROOT / "results/dual-dma"
FAULT_FIELDS = ("errors", "underruns", "own_errors", "txstalls_i", "txstalls_q",
                "txstalls", "link_overruns", "input_overruns")
LABEL = re.compile(r"^dual(?:-row(?P<row>[12]))?-(?P<clock>\d+)-src(?P<source>[012345])"
                   r"-k(?P<kernel>[012])-cpw(?P<cpw>[1-4])-fault(?P<fault>[012])"
                   r"(?:-sched(?P<schedule>[0-3])-div(?P<divider>\d+))?(?:-|$)")


def load(paths: list[Path]) -> list[dict]:
    rows = []
    for path in sorted(set(p.resolve() for p in paths)):
        manifest_path = path.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        uf2 = {digest for name, digest in manifest.get("sha256", {}).items()
               if Path(name).name == "iqbench.uf2" and isinstance(digest, str) and
               re.fullmatch(r"[0-9a-fA-F]{64}", digest)}
        for line, text in enumerate(path.read_text().splitlines(), 1):
            if not text.strip():
                continue
            record = json.loads(text)
            record["_run_file"] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
            record["_line"] = line
            if len(uf2) == 1:
                record["_uf2_sha256"] = next(iter(uf2)).lower()
            rows.append(record)
    return rows


def row_symbols(r: dict) -> int | None:
    """Require the firmware's row2 report; old row1 labels predate that field."""
    match = LABEL.match(str(r.get("label", "")))
    if not match:
        return None
    requested = int(match["row"] or 1)
    if r.get("requested_row_symbols", requested) != requested:
        return None
    if "row_symbols" not in r:
        return 1 if requested == 1 else None
    if r["row_symbols"] != requested:
        return None
    return requested


def configuration_ok(r: dict) -> bool:
    match = LABEL.match(str(r.get("label", "")))
    if not match or row_symbols(r) is None:
        return False
    fields = {key: int(match[key]) for key in ("clock", "source", "kernel", "cpw", "fault")}
    divider = int(match["divider"] or 0)
    schedule = int(match["schedule"] or 0)
    effective = divider or fields["cpw"] * 256
    label = str(r.get("label", ""))
    writers_match = re.search(r"(?:^|-)writers([24])(?:-|$)", label)
    priority_match = re.search(r"(?:^|-)prio(\d+)(?:-|$)", label)
    writers = int(writers_match[1]) if writers_match else 2
    priority = int(priority_match[1]) if priority_match else 0
    return (r.get("clk_hz") == fields["clock"] * 1000 and
            r.get("source") == fields["source"] and
            r.get("kernel") == ("c", "asm", "interp")[fields["kernel"]] and
            r.get("cpw") == fields["cpw"] and r.get("fault") == fields["fault"] and
            r.get("schedule", 0) == schedule and r.get("divider256", effective) == effective and
            r.get("requested_schedule", schedule) == schedule and
            r.get("requested_divider256", divider) == divider and
            r.get("writers", 2) == writers and r.get("requested_writers", writers) == writers and
            r.get("bus_priority_mask", 0) == priority and
            r.get("requested_bus_priority_mask", priority) == priority and
            abs(r.get("sym_rate", 0) - r["clk_hz"] * 256 / (8 * effective)) < 1)


def common_clean(r: dict) -> bool:
    return (r.get("classification") == "clean" and not r.get("error") and
            not r.get("faults") and not any(r.get(key, 0) for key in FAULT_FIELDS) and
            r.get("tables_ok") == 1 and r.get("sps") == 4 and r.get("L") == 12 and
            r.get("cap_aligned") is True and r.get("cap_mismatches") == 0 and
            not r.get("cap_error"))


def spi_clean(r: dict) -> bool:
    """Require the independent packet/BB checks and a bounded four-PLFRAME capture window."""
    from dual_spi import FRAME_SYMBOLS, packet_faults

    digest = r.get("_uf2_sha256", "")
    if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
        return False
    try:
        if packet_faults(r):
            return False
    except (TypeError, ValueError):
        return False
    cursor, offset, first = (r.get(key) for key in
                             ("cap_symbol_cursor", "cap_offset", "cap_reference_first_frame"))
    if not (isinstance(cursor, int) and cursor >= 0 and isinstance(offset, int) and
            first == max(0, cursor // FRAME_SYMBOLS - 1)):
        return False
    fractional = r.get("divider256", 256) == 384
    last_step = ((8191 * 34 + r.get("cap_phase", -1)) // 3 if fractional else 8191 * 17)
    if not (offset >= 12 * 8 and offset + last_step < 4 * FRAME_SYMBOLS * 8):
        return False
    backend, ts, total = (r.get(key, float("nan")) for key in
                           ("s2_busy", "ts_busy", "core1_work_busy"))
    return (r.get("pv_spi", {}).get("sck_hz") == r["clk_hz"] / 4 and
            active_work(r) is not None and
            all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
                for value in (backend, ts, total)) and abs(backend + ts - total) <= 0.00031)


def dual_clean(r: dict, source: int = 2) -> bool:
    """Full encoder + interpolation only, complete intact sparse snapshot, known config."""
    if not (r.get("cmd") == "dualstream" and common_clean(r) and configuration_ok(r) and
            r.get("source") == source and r.get("kernel") == "interp" and r.get("fault") == 0 and
            r.get("bch_kernel") == 2 and r.get("s2_runtime_frames", 0) > 0 and
            r.get("gen_syms", 0) > 0 and r.get("read_i", 0) > 0 and r.get("read_q", 0) > 0 and
            r.get("cap_words") == 4096 and r.get("cap_checked_bus_words") == 8192 and
            r.get("cap_stride") == 17 and r.get("cap_transport_ok") is True and
            not r.get("cap_reference_failed") and r.get("ms", 0) > 0 and
            r.get("elapsed_us", 0) >= r["ms"] * 1000):
        return False
    if r.get("divider256", r["cpw"] * 256) == 384:
        # The old synchronized-divider sampler aliased the physical GPIO feedback path.
        # Fractional proof needs the later SYSCLK sampler, one fixed phase and both axes.
        axes = r.get("cap_axis_counts", [])
        if not (r.get("cap_divider256") == 256 and r.get("cap_phase") in (0, 1, 2) and
                len(axes) == 2 and sum(axes) == 8192 and min(axes) >= 2048):
            return False
    values = [r.get(key, float("nan")) for key in ("gen_busy", "control_busy", "core0_work_busy")]
    if source == 3:
        axes = r.get("cap_axis_counts", [])
        encoding = r.get("s2_busy", float("nan"))
        if not (r.get("one_core") is True and r.get("s2_core") == 0 and
                r.get("cap_phase") in (0, 1, 2) and len(axes) == 2 and sum(axes) == 8192 and min(axes) >= 2048 and
                isinstance(encoding, (int, float)) and math.isfinite(encoding) and 0 <= encoding <= 1):
            return False
        expected = sum(values[:2]) + encoding
    elif source in (4, 5):
        bch = r.get("split_bch_busy", float("nan"))
        axes = r.get("cap_axis_counts", [])
        if not (r.get("writers") == 4 and row_symbols(r) == 2 and
                r.get("split_bb_stride_words") == (64 if source == 4 else 0) and
                r.get("split_bch_slice_words") == 128 and r.get("split_bch_frames", 0) > 0 and
                r.get("split_bch_slices", 0) > 0 and not r.get("one_core", True) and r.get("s2_core") == 1 and
                r.get("cap_phase") in (0, 1, 2) and len(axes) == 2 and sum(axes) == 8192 and min(axes) >= 2048 and
                isinstance(bch, (int, float)) and math.isfinite(bch) and 0 <= bch <= 1):
            return False
        if source == 5 and not spi_clean(r):
            return False
        expected = sum(values[:2]) + bch
    else:
        if r.get("one_core", False) or r.get("s2_core", 1) != 1:
            return False
        expected = sum(values[:2])
    return (all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
                for value in values) and abs(expected - values[2]) <= 0.00031)


def legacy_clean(r: dict, one_core: bool = False) -> bool:
    capture = ((r.get("cap_words"), r.get("cap_bus_words"), r.get("cap_skip", 8))
               in ((4088, 8176, 8), (4096, 8192, 0)))
    valid = (r.get("cmd") == "stream" and common_clean(r) and r.get("kernel") == "lut_asm_p" and
            r.get("s2_code") == "normal 2/3" and r.get("s2_index") == 5 and
            r.get("s2_runtime_frames", 0) > 0 and r.get("bch_kernel") == 2 and
            r.get("tx_mode") == ("one_core" if one_core else "two_core") and
            r.get("s2_core", 0 if one_core else 1) == (0 if one_core else 1) and r.get("cores") == "0" and
            capture and
            len(r.get("core", [])) == 1 and r["core"][0].get("id") == 0 and
            0 <= r["core"][0].get("busy", -1) <= 1)
    if one_core and valid:
        total = r["core"][0]["busy"] + r.get("s2_busy", float("nan"))
        return (math.isfinite(total) and 0 <= total <= 1 and
                abs(total - r.get("one_core_busy", float("nan"))) <= 0.00021)
    return valid


def order(r: dict) -> tuple:
    return (r.get("ms", 0), str(r.get("recorded_at", r.get("time", ""))),
            r.get("_run_file", ""), r.get("_line", 0))


def configuration_key(r: dict) -> tuple:
    return (r.get("source"), row_symbols(r), r["clk_hz"], r.get("divider256", r["cpw"] * 256),
            r.get("schedule", 0), r.get("poll_interval_us"),
            r.get("bus_priority_mask", 0), r.get("writers", 2),
            r.get("split_bb_stride_words", 0), r.get("split_bch_slice_words", 0))


def longest_configurations(rows: list[dict]) -> list[dict]:
    configurations = {}
    for r in rows:
        key = configuration_key(r)
        if key not in configurations or order(r) > order(configurations[key]):
            configurations[key] = r
    return list(configurations.values())


def select_one_core(rows: list[dict]) -> list[dict]:
    direct = longest_configurations([r for r in rows if dual_clean(r, 3) and r.get("writers", 2) == 2])
    legacy = [r for r in rows if legacy_clean(r, True)]
    cases = []
    for clock in sorted({r["clk_hz"] for r in direct}):
        new = max((r for r in direct if r["clk_hz"] == clock), key=lambda r: (r["sym_rate"], order(r)))
        old = [r for r in legacy if r["clk_hz"] == clock]
        digest = new.get("_uf2_sha256")
        same_binary = [r for r in old if digest and r.get("_uf2_sha256") == digest]
        old = same_binary or old
        if old:
            best_old = max(old, key=lambda r: (r["sym_rate"], order(r)))
            cases.append(dict(clk_hz=clock, legacy=best_old, direct=new,
                              identical_binary=bool(digest) and digest == best_old.get("_uf2_sha256")))
    return cases


def select(rows: list[dict]) -> dict:
    dual = [r for r in rows if dual_clean(r)]
    load_candidates = [r for r in dual if r["clk_hz"] == 128_000_000 and
                       abs(r["sym_rate"] - 8e6) < 1 and r.get("schedule", 0) == 0 and
                       r.get("writers", 2) == 2]
    # Prefer row1/row2 from the same flashed-artifact snapshot. First try identical bus
    # priority and polling settings; relax those before falling back to different binaries.
    for settings in (("bus_priority_mask", "poll_interval_us"), ("bus_priority_mask",), ()):
        groups = {}
        for r in load_candidates:
            digest = r.get("_uf2_sha256")
            if digest:
                key = (digest,) + tuple(r.get(field, 0) for field in settings)
                groups.setdefault(key, []).append(r)
        complete = [group for group in groups.values() if {row_symbols(r) for r in group} == {1, 2}]
        if complete:
            def pair_order(group):
                pair = [max((r for r in group if row_symbols(r) == n), key=order) for n in (1, 2)]
                return (min(r.get("ms", 0) for r in pair), max(order(r) for r in pair))
            load_candidates = max(complete, key=pair_order)
            break
    load_rows = []
    for symbols in (1, 2):
        candidates = [r for r in load_candidates if row_symbols(r) == symbols]
        if candidates:
            load_rows.append(max(candidates, key=order))
    baselines = [r for r in rows if legacy_clean(r) and r["clk_hz"] == 128_000_000 and
                 abs(r.get("sym_rate", 0) - 8e6) < 1]
    # A baseline in another results directory can still be from the identical UF2.
    paired = next((r for r in load_rows if row_symbols(r) == 2), None)
    if paired:
        digest = paired.get("_uf2_sha256")
        same_binary = [r for r in baselines if digest and r.get("_uf2_sha256") == digest]
        same_run = [r for r in baselines if r.get("_run_file") == paired.get("_run_file")]
        baselines = same_binary or same_run or baselines
    baseline = max(baselines, key=order) if baselines else None

    # First retain the longest clean test of each physical configuration. Then display the
    # fastest observed configuration per architecture/clock, with its actual duration printed.
    rates = {}
    split = [r for r in rows if dual_clean(r, 4) or dual_clean(r, 5)]
    for r in longest_configurations(dual + split):
        key = (r.get("writers", 2), r.get("source"), row_symbols(r), r["clk_hz"])
        if key not in rates or (r["sym_rate"], order(r)) > (rates[key]["sym_rate"], order(rates[key])):
            rates[key] = r
    hashes = {r.get("_uf2_sha256") for r in load_rows + ([baseline] if baseline else [])}
    return dict(load_baseline=baseline, load_direct=load_rows,
                load_identical_binary=bool(hashes) and None not in hashes and len(hashes) == 1,
                sustained=[rates[key] for key in sorted(rates)], one_core=select_one_core(rows))


def duration(r: dict) -> str:
    seconds = r.get("elapsed_us", r.get("ms", 0) * 1000) / 1e6
    return f"{seconds:.3g} s"


def active_work(r: dict) -> float | None:
    """Whole active-loop timing is separate from the sum of instrumented regions."""
    value = r.get("core0_active_busy")
    total = r.get("core0_work_busy", 0)
    if (isinstance(value, (int, float)) and math.isfinite(value) and
            total - 0.0005 <= value <= 1):
        return value
    return None


def percent_axis(ax):
    ax.set_xlim(0, 109)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("Instrumented work (%)")
    ax.grid(axis="x")
    ax.set_axisbelow(True)


def plot_load(selected: dict, out: Path) -> bool:
    from plotstyle import BLUE, GREEN, INK, RED, WIDTH, lollipop, plt, save_figure

    baseline, direct = selected["load_baseline"], selected["load_direct"]
    if not baseline or not direct:
        return False
    rows = [baseline, *direct]
    labels = ["Sample buffer"] + [f"Pointers, {row_symbols(r)} symbol{'s' if row_symbols(r) == 2 else ''}/row"
                                    for r in direct]
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.65), sharey=True,
                             gridspec_kw={"width_ratios": [1.35, 1]})
    for y, r in enumerate(rows):
        generation = r["core"][0]["busy"] if y == 0 else r["gen_busy"]
        control = 0 if y == 0 else r["control_busy"]
        axes[0].barh(y, generation * 100, height=0.42, color=BLUE, edgecolor=INK,
                     linewidth=0.4, label="Generation" if y == 0 else None)
        axes[0].barh(y, control * 100, left=generation * 100, height=0.42,
                     color="white", hatch="////", edgecolor=GREEN, linewidth=0.6,
                     label="Supervision" if y == 0 else None)
        axes[0].annotate(f"{(generation + control) * 100:.1f}",
                         ((generation + control) * 100, y), xytext=(4, 0),
                         textcoords="offset points", va="center", fontsize=8)
        encoding = r.get("s2_busy")
        if isinstance(encoding, (int, float)) and math.isfinite(encoding):
            lollipop(axes[1], y, encoding * 100, RED, fmt="{:.1f}")
    axes[0].set_yticks(range(len(rows)), labels)
    axes[0].set_ylim(len(rows) - 0.5, -0.5)
    axes[0].set_title("(a) Output preparation, core 0")
    axes[1].set_title("(b) FEC, core 1")
    for ax in axes:
        percent_axis(ax)
    axes[0].legend(loc="lower right", fontsize=7, handlelength=1.3)
    same_binary = "Same UF2." if selected["load_identical_binary"] else "Firmware snapshots differ."
    times = ", ".join(duration(r) for r in rows)
    save_figure(fig, out / "dual_dma_load.png",
                "8 Msym/s at 128 MHz; QPSK 2/3 + pilots, L = 12, 4 samples/symbol. " + same_binary + " Tests: " + times + ".\n"
                "Sample-buffer housekeeping is untimed; direct DMA includes CPU supervision. Unmeasured time is not an idle measurement.")
    return True


def rate_architecture(r: dict) -> tuple:
    return (r.get("writers", 2), r.get("source"), row_symbols(r),
            r.get("pv_spi", {}).get("sck_hz", 0))


def architecture_label(key: tuple) -> str:
    writers, source, symbols, sck = key
    if source == 5:
        return f"On-chip SPI, {sck / 1e6:g} MHz"
    if source == 4:
        return f"Split FEC, {writers} writers"
    return f"{symbols} symbol{'s' if symbols == 2 else ''}/row, {writers} writers"


def plot_rates(selected: dict, out: Path) -> bool:
    from matplotlib.lines import Line2D
    from plotstyle import BLUE, DARK, MID, RED, WIDTH, plt, save_figure

    rows = selected["sustained"]
    if not rows:
        return False
    groups = sorted({rate_architecture(r) for r in rows})
    clocks = sorted({r["clk_hz"] for r in rows})
    colors, markers = (BLUE, RED, DARK), ("o", "s", "^")
    fig, ax = plt.subplots(figsize=(WIDTH, max(2.55, 0.5 * len(groups) + 1.05)))
    for y, key in enumerate(groups):
        group = sorted((r for r in rows if rate_architecture(r) == key), key=lambda r: r["clk_hz"])
        rates = [r["sym_rate"] / 1e6 for r in group]
        if len(group) > 1:
            ax.hlines(y, min(rates), max(rates), color=MID, linewidth=0.6)
        for r, rate in zip(group, rates):
            ci = clocks.index(r["clk_hz"])
            outside = rate * 4 > 65
            color, marker = (RED, "x") if outside else (colors[ci % 3], markers[ci % 3])
            ax.plot(rate, y, marker=marker, color=color, ms=4)
            above = ci % 2 == 0
            ax.annotate(f"{rate:.3f} · {duration(r)}", (rate, y), xytext=(0, 7 if above else -8),
                        textcoords="offset points", ha="center", va="bottom" if above else "top",
                        fontsize=7.2, color=color)
    maximum = max(r["sym_rate"] / 1e6 for r in rows)
    ax.set_xlim(0, max(12, maximum + 2.1))
    ax.set_ylim(len(groups) - 0.6, -0.65)
    ax.set_yticks(range(len(groups)), [architecture_label(key) for key in groups])
    ax.set_xlabel("Measured symbol rate (Msym/s)")
    ax.grid(axis="x")
    secondary = ax.secondary_xaxis("top", functions=(lambda x: x * 4, lambda x: x / 4))
    secondary.set_xlabel("Per-axis sample rate (MS/s)")
    secondary.tick_params(labelsize=7)
    handles = [Line2D([], [], color=colors[i % 3], marker=markers[i % 3], linestyle="none",
                      markersize=4, label=f"{clock / 1e6:g} MHz") for i, clock in enumerate(clocks)]
    outside = maximum * 4 > 65
    if outside:
        handles.append(Line2D([], [], color=RED, marker="x", linestyle="none", label=">65 MS/s"))
        ax.axvline(65 / 4, color=MID, ls="--", lw=0.7)
    ax.legend(handles=handles, loc="upper left", fontsize=7.5, handlelength=1)
    save_figure(fig, out / "dual_dma_rates.png",
                "Real QPSK 2/3 encoding, pilots, L = 12, 4 samples/symbol. On-chip SPI uses an emulated master.\n"
                "Fastest clean configuration; longest test at that setting. Sparse exact captures and continuous fault counters; digital bench only.")
    return True


def plot_one_core(selected: dict, out: Path) -> bool:
    from plotstyle import BLUE, GREEN, INK, RED, WIDTH, plt, save_figure

    cases = selected["one_core"]
    if not cases:
        return False
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, max(2.8, 0.72 * len(cases) + 1.3)), sharey=True,
                             gridspec_kw={"width_ratios": [1, 1.25]})
    labels = []
    for case_index, case in enumerate(cases):
        for variant_index, key in enumerate(("legacy", "direct")):
            y = case_index * 2 + variant_index
            r = case[key]
            direct = key == "direct"
            generation = r["gen_busy"] if direct else r["core"][0]["busy"]
            control = r["control_busy"] if direct else 0
            encoding = r["s2_busy"]
            total = generation + encoding + control
            rate = r["sym_rate"] / 1e6
            color = GREEN if direct else BLUE
            name = "Pointers" if direct else "Samples"
            labels.append(f"{case['clk_hz'] / 1e6:g} MHz · {name}")
            axes[0].hlines(y, 0, rate, color=color, linewidth=0.7)
            axes[0].plot(rate, y, marker="s" if direct else "o", color=color, ms=3.5)
            axes[0].annotate(f"{rate:.3f} · {duration(r)}", (rate, y), xytext=(5, 0),
                             textcoords="offset points", va="center", fontsize=7.2)
            left = 0
            for amount, part_color, hatch, label in ((generation, BLUE, None, "Generation"),
                                                       (encoding, RED, None, "FEC"),
                                                       (control, GREEN, "////", "Supervision")):
                axes[1].barh(y, amount * 100, left=left * 100, height=0.43,
                             color="white" if hatch else part_color, edgecolor=part_color,
                             hatch=hatch, linewidth=0.5, label=label if y == 0 else None)
                left += amount
            axes[1].annotate(f"{total * 100:.1f}", (total * 100, y), xytext=(4, 0),
                             textcoords="offset points", va="center", fontsize=7.5)
        if case_index + 1 < len(cases):
            for ax in axes:
                ax.axhline(case_index * 2 + 1.5, color="#dddddd", linewidth=0.4)
    axes[0].set_yticks(range(len(labels)), labels)
    axes[0].set_ylim(len(labels) - 0.5, -0.5)
    maximum = max(case[key]["sym_rate"] for case in cases for key in ("legacy", "direct")) / 1e6
    axes[0].set_xlim(0, maximum * 1.48)
    axes[0].set_xlabel("Measured rate (Msym/s)")
    axes[0].set_title("(a) One-core rate")
    axes[1].set_title("(b) Work on the same core")
    percent_axis(axes[1])
    handles, legend_labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, legend_labels, loc="upper center", bbox_to_anchor=(0.65, 1.02),
               fontsize=7, ncol=3, handlelength=1.2, columnspacing=0.9)
    axes[0].grid(axis="x")
    save_figure(fig, out / "dual_dma_onecore.png",
                "Real QPSK 2/3 encoding + pilots and L12 / 4-sample shaping; each pair uses the same clock, at different rates.\n"
                "Fastest clean configuration, longest test at that setting. Sample-buffer housekeeping is untimed; blank space is not measured idle.")
    return True


def plot_pipeline(selected: dict, out: Path) -> bool:
    """Expose both core budgets for the fastest qualified split-FEC records."""
    from matplotlib.lines import Line2D
    from plotstyle import BLUE, DARK, GREEN, INK, RED, WIDTH, lollipop, plt, save_figure

    rows = [r for r in selected["sustained"] if r.get("source") in (4, 5)]
    if not rows:
        return False
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, max(2.1, len(rows) * 0.48 + 1.4)), sharey=True,
                             gridspec_kw={"width_ratios": [1.5, 1]})
    labels, has_active = [], False
    for y, r in enumerate(rows):
        label = (f"On-chip SPI, {r['pv_spi']['sck_hz'] / 1e6:g} MHz" if r["source"] == 5
                 else "Generated BBFRAMEs")
        labels.append(f"{label}\n{r['sym_rate'] / 1e6:g} Msym/s · {r['clk_hz'] / 1e6:g} MHz")
        left = 0
        parts = ((r["gen_busy"], BLUE, None, "Pointers"),
                 (r["split_bch_busy"], RED, None, "FEC"),
                 (r["control_busy"], GREEN, "////", "Supervision"))
        for amount, color, hatch, label in parts:
            axes[0].barh(y, amount * 100, left=left * 100, height=0.26,
                         color="white" if hatch else color, edgecolor=color, hatch=hatch,
                         linewidth=0.5, label=label if y == 0 else None)
            left += amount
        axes[0].annotate(f"{left * 100:.1f}", (left * 100, y), xytext=(4, 7),
                         textcoords="offset points", fontsize=7.5, va="bottom")
        active = active_work(r)
        if active is not None:
            has_active = True
            axes[0].plot(active * 100, y, marker="D", color=INK, ms=4,
                         markerfacecolor="white", zorder=4)
            axes[0].annotate(f"{active * 100:.1f} active", (active * 100, y), xytext=(0, -12),
                             textcoords="offset points", ha="center", va="top", fontsize=7)
        if r["source"] == 5:
            axes[1].barh(y, r["s2_busy"] * 100, height=0.26, color=RED, linewidth=0.5)
            axes[1].barh(y, r["ts_busy"] * 100, left=r["s2_busy"] * 100, height=0.26,
                         color=DARK, linewidth=0.5, label="TS assembly")
            total = r["core1_work_busy"] * 100
            axes[1].annotate(f"{total:.1f}", (total, y), xytext=(0, 7),
                             textcoords="offset points", ha="right", va="bottom", fontsize=7.5)
        else:
            lollipop(axes[1], y, r["s2_busy"] * 100, RED, fmt="{:.1f}")
        axes[1].annotate(duration(r), (0, y), xytext=(4, 7), textcoords="offset points", fontsize=7)
    axes[0].set_yticks(range(len(rows)), labels)
    axes[0].set_ylim(len(rows) - 0.55, -0.6)
    axes[0].set_title("(a) Core 0: BB/BCH + output")
    spi_title = " + TS" if any(r["source"] == 5 for r in rows) else ""
    axes[1].set_title("(b) Core 1: LDPC + framing" + spi_title)
    for ax in axes:
        percent_axis(ax)
    axes[1].set_xlim(0, 112)
    handles, legend_labels = axes[0].get_legend_handles_labels()
    if has_active:
        handles.append(Line2D([], [], color=INK, marker="D", markerfacecolor="white", linestyle="none"))
        legend_labels.append("Active loop")
    spi_handles, spi_labels = axes[1].get_legend_handles_labels()
    if spi_handles:
        handles.append(spi_handles[0])
        legend_labels.append(spi_labels[0])
    fig.legend(handles, legend_labels, loc="upper center", bbox_to_anchor=(0.57, 1.02),
               ncol=len(handles), fontsize=6.9, handlelength=1.2, columnspacing=0.8)
    save_figure(fig, out / "dual_dma_pipeline.png",
                "Split FEC, four writers, paired rows; QPSK 2/3 + pilots, L12 at 4 samples/symbol. SPI uses an on-chip master.\n"
                "Bars sum timed regions; diamonds include the whole active loop when reported. Excluded time is not an idle measurement.")
    return True


def self_test():
    good = dict(label="dual-row2-128000-src2-k2-cpw2-fault0", cmd="dualstream", classification="clean",
                clk_hz=128_000_000, sps=4, L=12, kernel="interp", source=2, cpw=2, divider256=512,
                schedule=0, row_symbols=2, fault=0, tables_ok=1, bch_kernel=2, s2_runtime_frames=724,
                ms=3000, elapsed_us=3_000_020, gen_syms=23_999_488, read_i=24_000_200, read_q=24_000_200,
                sym_rate=8e6, gen_busy=0.25, control_busy=0.2, core0_work_busy=0.45, cap_words=4096,
                cap_checked_bus_words=8192, cap_stride=17, cap_aligned=True, cap_mismatches=0,
                cap_transport_ok=True, cap_reference_failed=False, recorded_at="2026-10-15T00:00:00")
    original = copy.deepcopy(good)
    assert dual_clean(good)
    for changes in ({"fault": 1}, {"source": 0}, {"cap_checked_bus_words": 8190},
                    {"cap_transport_ok": False}, {"cap_mismatches": 1}, {"errors": 1},
                    {"row_symbols": 1}, {"schedule": 1}, {"divider256": 384},
                    {"clk_hz": 150_000_000}, {"gen_busy": float("nan")},
                    {"elapsed_us": 1000}, {"kernel": "asm"}, {"s2_runtime_frames": 0}):
        assert not dual_clean(dict(good, **changes)), changes
    missing_row2 = dict(good); del missing_row2["row_symbols"]
    assert not dual_clean(missing_row2)
    historical = dict(good, label="dual-128000-src2-k2-cpw2-fault0")
    del historical["row_symbols"]; del historical["divider256"]; del historical["schedule"]
    assert row_symbols(historical) == 1 and dual_clean(historical)
    longer = dict(good, ms=60000, elapsed_us=60_000_030)
    assert select([good, longer])["sustained"] == [longer]
    newer_short = dict(good, recorded_at="2026-10-16T00:00:00")
    assert select([longer, newer_short])["sustained"] == [longer]
    failed_ceiling = dict(good, label="dual-row2-128000-src2-k2-cpw1-fault0-ceiling-probe",
                          cpw=1, divider256=256, sym_rate=16e6, cap_aligned=False, classification="failed")
    assert select([longer, failed_ceiling])["sustained"] == [longer]
    assert configuration_key(good) != configuration_key(dict(good, bus_priority_mask=1))
    four = dict(longer, label=longer["label"] + "-writers4", writers=4)
    assert dual_clean(four) and select([good, four])["load_direct"] == [good]
    assert len(select([good, four])["sustained"]) == 2
    common = dict(good, _uf2_sha256="a" * 64)
    row1 = dict(historical, _uf2_sha256="a" * 64)
    unrelated = dict(longer, _uf2_sha256="b" * 64)
    assert select([row1, common, unrelated])["load_direct"] == [row1, common]
    fractional = dict(good, label="dual-row2-128000-src2-k2-cpw1-fault0-sched0-div384-ceiling-probe",
                      cpw=1, divider256=384, sym_rate=128e6 / 12,
                      cap_divider256=256, cap_phase=2, cap_axis_counts=[2730, 5462])
    assert dual_clean(fractional)
    assert not dual_clean(dict(fractional, cap_divider256=384))
    assert not dual_clean(dict(fractional, cap_axis_counts=[0, 8192]))
    one = dict(good, label="dual-row2-128000-src3-k2-cpw2-fault0", source=3,
               one_core=True, s2_core=0, s2_busy=0.4, core0_work_busy=0.85,
               cap_phase=0, cap_axis_counts=[4096, 4096])
    assert dual_clean(one, 3) and not dual_clean(one)
    assert not dual_clean(dict(one, one_core=False), 3)
    assert not dual_clean(dict(one, core0_work_busy=0.45), 3)
    old_one = dict(good, cmd="stream", label="legacy-onecore-128000", kernel="lut_asm_p",
                   tx_mode="one_core", s2_core=0, s2_code="normal 2/3", s2_index=5, cores="0",
                   core=[dict(id=0, busy=0.45)], s2_busy=0.4, one_core_busy=0.85,
                   cap_words=4088, cap_bus_words=8176, sym_rate=128e6 / 24)
    assert legacy_clean(old_one, True)
    assert legacy_clean(dict(old_one, cap_words=4096, cap_bus_words=8192, cap_skip=0), True)
    assert not legacy_clean(dict(old_one, cap_skip=0), True)
    assert not legacy_clean(dict(old_one, cap_words=4096, cap_bus_words=8192), True)
    assert select([one, old_one])["one_core"][0]["direct"] == one
    assert select([one, old_one])["sustained"] == []
    split = dict(good, label="dual-row2-128000-src4-k2-cpw1-fault0-writers4-ceiling-probe",
                 source=4, cpw=1, divider256=256, sym_rate=16e6, writers=4,
                 one_core=False, s2_core=1, split_bb_stride_words=64, split_bch_slice_words=128,
                 split_bch_frames=1442, split_bch_slices=17312, split_bch_busy=0.3956,
                 gen_busy=0.4551, control_busy=0.1250, core0_work_busy=0.9757,
                 cap_phase=0, cap_axis_counts=[4096, 4096])
    assert dual_clean(split, 4) and not dual_clean(split)
    assert not dual_clean(dict(split, split_bb_stride_words=0), 4)
    assert not dual_clean(dict(split, split_bch_slice_words=0), 4)
    assert not dual_clean(dict(split, core0_work_busy=0.5801), 4)
    selection = select([split, good])
    assert selection["load_direct"] == [good] and split in selection["sustained"]
    assert selection["one_core"] == []
    spi_pending = dict(split, label=split["label"].replace("src4", "src5"), source=5)
    assert not dual_clean(spi_pending, 5) and select([spi_pending])["sustained"] == []
    from dual_spi import first_crcs, pv
    import zlib
    fields = b"".join(pv.message(k, pv.pattern_payload(k))[-4:] for k in range(16))
    spi = dict(spi_pending, _uf2_sha256="a" * 64, split_bb_stride_words=0,
               gen_busy=0.3, control_busy=0.1, split_bch_busy=0.3,
               core0_work_busy=0.7, core0_active_busy=0.72,
               s2_busy=0.6, ts_busy=0.15, core1_work_busy=0.75,
               cap_symbol_cursor=100000, cap_reference_first_frame=2, cap_offset=300000,
               pv_spi=dict(sck_hz=32_000_000, ok=48, ts_packets=336, runtime_bbframes=6,
                           runtime_messages=24, crc_chain=zlib.crc32(fields * 3), bb_crc=first_crcs(),
                           lost=0, bad_hdr=0, bad_crc=0, bad_sync=0, null_packets=0,
                           short_msgs=0, long_msgs=0, overflows=0))
    assert dual_clean(spi, 5) and select([spi])["sustained"] == [spi]
    for changes in ({"_uf2_sha256": ""}, {"core1_work_busy": 0.6},
                    {"core0_active_busy": 0.5}, {"cap_reference_first_frame": 0},
                    {"cap_offset": 4 * 33282 * 8 - 1},
                    {"pv_spi": dict(spi["pv_spi"], null_packets=1)},
                    {"pv_spi": dict(spi["pv_spi"], crc_chain=0)},
                    {"pv_spi": dict(spi["pv_spi"], bb_crc=[0] * 4)}):
        assert not dual_clean(dict(spi, **changes), 5), changes
    assert active_work(dict(split, core0_active_busy=0.99)) == 0.99
    assert active_work(dict(split, core0_active_busy=0.8)) is None
    assert active_work(dict(split, core0_active_busy=float("nan"))) is None
    assert good == original
    print("plot_dual self-test: configuration, capture, negative-control and longest-run selection checked")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--runs", type=Path, nargs="+", help="explicit immutable runs.jsonl files")
    inputs.add_argument("--root", type=Path, help=f"recursively find runs.jsonl (default: {DEFAULT_ROOT.relative_to(ROOT)})")
    parser.add_argument("--out", type=Path, default=ROOT / "results/plots", help="output directory")
    parser.add_argument("--check", action="store_true", help="print selected records without writing figures")
    parser.add_argument("--self-test", action="store_true", help="run synthetic selection checks, no figures")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    paths = args.runs if args.runs is not None else list((args.root or DEFAULT_ROOT).rglob("runs.jsonl"))
    if not paths:
        parser.error("no runs.jsonl files found")
    selected = select(load(paths))
    if args.check:
        print(json.dumps(selected, indent=2))
        return
    count = (int(plot_load(selected, args.out)) + int(plot_rates(selected, args.out)) +
             int(plot_one_core(selected, args.out)) + int(plot_pipeline(selected, args.out)))
    if not count:
        parser.error("no eligible clean full-DVB records; no figures written")
    args.out.mkdir(parents=True, exist_ok=True)
    # Matplotlib's path serialization leaves trailing spaces in SVG coordinate lines.
    # Normalize whitespace so regenerated vector artifacts pass the repository diff check.
    for svg in args.out.glob("dual_dma_*.svg"):
        svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
    portable = copy.deepcopy(selected)
    def portable_paths(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "_run_file":
                    try:
                        value[key] = str(Path(item).relative_to(ROOT))
                    except ValueError:
                        pass
                else:
                    portable_paths(item)
        elif isinstance(value, list):
            for item in value:
                portable_paths(item)
    portable_paths(portable)
    (args.out / "dual_dma_selection.json").write_text(json.dumps(portable, indent=2) + "\n")
    print(f"{len(paths)} result files -> {count} figures (PNG/SVG/PDF) in {args.out}")


if __name__ == "__main__":
    main()
