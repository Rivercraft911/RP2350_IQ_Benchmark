#!/usr/bin/env python3
"""Emit markdown tables for frontier.md from frontier.py's model (no hand-copied numbers)."""
import math
import pathlib

import frontier as F

HERE = pathlib.Path(__file__).resolve().parent
OUT = []


def p(line=""):
    OUT.append(line)


def f(x, n=2):
    if x is None:
        return "-"
    if isinstance(x, float):
        if math.isnan(x):
            return "-"
        if math.isinf(x):
            return "inf"
        return f"{x:.{n}f}"
    return str(x)


BIND = {"e_afe": "AFE", "ab_cpu": "CPU", "c_dma_handoff": "DMA hand-off", "c_dma_bw": "DMA bw", "f_spi": "ingress",
        "d_sram": "SRAM", "no engine": "no engine"}

# validation
p("### T-val. CPU model vs measured runs (work fraction of each core)")
p()
p("| Run | Core 0 pred / meas | Core 1 pred / meas |")
p("|---|---:|---:|")
for v in F.validate():
    m0 = "-" if math.isnan(v["meas_c0"]) else f"{100 * v['meas_c0']:.1f}%"
    p(f"| {v['label']} | {100 * v['pred_c0']:.1f}% / {m0} | {100 * v['pred_c1']:.1f}% / {100 * v['meas_c1']:.1f}% |")
p()

# cycle budget
cb, avail = F.cycle_budget_view()
p("### T-cyc. Two-core cycles per on-air symbol at SYS-independent grid steps")
p()
p(f"Available (2 cores x u_demo {F.U_DEMO} x 2N d): N4 d=1 (16 Msym/s at SYS128) = {avail['N4 d1']:.1f}; "
  f"N4 d=2 = {avail['N4 d2']:.1f}; N2 d=2 (16 Msym/s) = {avail['N2 d2']:.1f}; N2 d=3 = {avail['N2 d3']:.1f}; "
  f"N2 d=4 = {avail['N2 d4']:.1f}.")
p()
p("| MODCOD | FEC variant | ingress+BCH+LDPC+map | of which map | SW shaping (+sup.) | total, SW shaping | total, HW shaping |")
p("|---|---|---:|---:|---:|---:|---:|")
for x in cb:
    if x["rate"] == "9/10" and x["fec"] == "meas":
        continue
    p(f"| {x['mod']} {x['rate']} | {x['fec']} | {x['fec_cyc']:.2f} | {x['map_cyc']:.2f} | "
      f"{x['total_sw'] - x['fec_cyc']:.2f} ({x['sw_shape_engine']}) | {x['total_sw']:.2f} | {f(x['total_hw'])} |")
p()

# frontier summary across scenarios
pts = F.scenarios()
idx = {(r["scenario"], r["mod"], r["rate"], r["N"]): r for r in pts}
scen = [s[0] for s in F.SCENARIOS]
p("### T-front. Frontier by scenario, SYS128, GCS input, u_demo (Rs Msym/s / BB Mb/s / binding)")
p()
p("| MODCOD | N | " + " | ".join(scen) + " |")
p("|---|---|" + "---|" * len(scen))
for mod, rates in F.MODCODS.items():
    for rate in rates:
        if rate not in ("2/3", "3/4", "5/6", "9/10"):
            continue
        for n in (4, 2):
            cells = []
            for s in scen:
                r = idx[(s, mod, rate, n)]
                if r["engine"] == "(none)":
                    cells.append("no engine")
                elif r["rs_grid"] == 0:
                    cells.append(f"SRAM ({r['table_kib']:.0f} KiB)")
                else:
                    star = "" if r["engine_status"].startswith("measured") else "*"
                    cells.append(f"{r['rs_grid']:.2f} / {r['bb_mbps']:.1f} / {BIND[r['binding']]}{star}")
            p(f"| {mod} {rate} | N{n} | " + " | ".join(cells) + " |")
p()
p("`*` = engine not measured (derived or hypothetical). Ingress-bound rows give capacity; deliverable data is the "
  f"ingress cap ({F.SPI_CAPS['onchip32'] / 1e6:.2f} Mb/s on-chip 32 MHz; {F.SPI_CAPS['ext25'] / 1e6:.2f} Mb/s external 25 MHz).")
p()

# per-constraint bounds for the most capable scenario
p("### T-bounds. Separate bounds (Msym/s) for the S4 point of each 5/6 and 9/10 MODCOD, SYS128")
p()
p("| MODCOD | N | engine / partition | (a) FEC | (b) shaping | (a+b) CPU | (c) DMA bw / hand-off | (d) table / budget KiB | "
  "(e) AFE | (f) ingress 32 / 25 MHz | Rs grid | BB Mb/s | max core load | binding | flags |")
p("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
for mod, rates in F.MODCODS.items():
    for rate in ("5/6", "9/10", "2/3"):
        if rate not in rates or (rate == "2/3" and mod not in ("8PSK",)):
            continue
        for n in (4, 2):
            r = idx[("S4 beyond", mod, rate, n)]
            if r["engine"] == "(none)":
                continue
            ml = max(r["load_c0"], r["load_c1"])
            p(f"| {mod} {rate} | N{n} | {r['engine']} / {r['partition']} | {f(r['rs_a_fec'], 1)} | {f(r['rs_b_shape'], 1)} | "
              f"{f(r['rs_ab_cpu'], 1)} | {f(r['rs_c_dma_bw'], 1)} / {f(r['rs_c_dma_handoff'], 1)} | "
              f"{r['table_kib']:.0f} / {r['sram_budget_kib']:.0f} | {f(r['rs_e_afe'], 2)} | "
              f"{f(r['rs_f_spi'], 1)} / {f(r['rs_f_spi_ext25'], 1)} | {f(r['rs_grid'], 3)} | {f(r['bb_mbps'], 2)} | "
              f"{'-' if math.isnan(ml) else f'{100 * ml:.0f}%'} | {BIND[r['binding']]} | {r.get('flags', '')} |")
p()

# FEC-only caps
p("### T-fec. BB capacity allowed by ingress+FEC+map alone on two cores (frame-parallel, SYS128, u_demo)")
p()
p("| MODCOD | variant | cycles/symbol | cycles/BB bit | Rs cap | BB cap Mb/s |")
p("|---|---|---:|---:|---:|---:|")
for x in F.fec_caps():
    if x["rate"] in ("2/3", "5/6", "9/10"):
        p(f"| {x['mod']} {x['rate']} | {x['fec']} | {x['c_sym']:.2f} | {x['c_per_bb_bit']:.2f} | {x['rs_cap']:.2f} | "
          f"{x['bb_cap']:.1f} |")
p()

# hardware APSK
hw = F.hw_apsk_tables()
p("### T-lvl. Per-axis amplitude levels (data + unit-radius header/pilot + Gold quarter turns)")
p()
p("| Alphabet | levels |")
p("|---|---:|")
for k, v in hw["levels"].items():
    p(f"| {k} | {v} |")
p()
p("### T-tab. Full-history table size (one row per axis history, R=1, int16 samples), KiB")
p()
p("| Alphabet | N | L5 | L6 | L7 | L8 | L9 |")
p("|---|---|---:|---:|---:|---:|---:|")
for name in ("2-level (QPSK)", "4-level (QPSK/8PSK pi/8)", "8-level (16APSK)", "17-level (32APSK)"):
    for n in (2, 4):
        cells = []
        for span in (5, 6, 7, 8, 9):
            s = next(x for x in hw["full_history"] if x["alphabet"] == name and x["N"] == n and x["L"] == span
                     and x["R"] == 1)
            kb = s["pio_bytes"] / 1024
            dense = s["dense_bytes"] / 1024
            cells.append(f"{kb:,.0f}" if kb == dense else f"{dense:,.0f} ({kb:,.0f} PIO-keyed)")
        p(f"| {name} | N{n} | " + " | ".join(cells) + " |")
p()
p("### T-part. Split-span (partial) tables: memory vs. required summation")
p()
p("| Scheme | KiB | adds per output word | output words per symbol | source |")
p("|---|---:|---:|---:|---|")
for x in hw["partial"]:
    p(f"| {x['scheme']} | {x['bytes'] / 1024:.1f} | {x['adds_per_output_word']} | {x['words_per_symbol']} | {x['src']} |")
p()
p("### T-snif. DMA sniffer SUM as an adder: transfer-count bound only (ignores chain latency)")
p()
p("| terms S | N | transfers per summed 32-bit word | Rs bound at SYS128 (Msym/s) |")
p("|---:|---:|---:|---:|")
for x in hw["sniffer"]:
    p(f"| {x['S']} | {x['N']} | {x['transfers_per_output_word']} | {x['rs_bw_bound_msym']:.1f} |")
p()

# levers
lv, base = F.levers()
p("### T-lev. Lever results (vs measured QPSK 5/6 N4 16 Msym/s, 25.84 Mb/s)")
p()
p("| Group | Lever | BB Mb/s | change | deliverable 32 / 25 MHz SPI | Rs | binding | core loads | flags |")
p("|---|---|---:|---:|---:|---:|---|---|---|")
for x in lv:
    p(f"| {x['group']} | {x['lever']} | {x['bb_mbps']:.2f} | {x['gain_pct']:+.1f}% | {x['bb_onchip']:.2f} / {x['bb_ext25']:.2f} | "
      f"{x['rs']:.3f} | {BIND[x['binding']]} | {100 * x['load'][0]:.0f}% / {100 * x['load'][1]:.0f}% | {x['flags']} |")
p()
p("### T-map. APSK PL-mapper sensitivity (PL-job balance, 5/6, N2, SYS128)")
p()
p("| Mod | map cycles/symbol | continuous Rs | grid Rs | BB Mb/s |")
p("|---|---:|---:|---:|---:|")
for x in F.apsk_mapper_sensitivity():
    p(f"| {x['mod']} | {x['map_cyc_per_sym']:.1f} | {x['rs_limit']:.2f} | {x['rs']:.3f} | {x['bb']:.2f} |")
p()
p(f"SRAM shaping-table budgets (KiB): lab image {F.sram_table_budget('lab_image') / 1024:.1f}; "
  f"flight single-profile {F.sram_table_budget('flight') / 1024:.1f}; flight with frame-parallel FEC "
  f"{F.sram_table_budget('flight_fp') / 1024:.1f}.")
p()
p("### T-sck. PV-SPI SCK needed per lane (1332-byte messages, 10 us CS gap)")
p()
p("| Point | BB Mb/s | 1 lane | 2 lanes |")
p("|---|---:|---:|---:|")
for lab, mod, rate in (("QPSK 5/6 @16", "QPSK", "5/6"), ("QPSK 9/10 @16", "QPSK", "9/10"),
                       ("QPSK 9/10 @16.25", "QPSK", "9/10"), ("8PSK 2/3 @16", "8PSK", "2/3"),
                       ("8PSK 5/6 @16", "8PSK", "5/6"), ("8PSK 9/10 @16", "8PSK", "9/10")):
    rs = 16.25e6 if "16.25" in lab else 16e6
    bb = F.bb_rate(mod, rate, rs)
    p(f"| {lab} | {bb / 1e6:.2f} | {F.sck_needed(bb) / 1e6:.1f} MHz | {F.sck_needed(bb, lanes=2) / 1e6:.1f} MHz |")
(HERE / "frontier_tables.md").write_text("\n".join(OUT) + "\n")
print("\n".join(OUT))
