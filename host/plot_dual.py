"""Plot the isolated direct-DMA experiments without changing historical progress figures.

Examples:
  python3 host/plot_dual.py --root results/dual-dma/2026-10-14 --check
  python3 host/plot_dual.py --runs results/dual-dma/2026-10-14/soak-1/runs.jsonl

Writes dual_dma_load, dual_dma_rates and dual_dma_onecore as PNG/SVG/PDF in --out (results/plots by
default), plus the exact selected records in dual_dma_selection.json. --check only
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

DEFAULT_ROOT = ROOT / "results/dual-dma/2026-10-14"
FAULT_FIELDS = ("errors", "underruns", "own_errors", "txstalls_i", "txstalls_q",
                "txstalls", "link_overruns", "input_overruns")
LABEL = re.compile(r"^dual(?:-row(?P<row>[12]))?-(?P<clock>\d+)-src(?P<source>[01234])"
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
    elif source == 4:
        bch = r.get("split_bch_busy", float("nan"))
        axes = r.get("cap_axis_counts", [])
        if not (r.get("writers") == 4 and row_symbols(r) == 2 and r.get("split_bb_stride_words") == 64 and
                r.get("split_bch_slice_words") == 128 and r.get("split_bch_frames", 0) > 0 and
                r.get("split_bch_slices", 0) > 0 and not r.get("one_core", True) and r.get("s2_core") == 1 and
                r.get("cap_phase") in (0, 1, 2) and len(axes) == 2 and sum(axes) == 8192 and min(axes) >= 2048 and
                isinstance(bch, (int, float)) and math.isfinite(bch) and 0 <= bch <= 1):
            return False
        expected = sum(values[:2]) + bch
    else:
        if r.get("one_core", False) or r.get("s2_core", 1) != 1:
            return False
        expected = sum(values[:2])
    return (all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
                for value in values) and abs(expected - values[2]) <= 0.00031)


def legacy_clean(r: dict, one_core: bool = False) -> bool:
    valid = (r.get("cmd") == "stream" and common_clean(r) and r.get("kernel") == "lut_asm_p" and
            r.get("s2_code") == "normal 2/3" and r.get("s2_index") == 5 and
            r.get("s2_runtime_frames", 0) > 0 and r.get("bch_kernel") == 2 and
            r.get("tx_mode") == ("one_core" if one_core else "two_core") and
            r.get("s2_core", 0 if one_core else 1) == (0 if one_core else 1) and r.get("cores") == "0" and
            r.get("cap_words") == 4088 and r.get("cap_bus_words") == 8176 and
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
    split = [r for r in rows if dual_clean(r, 4)]
    for r in longest_configurations(dual + split):
        key = (r.get("writers", 2), r.get("source"), row_symbols(r), r["clk_hz"])
        if key not in rates or (r["sym_rate"], order(r)) > (rates[key]["sym_rate"], order(rates[key])):
            rates[key] = r
    hashes = {r.get("_uf2_sha256") for r in load_rows + ([baseline] if baseline else [])}
    return dict(load_baseline=baseline, load_direct=load_rows,
                load_identical_binary=bool(hashes) and None not in hashes and len(hashes) == 1,
                sustained=[rates[key] for key in sorted(rates)], one_core=select_one_core(rows))


def plot_load(selected: dict, out: Path) -> bool:
    from plotstyle import BLUE, GREEN, INK, WIDTH, plt, save_figure

    baseline, direct = selected["load_baseline"], selected["load_direct"]
    if not baseline or not direct:
        return False
    labels = ["Legacy generation only"] + [f"Direct DMA, {row_symbols(r)} symbol{'s' if row_symbols(r) == 2 else ''}/row" +
                                          (f" (priority {r['bus_priority_mask']})" if r.get("bus_priority_mask", 0) else "")
                                          for r in direct]
    gen = [baseline["core"][0]["busy"] * 100] + [r["gen_busy"] * 100 for r in direct]
    control = [0] + [r["control_busy"] * 100 for r in direct]
    fig, ax = plt.subplots(figsize=(WIDTH, 3.1))
    y = list(range(len(labels)))
    ax.barh(y, gen, height=0.48, color=BLUE, edgecolor=INK, linewidth=0.4, label="Generation")
    ax.barh(y, control, left=gen, height=0.48, color="white", hatch="////", edgecolor=GREEN,
            linewidth=0.6, label="Ownership / fault checks")
    for j, (generation, checks) in enumerate(zip(gen, control)):
        ax.text(generation + checks + 1.5, j, f"{generation + checks:.1f}%", va="center", fontsize=8)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("Instrumented core 0 work (%)")
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    ax.legend(loc="lower right")
    ax.set_title("DVB-S2 normal QPSK 2/3 with pilots: 8 Msym/s, 128 MHz, L = 12, 4 samples/symbol")
    binary_note = "Same UF2 snapshot for all bars." if selected["load_identical_binary"] else "Firmware snapshots differ between bars."
    save_figure(fig, out / "dual_dma_load.png",
                "Legacy housekeeping was not instrumented. Direct DMA includes generation and monitored control work.\n"
                "Idle waits and untimed overhead are excluded; IRQ time counts only inside timed regions. " + binary_note)
    return True


def plot_rates(selected: dict, out: Path) -> bool:
    from plotstyle import BLUE, GREEN, MID, RED, WIDTH, plt, save_figure

    rows = selected["sustained"]
    if not rows:
        return False
    fig, ax = plt.subplots(figsize=(WIDTH, max(3.0, 0.42 * len(rows) + 1.4)))
    labels = []
    outside = False
    show_writers = any(r.get("writers", 2) != 2 for r in rows)
    show_distribution = any(r.get("source") == 4 for r in rows)
    for y, r in enumerate(rows):
        symbols = row_symbols(r)
        rate = r["sym_rate"] / 1e6
        too_fast = r["sym_rate"] * 4 > 65e6
        outside |= too_fast
        color = RED if too_fast else BLUE if symbols == 1 else GREEN
        marker = "X" if too_fast else "D" if r.get("source") == 4 else "o" if symbols == 1 else "s"
        ax.hlines(y, 0, rate, color=color, lw=0.8)
        ax.plot(rate, y, marker=marker, color=color, ms=4)
        seconds = r["elapsed_us"] / 1e6
        suffix = "; >65 MS/s" if too_fast else ""
        fractional = r.get("divider256", r["cpw"] * 256) % 256 != 0
        suffix += "; fractional" if fractional else ""
        ax.annotate(f"{rate:.3f} ({seconds:.3g} s{suffix})", (rate, y), xytext=(6, 0),
                    textcoords="offset points", va="center", fontsize=8)
        writers = f"{r.get('writers', 2)} writers, " if show_writers else ""
        distribution = ("split FEC" if r.get("source") == 4 else "FEC on core 1") if show_distribution else ""
        labels.append(writers + distribution + ("\n" if distribution else "") +
                      f"{symbols} symbol{'s' if symbols == 2 else ''}/row, {r['clk_hz'] / 1e6:g} MHz")
    maximum = max(r["sym_rate"] / 1e6 for r in rows)
    ax.set_xlim(0, maximum * 1.55)
    if outside:
        ax.axvline(65 / 4, color=MID, ls="--", lw=0.7)
    ax.set_yticks(range(len(rows)), labels)
    ax.invert_yaxis()
    ax.set_xlabel("Fastest fully checked observed rate (Msym/s); duration in parentheses")
    ax.grid(axis="x")
    ax.set_title("Direct DMA with real DVB-S2 encoding: longest clean test per configuration")
    save_figure(fig, out / "dual_dma_rates.png",
                "L12, 4 samples/symbol; all 8192 observations checked. Split FEC: core 0 BCH, core 1 LDPC/framing.\n"
                "Digital bench only; finite captures do not check every sample or qualify AFE timing. Red X exceeds 65 MS/s.")
    return True


def plot_one_core(selected: dict, out: Path) -> bool:
    from plotstyle import BLUE, GREEN, INK, RED, WIDTH, plt, save_figure

    cases = selected["one_core"]
    if not cases:
        return False
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, max(3.4, 0.68 * len(cases) + 1.7)), sharey=True,
                             gridspec_kw={"width_ratios": [1, 1.3]})
    labels = []
    for case_index, case in enumerate(cases):
        for variant_index, key in enumerate(("legacy", "direct")):
            y = case_index * 2 + variant_index
            r = case[key]
            is_direct = key == "direct"
            generation = r["gen_busy"] if is_direct else r["core"][0]["busy"]
            control = r["control_busy"] if is_direct else 0
            encoding = r["s2_busy"]
            total = generation + encoding + control
            rate = r["sym_rate"] / 1e6
            seconds = r.get("elapsed_us", r["ms"] * 1000) / 1e6
            name = f"Direct DMA, {row_symbols(r)} symbols/row" if is_direct else "Legacy samples"
            labels.append(f"{case['clk_hz'] / 1e6:g} MHz: {name}")
            axes[0].barh(y, rate, height=0.52, color=GREEN if is_direct else BLUE, edgecolor=INK, linewidth=0.4)
            axes[0].text(rate + 0.15, y, f"{rate:.3f}\n({seconds:.3g} s)", va="center", fontsize=7.5)
            axes[1].barh(y, generation * 100, height=0.52, color=BLUE, edgecolor=INK, linewidth=0.4,
                         label="Generation" if y == 0 else None)
            axes[1].barh(y, encoding * 100, left=generation * 100, height=0.52, color=RED, edgecolor=INK,
                         linewidth=0.4, label="Encoding" if y == 0 else None)
            axes[1].barh(y, control * 100, left=(generation + encoding) * 100, height=0.52,
                         color="white", hatch="////", edgecolor=GREEN, linewidth=0.6,
                         label="Control" if y == 0 else None)
            axes[1].text(total * 100 + 1, y, f"{total * 100:.1f}%", va="center", fontsize=7.5)
    axes[0].set_yticks(range(len(labels)), labels)
    axes[0].invert_yaxis()
    axes[0].set_xlim(0, max(r[k]["sym_rate"] for r in cases for k in ("legacy", "direct")) / 1e6 * 1.4)
    axes[0].set_xlabel("Fastest checked rate (Msym/s)")
    axes[1].set_xlim(0, 105)
    axes[1].set_xticks([0, 25, 50, 75, 100])
    axes[1].set_xlabel("Timed core 0 work (%)")
    axes[1].legend(loc="upper center", bbox_to_anchor=(0.5, 1.18), ncol=3, handlelength=1.2,
                   columnspacing=0.8)
    for ax in axes:
        ax.grid(axis="x")
        ax.set_axisbelow(True)
    fig.suptitle("One core: repeated DVB-S2 test BBFRAME → L12 / 4-sample shaping → output", fontsize=10)
    save_figure(fig, out / "dual_dma_onecore.png",
                "Fastest clean configuration per architecture/clock; longest test of that configuration. Rates differ between bars.\n"
                "Legacy housekeeping is untimed. Direct work = generation + control + encoding; idle and untimed overhead excluded.")
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
             int(plot_one_core(selected, args.out)))
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
