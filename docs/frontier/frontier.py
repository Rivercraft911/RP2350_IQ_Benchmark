#!/usr/bin/env python3
"""RP2350B DVB-S2 transmitter feasibility frontier (digital baseband only).

For each MODCOD (normal FECFRAME, pilots on) and N in {2, 4} samples/symbol, compute
the maximum symbol rate and BB data-field capacity allowed separately by
  (a) FEC compute on two cores        (b) shaping compute
  (ab) joint CPU (FEC + shaping + supervision on the same two cores)
  (c) DMA transfers (bandwidth and per-row chain hand-off)
  (d) SRAM for shaping tables         (e) AFE7071 65 MSPS/axis and 20 MHz RF bandwidth
  (f) PV-SPI ingress
then quantise to the integer-divider grid Rs = SYS / (2 N d) and name the binding
constraint. Also: per-axis level counts and full-history/partial table sizes for
hardware-only APSK shaping, a validation of the CPU model against measured runs, and
lever estimates.

Every input is registered in INPUTS with value, unit, kind and source:
  spec      datasheet / standard value (page or line cited)
  measured  device measurement recorded in the repo (file + record cited)
  derived   arithmetic from cited inputs (formula in code)
  assumed   engineering assumption; stated, not verified
Paths are relative to the RP2350_IQ_Benchmark repo root unless they start with '../'
or local to this folder. Python 3 + NumPy only. Run: python3 frontier.py
Outputs (next to this file): frontier_inputs.json, frontier_points.csv,
frontier_summary.json; console tables.

This is a digital-throughput model. It does not predict RF spectrum, EVM, AFE
timing, or link closure.
"""
import csv
import json
import math
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
INPUTS = {}


def I(name, value, unit, kind, src, note=""):
    """Register an input. Returns value."""
    assert kind in ("spec", "measured", "derived", "assumed"), kind
    INPUTS[name] = dict(value=value, unit=unit, kind=kind, src=src, note=note)
    return value


# ---------------------------------------------------------------------------
# 1. Platform and AFE limits
# ---------------------------------------------------------------------------
SYS128 = I("SYS_adopted", 128e6, "Hz", "spec",
           "docs/rp2350-notes.md 'Design consequences' 1; ../notes/yapogee/docs/clocks.md line 12",
           "Yapogee adopted SYS128 with integer PIO dividers")
SYS150 = I("SYS_stock_max", 150e6, "Hz", "spec", "RP2350 datasheet §8.6.3 p576 via docs/rp2350-notes.md")
DMA_XFER_PER_CYCLE = I("dma_transfers_per_cycle", 1.0, "transfer/cycle", "spec",
                       "docs/sources/rp2350.txt line 82501, RP2350 datasheet §12.6 p1094: 'one read access and "
                       "one write access, up to 32 bits ... every clock cycle' (all 16 channels share it)")
SRAM_TOTAL = I("sram_total", 520 * 1024, "B", "spec", "docs/rp2350-notes.md 'Memory and bus' (2x256 KB + SRAM8/9 4 KB)")
F_DAC_MAX = I("afe_fdac_max", 65e6, "S/s per axis", "spec",
              "docs/sources/afe7071.txt line 285, SLOS789C p5 (fDAC 0-65 MSPS, interleaved)")
BW_RF_MAX = I("afe_rf_bw_max", 20e6, "Hz", "spec", "docs/sources/afe7071.txt line 23, SLOS789C p1 features")
ALPHA = I("rolloff", 0.20, "-", "spec", "docs/pv-spi-spec.md waveform; EN 302 307-1 Table A.1 alpha=0.20 column")
AFE_TUNE0 = I("afe_filter_tune0_typ", [(10e6, 1.0), (20e6, 18.0), (40e6, 42.0), (55e6, 58.0)], "(Hz, dB)", "spec",
              "docs/sources/afe7071.txt lines 342-345, SLOS789C p6 (typical, Filtertune=0)")

# ---------------------------------------------------------------------------
# 2. DVB-S2 constants (EN 302 307-1 V1.4.1, local text docs/sources/en_30230701v010401p.txt)
# ---------------------------------------------------------------------------
KBCH = I("Kbch_normal", {"1/2": 32208, "3/5": 38688, "2/3": 43040, "3/4": 48408, "4/5": 51648,
                         "5/6": 53840, "8/9": 57472, "9/10": 58192}, "bit", "spec",
         "EN 302 307-1 Table 5a (en_30230701v010401p.txt line 1271)")
BCH_T = I("bch_t_normal", {"1/2": 12, "3/5": 12, "2/3": 10, "3/4": 12, "4/5": 12, "5/6": 10, "8/9": 8, "9/10": 8},
          "-", "spec", "EN 302 307-1 Table 5a")
KLDPC = {r: KBCH[r] + 16 * BCH_T[r] for r in KBCH}          # Nbch = Kbch + 16 t (normal frames)
LDPC_ENTRIES = I("ldpc_addr_entries", {"3/5": 648, "2/3": 480, "3/4": 540, "4/5": 576, "5/6": 600, "8/9": 500,
                                       "9/10": 504}, "360-bit rotate-XOR ops/frame", "spec",
                 "firmware/src/dvbs2_codes.h addr_n* array lengths (generated from EN 302 307-1 annex B)")
ESN0 = I("esn0_qef_awgn", {
    ("QPSK", "1/2"): 1.00, ("QPSK", "3/5"): 2.23, ("QPSK", "2/3"): 3.10, ("QPSK", "3/4"): 4.03,
    ("QPSK", "4/5"): 4.68, ("QPSK", "5/6"): 5.18, ("QPSK", "8/9"): 6.20, ("QPSK", "9/10"): 6.42,
    ("8PSK", "3/5"): 5.50, ("8PSK", "2/3"): 6.62, ("8PSK", "3/4"): 7.91, ("8PSK", "5/6"): 9.35,
    ("8PSK", "8/9"): 10.69, ("8PSK", "9/10"): 10.98,
    ("16APSK", "2/3"): 8.97, ("16APSK", "3/4"): 10.21, ("16APSK", "4/5"): 11.03, ("16APSK", "5/6"): 11.61,
    ("16APSK", "8/9"): 12.89, ("16APSK", "9/10"): 13.13,
    ("32APSK", "3/4"): 12.73, ("32APSK", "4/5"): 13.64, ("32APSK", "5/6"): 14.28, ("32APSK", "8/9"): 15.69,
    ("32APSK", "9/10"): 16.05}, "dB", "spec",
    "EN 302 307-1 Table 13 (line 2117): ideal Es/N0 at QEF, AWGN, normal frames, no pilots; link context only")
GAMMA16 = I("gamma_16apsk", {"2/3": 3.15, "3/4": 2.85, "4/5": 2.75, "5/6": 2.70, "8/9": 2.60, "9/10": 2.57},
            "-", "spec", "EN 302 307-1 Table 9 (line 1670)")
GAMMA32 = I("gamma_32apsk", {"3/4": (2.84, 5.27), "4/5": (2.72, 4.87), "5/6": (2.64, 4.64), "8/9": (2.54, 4.33),
                             "9/10": (2.53, 4.30)}, "-", "spec", "EN 302 307-1 Table 10 (line 1734)")
BITS = {"QPSK": 2, "8PSK": 3, "16APSK": 4, "32APSK": 5}
MODCODS = {"QPSK": ["2/3", "3/4", "4/5", "5/6", "8/9", "9/10"],
           "8PSK": ["3/5", "2/3", "3/4", "5/6", "8/9", "9/10"],
           "16APSK": ["2/3", "3/4", "4/5", "5/6", "8/9", "9/10"],
           "32APSK": ["3/4", "4/5", "5/6", "8/9", "9/10"]}
BBHEADER = 80


def frame_symbols(mod, pilots=True):
    """PLFRAME symbols: 90 header + 64800/m data + 36 per pilot block every 16 slots (EN 302 307-1 §5.5)."""
    m = BITS[mod]
    data = 64800 // m
    slots = data // 90
    return 90 + data + (36 * ((slots - 1) // 16) if pilots else 0)


# repo cross-check of frame lengths (README/apsk-fec README/8psk-live records)
assert [frame_symbols(m) for m in BITS] == [33282, 22194, 16686, 13338]


def dfl(rate):
    return KBCH[rate] - BBHEADER          # BB data-field bits per frame (capacity convention of the repo)


def bb_rate(mod, rate, rs):
    return rs * dfl(rate) / frame_symbols(mod)


# ---------------------------------------------------------------------------
# 3. Measured FEC stage cycles (isolated, one core, 128 MHz; cycles/frame are clock-independent)
# ---------------------------------------------------------------------------
SRC_A = "results/optimizations/2026-10-15/runs.jsonl stage A (git 5e38990269), dvbs2 <code> 1 20, bchkernel 2, mapkernel 1"
A = I("fec_stage_A", {  # code: (f_bb_bch, cyc_bch, f_ldpc pack0, f_ldpc pack1, f_map QPSK packed)
    "2/3": (91907, 80794, 107834, 107834, 70603),
    "3/4": (139286, 127207, 144444, 114667, 70603),
    "5/6": (113948, 101103, 126800, 111314, 70603),
    "9/10": (166330, 152873, 111004, 100422, 70603)}, "cycles/frame", "measured", SRC_A)
SRC_B = ("results/higher-mapping/2026-10-15/fast-1/runs.jsonl bench8map pilots=1 (git b29d1a7ec2-dirty); "
         "image predates the LDPC packer, so ldpc is pack0")
B = I("fec_stage_B", {  # code: (bb_bch, ldpc pack0, 8PSK packed map)
    "3/5": (112433, 156330, 47948.2), "2/3": (92182, 107092, 47952.9), "3/4": (139717, 144044, 47948.4),
    "5/6": (114111, 126828, 47951.8), "8/9": (165280, 103122, 47951.0), "9/10": (166941, 110565, 47951.8)},
    "cycles/frame", "measured", SRC_B)
MAP_APSK = I("apsk_map_live", {
    "16APSK": 4852247294 / 16856, "32APSK": 5954447802 / 14057}, "cycles/frame", "measured",
    "results/optimizations/2026-10-15/apsk{16,32}-split-soak/runs.jsonl, 150 MHz: encoder_stage_cycles.map / "
    "encoder_runtime_frames (PL interleave+constellation+Gold+native packing on core 1, live)")
K_N = I("shaper_cost_per_N_doubling", 20.9 / 11.55, "x", "derived",
        "docs/benchmark-history.md: lut_asm N=4 11.55 -> N=8 20.9 cycles/symbol (same algorithm)",
        "used only to scale software shaping engines from N2 to N4")

# BCH cost per info bit and BB scrambling, from stage A
CPB_BCH160 = I("bch160_cycles_per_bit", A["5/6"][1] / KBCH["5/6"], "cycles/bit", "derived",
               SRC_A + ": cyc_bch(5/6)/Kbch; 2/3 gives " + f"{A['2/3'][1] / KBCH['2/3']:.3f}")
CPB_BCH_GENERIC = I("bch_generic_cycles_per_bit", np.mean([A["3/4"][1] / KBCH["3/4"], A["9/10"][1] / KBCH["9/10"]]),
                    "cycles/bit", "derived",
                    SRC_A + ": t=12 (3/4) and t=8 (9/10) use the 8-word baseline kernel "
                    "(firmware/src/dvbs2.c:224 uses bch160 only when bch_deg == 160)")
CPB_BBSCR = I("bb_scramble_cycles_per_bit",
              np.mean([(A[r][0] - A[r][1]) / KBCH[r] for r in A]), "cycles/bit", "derived",
              SRC_A + ": (f_bb_bch - cyc_bch)/Kbch averaged over 4 codes")
# Hypothetical specialised BCH kernel for t=12 (192-bit) and t=8 (128-bit) remainders: slicing-by-4 is
# load-bound with 4 table loads per remainder word per byte group (strategy-report §3 'Rewrite BCH': 20 loads per
# 32 bits for 160 bits). Scale the measured bch160 cost by remainder words: 6/5 and 4/5.
CPB_BCH_T = I("bchT_cycles_per_bit", {12: CPB_BCH160 * 6 / 5, 10: CPB_BCH160, 8: CPB_BCH160 * 4 / 5},
              "cycles/bit", "derived",
              "hypothesis: specialised kernel cost scales with remainder words (160 bits = 5 words measured)")


def bbbch_cycles(rate, variant):
    """Isolated BB scrambling + BCH cycles/frame."""
    if variant == "bchT" and BCH_T[rate] != 10:
        return KBCH[rate] * (CPB_BCH_T[BCH_T[rate]] + CPB_BBSCR)
    if rate in A:
        return A[rate][0]
    if rate in B:
        return B[rate][0]
    # 4/5: t=12 generic kernel, derived
    return KBCH[rate] * (CPB_BCH_GENERIC + CPB_BBSCR)


def ldpc_cycles(rate, pack):
    """Isolated LDPC (+ parity append) cycles/frame."""
    if rate in A:
        return A[rate][3] if pack else A[rate][2]
    if rate == "3/5":
        return B[rate][1]                          # q = 72 > 64: packer does not apply (dvbs2.h:39)
    if rate == "8/9":
        p0 = B[rate][1]
        return p0 * (A["9/10"][3] / A["9/10"][2]) if pack else p0   # packer ratio borrowed from 9/10 (derived)
    if rate == "4/5":                              # interpolate between 3/4 and 5/6 by address entries
        w = (LDPC_ENTRIES["4/5"] - LDPC_ENTRIES["3/4"]) / (LDPC_ENTRIES["5/6"] - LDPC_ENTRIES["3/4"])
        i = 3 if pack else 2
        return A["3/4"][i] + w * (A["5/6"][i] - A["3/4"][i])
    raise KeyError(rate)


FEC_PROVENANCE = {r: ("measured A" if r in A else "measured B (pack0); pack1 derived" if r == "8/9"
                      else "measured B" if r in B else "derived (interpolated)") for r in KBCH}

# ---------------------------------------------------------------------------
# 4. Live-pipeline calibration (contention, ingress, supervision) from one measured run
# ---------------------------------------------------------------------------
QPSK_FRAME_CYC_16 = frame_symbols("QPSK") / 16e6 * 128e6          # 266,256 cycles per frame at 16 Msym/s
SRC_RUN = ("results/optimizations/2026-10-15/runs.jsonl stage B/C, dualstream QPSK 5/6 16 Msym/s 60 s, "
           "four writers, L8, git 5e38990269")
K_BCH = I("k_bch_live", {"bank0": 0.4907 * QPSK_FRAME_CYC_16 / A["5/6"][0], "bank1": 0.4519 * QPSK_FRAME_CYC_16 / A["5/6"][0]},
          "x isolated", "derived", SRC_RUN + ": split_bch_busy 0.4907 (gcs16-bank0-pack0) / 0.4519 (gcs16-bank1-pack1)")
K_C1 = I("k_ldpc_map_live", 0.6976 * QPSK_FRAME_CYC_16 / (A["5/6"][3] + A["5/6"][4]), "x isolated", "derived",
         SRC_RUN + ": s2_busy 0.6976 (gcs16-bank1-pack1) vs isolated LDPC(pack1)+map")
BB_QPSK56_16 = bb_rate("QPSK", "5/6", 16e6)
C_IN = I("ingress_cycles_per_bb_bit", {"GCS": 0.1224 * 128e6 / BB_QPSK56_16, "TS": 0.2389 * 128e6 / BB_QPSK56_16},
         "cycles/bit", "derived", SRC_RUN + ": ts_busy 0.1224 (GCS, gcs16-bank1-pack1), 0.2389 (TS, ts16-bank1-pack1); "
         "PV-SPI parse + BBFRAME build on core 1")
U_DEMO = I("u_demonstrated", 0.9628, "fraction of a core", "measured",
           "results/apsk-fec/2026-10-15/README.md: 16APSK 150 MHz core 1 96.28% passed 60 s; failures at "
           ">=99.2% (results/integration/2026-10-15/README.md 32APSK CPW8) and 99.8-99.9% (optimizations README)")
U_PLAN = I("u_planning", 0.90, "fraction of a core", "assumed",
           "10% reserve: measured worst-frame excursion +4.3% (s2_max_frame_cyc 193,720 vs mean 185,740, "
           "gcs16-bank1-pack1) plus unmodelled placement contention (memory-placement A/B/A: up to 31% BCH)")

# ---------------------------------------------------------------------------
# 5. Shaping engines
# ---------------------------------------------------------------------------
PHI_DESC = I("supervision_desc_cps", 0.121 * 128e6, "cycles/s", "measured",
             SRC_RUN + ": control_busy 0.121 at 16 Msym/s; 0.1209 at 8 Msym/s (results/gcs-ingress/2026-10-15/"
             "soak-four-1) -> fixed time-based cost (32 us polling)")
PHI_PTR = I("supervision_ptr_cps", 0.033803 * 128e6, "cycles/s", "measured",
            "results/quad-pointers/2026-10-15/poll128-soak-1/runs.jsonl: supervision_busy 0.033803 @128 MHz, "
            "0.028757 @150 MHz (same cycles/s); cyclic PRBS source, no SPI/FEC")
PIO_PTR_CPS_L8 = I("pio_pointer_cycles_per_symbol_L8", 49 / 16, "PIO cycles/symbol/axis", "derived",
                   "results/waveforms/2026-10-15/pulse-design.md: 49 non-stalled instructions per 16-symbol raw word")
PIO_PTR_CPS_4LVL = I("pio_pointer_cycles_per_symbol_4level", 4.0, "PIO cycles/symbol/axis", "assumed",
                     "no 4-level pointer program exists; assume <= 4 instructions/symbol (2-bit IN, MOV, PUSH)")


def table_bytes(levels, span, rows_syms, n_sps, bits_per_level=None):
    """Full-history table: one row per distinct axis history of span+R-1 levels; R*N int16 samples per row.
    With bits_per_level the key is a PIO bit concatenation (2^(b*(L+R-1)) rows)."""
    k = span + rows_syms - 1
    rows = (2 ** (bits_per_level * k)) if bits_per_level else levels ** k
    return rows * rows_syms * n_sps * 2


ENGINES = {
    "qpsk_desc_n4": dict(mods={"QPSK"}, N=4, R=4, hw_lut=True, chains=1, c_shape=2.0045, phi=PHI_DESC,
                         table=table_bytes(2, 8, 4, 4), pio=None, status="measured: complete SPI/FEC 60 s",
                         src=SRC_RUN + ": gen_cyc_per_sym 2.0045 (interpolator descriptors, L8, 4-symbol rows)"),
    "qpsk_ptr_n4": dict(mods={"QPSK"}, N=4, R=4, hw_lut=True, chains=1, c_shape=0.0, phi=PHI_PTR,
                        table=table_bytes(2, 8, 4, 4), pio=PIO_PTR_CPS_L8, src_bits=1,
                        status="measured: PRBS source only (A1 integrating live FEC)",
                        src="results/quad-pointers/2026-10-15/poll128-soak-1"),
    "qpsk_ptr_n2": dict(mods={"QPSK"}, N=2, R=4, hw_lut=True, chains=1, c_shape=0.0, phi=PHI_PTR,
                        table=table_bytes(2, 8, 4, 2), pio=PIO_PTR_CPS_L8, src_bits=1,
                        status="hypothetical: N2 variant of the pointer engine", src="derived"),
    "psk2plane_n2": dict(mods={"QPSK", "8PSK"}, N=2, R=4, hw_lut=False, c_shape=12.0803, phi=0.0,
                         table=131072, status="measured: complete 8PSK SPI/FEC 60 s",
                         src="results/8psk-live/2026-10-15/bch-placement-soak-1/runs.jsonl gen_cyc_per_sym 12.0803 "
                             "@128 (12.0996 @150); L9, two signed planes, 128 KiB"),
    "psk2plane_n4": dict(mods={"QPSK", "8PSK"}, N=4, R=4, hw_lut=False, c_shape=12.0803 * K_N, phi=0.0,
                         table=2 * table_bytes(2, 8, 4, 4), status="derived: N2 cost x K_N; L8 planes",
                         src="derived"),
    "hw4_L7R1_n4": dict(mods={"QPSK", "8PSK"}, N=4, R=1, hw_lut=True, chains=1, c_shape=0.0, phi=PHI_PTR,
                        table=table_bytes(4, 7, 1, 4), pio=PIO_PTR_CPS_4LVL, src_bits=2,
                        status="hypothetical: Codex B1 (4-level L7 full-history)", src="strategy-report P2"),
    "hw4_L7R1_n4_2ch": dict(mods={"QPSK", "8PSK"}, N=4, R=1, hw_lut=True, chains=2, c_shape=0.0, phi=PHI_PTR,
                            table=table_bytes(4, 7, 1, 4), pio=PIO_PTR_CPS_4LVL / 2, src_bits=2,
                            status="hypothetical: B1 with two DMA chains/axis, 4 writers, 4 pointer SMs",
                            src="this note"),
    "hw4_L7R1_n2": dict(mods={"QPSK", "8PSK"}, N=2, R=1, hw_lut=True, chains=1, c_shape=0.0, phi=PHI_PTR,
                        table=table_bytes(4, 7, 1, 2), pio=PIO_PTR_CPS_4LVL, src_bits=2,
                        status="hypothetical: 4-level L7 at N2 (pulse_check: L7 N2 hold-model passes)",
                        src="this note"),
    "hw4_L7R1_n2_2ch": dict(mods={"QPSK", "8PSK"}, N=2, R=1, hw_lut=True, chains=2, c_shape=0.0, phi=PHI_PTR,
                            table=table_bytes(4, 7, 1, 2), pio=PIO_PTR_CPS_4LVL / 2, src_bits=2,
                            status="hypothetical: 4-level L7 at N2, two DMA chains/axis", src="this note"),
    "apsk16_sw_n2": dict(mods={"16APSK"}, N=2, R=4, hw_lut=False, c_shape=20.7828, phi=0.0, table=139264,
                         status="measured: complete SPI/FEC split 60 s",
                         src="results/optimizations/2026-10-15/apsk16-split-soak gen_cyc_per_sym 20.7828 @150; "
                             "136 KiB 3+4+4 chunk tables (results/apsk-dual/2026-10-15/README.md)"),
    "apsk32_sw_n2": dict(mods={"32APSK"}, N=2, R=2, hw_lut=False, c_shape=19.8991 + 14.0411, phi=0.0, table=117912,
                         status="measured: complete SPI/FEC split 60 s",
                         src="results/optimizations/2026-10-15/apsk32-split-soak gen 19.8991 + prepare 14.0411 "
                             "cycles/symbol @150; 117,912 B rolling-key tables"),
    "apsk16_sw_n4": dict(mods={"16APSK"}, N=4, R=4, hw_lut=False, c_shape=20.7828 * K_N, phi=0.0,
                         table=2 * 139264, status="derived: N2 cost x K_N, tables x2", src="derived"),
    "apsk32_sw_n4": dict(mods={"32APSK"}, N=4, R=2, hw_lut=False, c_shape=19.8991 * K_N + 14.0411, phi=0.0,
                         table=2 * 117912, status="derived: N2 shaping x K_N, tables x2", src="derived"),
    "apsk16_hw_L7": dict(mods={"16APSK"}, N=None, R=1, hw_lut=True, chains=1, c_shape=0.0, phi=PHI_PTR,
                         table=None, levels=8, bits=3, pio=4.0, src_bits=3,
                         status="hypothetical: full-history 8-level L7 (minimum mask span)", src="this note"),
    "apsk32_hw_L7": dict(mods={"32APSK"}, N=None, R=1, hw_lut=True, chains=1, c_shape=0.0, phi=PHI_PTR,
                         table=None, levels=17, bits=5, pio=4.0, src_bits=5,
                         status="hypothetical: full-history 17-level L7 (minimum mask span)", src="this note"),
}
for name, e in ENGINES.items():
    I(f"engine.{name}", {k: v for k, v in e.items() if k not in ("mods",)}, "mixed",
      "measured" if e["status"].startswith("measured") else "assumed" if e["status"].startswith("hypothetical")
      else "derived", e["src"], e["status"])


def _min_span():
    """Shortest linear-phase span passing Annex A (any model, zero margin) from pulse_check.json if present."""
    path = HERE / "pulse_check.json"
    if path.exists():
        cases = json.loads(path.read_text())["cases"]
        ok = [c["L"] for c in cases if c["feasible"] and c["margin_db"] == 0.0]
        return min(ok)
    return 7


L_MIN = I("min_mask_span", _min_span(), "symbols", "derived",
          "pulse_check.py/json (SciPy HiGHS LP on a grid; reproduces 5/5 pulse-design.md outcomes): "
          "L<=6 infeasible at N2 and N4 for digital, hold and both models; grid infeasibility implies continuous "
          "infeasibility for symmetric FIRs")


def engine_table(e, n_sps):
    if e["table"] is not None:
        return e["table"]
    return table_bytes(e["levels"], L_MIN, e["R"], n_sps, bits_per_level=e["bits"])


# ---------------------------------------------------------------------------
# 6. DMA hand-off evidence (per-axis chain: control pointer -> sample burst -> chain back)
# ---------------------------------------------------------------------------
T_ROW_OK = I("dma_row_period_demonstrated", 16, "SYS cycles per row per axis chain", "measured",
             "R1 rows at 8 Msym/s (16 cycles) and R2 rows at 16 Msym/s, 4 writers (16 cycles) pass 60 s: "
             "results/dual-dma/2026-10-14/README.md table, results/dual-dma/2026-10-15/README.md qualify-1")
T_ROW_FAIL = I("dma_row_period_failed", 8, "SYS cycles per row per axis chain", "measured",
               "R1 rows at 16 Msym/s stall in every record, even with precomputed pointers: "
               "results/dac-clock/2026-10-15/README.md 'layout-coverage'; results/dual-dma/2026-10-14/README.md "
               "'Retained failed probes'")
ETA_DMA_DEMO = I("dma_bandwidth_demonstrated", (4 + 0.5 + 2 / 32) * 16e6 / 128e6, "fraction of transfers/cycle",
                 "derived", "PIO-pointer QPSK at 16 Msym/s (results/quad-pointers/2026-10-15/poll128-soak-1): 4 data + "
                 "0.5 pointer + 0.0625 source transfers/symbol = 73 M/s of 128 M/s (credit DMAs not counted)")

DMA_CH_PER_POINTER_SM = I("dma_channels_per_pointer_sm", 2, "channels", "measured",
                          "firmware/src/iqquadptr.c:209-264 (input_dma + credit_dma per pointer SM)")
DMA_CH_PER_CHAIN = I("dma_channels_per_output_chain", 2, "channels", "measured",
                     "results/waveforms/2026-10-15/README.md 'Two control/sample-DMA pairs'; dual_dma.c")


def resources(e):
    """(state machines, DMA channels) for a hardware-LUT engine in a flight build (no capture SM/channel)."""
    chains = 2 * e.get("chains", 1)                       # per axis
    ptr_sms = chains
    writers = 4 if (e.get("chains", 1) > 1 or e["N"] == 4) else 2
    sms = writers + ptr_sms + 1 + 2                      # + DDR clock + PV-SPI rx/cs
    dma = ptr_sms * DMA_CH_PER_POINTER_SM + chains * DMA_CH_PER_CHAIN + 1   # + PV-SPI rx
    return sms, dma


# ---------------------------------------------------------------------------
# 7. SRAM budget (estimate from the integration image ELF)
# ---------------------------------------------------------------------------
ELF = "results/integration/2026-10-15/iqbench.elf (git 8014ed33b9), arm-none-eabi-readelf -S / nm -S"
SRAM_ITEMS = I("sram_items", {
    # name: (bytes, class)  class in {core, fec, lab, legacy, table_slot}
    "hot code .data": (40440, "core"), "scratch X+Y (code+stacks)": (8192, "core"),
    "misc .bss/heap/vectors": (6680 + 1024 + 272, "core"),
    "PV-SPI queue link_ring": (32768, "core"), "symbol source in_buf": (32768, "core"),
    "ring_buf rings (non-table half)": (65536, "core"), "idle_block": (16384, "core"),
    "FEC buffers (bb_buf,lpar,fe,bb_prbs,bb_save,par_buf,flipm,swapm,r0,r1,P,a_off,a_row,bch_tab,crc_t,small)":
        (6 * 8108 + 2 * 8104 + 2 * 8304 + 8016 + 2 * 4096 + 32768 + 4096 + 264 + 256 + 326, "fec"),
    "FE slot 2 (borrowed from cap_buf)": (8108, "fec"),
    "QPSK table slot in ring_buf": (65536, "table_slot"),
    "capture buffer cap_buf minus FE slot": (57344 - 8108, "lab"),
    "legacy iqout tables tab_i+tab_q": (65536, "legacy"),
    "unallocated (SRAM0-3, SRAM4-7, scratch)": (680 + 4464 + 16 + 288, "free")}, "B", "derived",
    ELF + "; classes are assumptions about a single-profile flight image")
FEC_CTX2 = I("sram_second_fec_context", 6 * 8108, "B", "assumed",
             "frame-parallel FEC needs a second set of per-frame workspaces; ~6 frame-sized buffers (r0,r1,P,lpar,"
             "par_buf,fe ~ 8.1 KiB each); A1 will measure the real figure")


SRAM_PARTITION_EXTRA = {"fp": FEC_CTX2, "bal": 8108}   # bal: one more FE slot (next-experiments.md #1)


def sram_table_budget(scenario):
    """Bytes available for shaping tables (+ engine-specific extras) under a scenario."""
    it = {k: v for k, (v, c) in SRAM_ITEMS.items()}
    cls = {k: c for k, (v, c) in SRAM_ITEMS.items()}
    slot = sum(v for k, v in it.items() if cls[k] == "table_slot")
    free = sum(v for k, v in it.items() if cls[k] == "free")
    lab = sum(v for k, v in it.items() if cls[k] in ("lab", "legacy"))
    if scenario == "lab_image":
        return slot + free                     # benches borrow other arenas while TX is idle
    if scenario == "flight":
        return slot + free + lab
    if scenario == "flight_fp":
        return slot + free + lab - FEC_CTX2
    raise KeyError(scenario)


assert abs(sum(v for v, c in SRAM_ITEMS.values()) - SRAM_TOTAL) < 6 * 1024, "SRAM itemisation should cover ~520 KiB"

# ---------------------------------------------------------------------------
# 8. PV-SPI ingress
# ---------------------------------------------------------------------------
MSG_WIRE, MSG_PAYLOAD = I("pvspi_message", (1332, 1316), "B", "spec", "docs/pv-spi-spec.md message format")
GAP = I("pvspi_cs_gap", 10e-6, "s", "spec", "docs/pv-spi-spec.md 'CS_N must be high for >= 10 us'")
SCK_EXT = I("sck_external_proven", 25e6, "Hz", "measured",
            "results/cm5-spi/README.md faults-25mhz (CM5, 16,001 messages at 982 msg/s, not back-to-back)")
ONCHIP_CYC_PER_MSG = I("onchip_master_cycles_per_message", 43654, "SYS cycles @128 MHz", "derived",
                       "results/waveforms/2026-10-15/three-sps.md (32 MHz on-chip master incl. 1,024-cycle gap)")


def spi_bb_max(sck, gap=GAP, lanes=1):
    return lanes * MSG_PAYLOAD * 8 / (MSG_WIRE * 8 / sck + gap)


def sck_needed(bb_bps, gap=GAP, lanes=1):
    """Per-lane SCK (Hz) needed to carry bb_bps of payload with fixed 1332-byte messages and a CS gap."""
    t_msg = lanes * MSG_PAYLOAD * 8 / bb_bps - gap
    return MSG_WIRE * 8 / t_msg if t_msg > 0 else math.inf


SPI_CAPS = {"ext25": spi_bb_max(SCK_EXT), "onchip32": MSG_PAYLOAD * 8 / (ONCHIP_CYC_PER_MSG / 128e6),
            "ext20": spi_bb_max(20e6)}
I("spi_bb_caps", SPI_CAPS, "bit/s", "derived", "1316*8/(1332*8/SCK + gap) per lane")


# ---------------------------------------------------------------------------
# 9. The model
# ---------------------------------------------------------------------------
def stage_costs(mod, rate, fec, ingress):
    """Live cycles per on-air symbol for each encoder stage."""
    s = frame_symbols(mod)
    kb = K_BCH["bank0"] if fec == "base" else K_BCH["bank1"]
    pack = fec != "base"
    c = dict(
        ingress=C_IN[ingress] * dfl(rate) / s,
        bch=bbbch_cycles(rate, fec) * kb / s,
        ldpc=ldpc_cycles(rate, pack) * K_C1 / s,
    )
    if mod == "QPSK":
        c["map"] = A["5/6"][4] * K_C1 / s
    elif mod == "8PSK":
        c["map"] = B["5/6"][2] * K_C1 / s
    else:
        c["map"] = MAP_APSK[mod] / s           # live already
    return c


def frontend_split_ok(rate, fec, e=None):
    """BB/BCH on core 0 alongside shaping. The sliced (128-word) frontend exists only for t=10 (bch_deg 160,
    firmware/src/dvbs2.c:669); otherwise bench_dual.c:318-324 runs a whole-frame BCH (~1.3 ms) on core 0. The
    descriptor engine cannot absorb that; the PIO-pointer engine can only if the generic kernel is also sliced
    (same cycles) or supervision moves to an IRQ, because its credit supervision must run every <= 256 us
    (results/waveforms/2026-10-15/pointer-polling-proof.md). bchT assumes sliced, faster t=8/12 kernels."""
    return BCH_T[rate] == 10 or fec == "bchT" or bool(e and e.get("pio"))


PARTITIONS = {
    "pipe0": "core 0 shapes (+supervision); core 1 runs ingress+BB/BCH+LDPC+map (8PSK/APSK no-split today)",
    "pipe1": "core 0 shapes + BB/BCH frontend; core 1 ingress+LDPC+map (QPSK 16 Msym/s, APSK split today; "
             "t=10 codes only unless a t=8/12 frontend kernel exists)",
    "bal": "ideal balance by job sharing (BCH frontend + shared PL-map jobs + shared shaping blocks); "
           "ingress+LDPC stay on one core (apsk-fec next-experiments.md #1)",
    "fp": "frame-parallel FEC: each core encodes whole frames; ideal balance; needs a second FEC context (Codex A1)",
}


def _split(c, shape, partition, rate, fec, e=None):
    """(core0, core1) cycles/symbol for pipelined partitions."""
    if partition == "pipe1" and frontend_split_ok(rate, fec, e):
        return shape + c["bch"], c["ingress"] + c["ldpc"] + c["map"]
    return shape, sum(c.values())


def cpu_bounds(mod, rate, e, n_sps, partition, fec, ingress, sys_hz, u):
    """Symbol-rate bounds from compute: (a) FEC-only, (b) shaping-only, (ab) joint."""
    c = stage_costs(mod, rate, fec, ingress)
    phi = e["phi"] / sys_hz                     # supervision, fraction of one core
    shape = e["c_shape"]
    fec_sum = sum(c.values())
    indivisible = c["ingress"] + c["ldpc"]      # cannot be shared without a second FEC context
    out = {}
    if partition == "fp":
        out["fec"] = 2 * u * sys_hz / fec_sum
        out["cpu"] = (2 * u - phi) * sys_hz / (fec_sum + shape)
    elif partition == "bal":
        out["fec"] = min(2 * u * sys_hz / fec_sum, u * sys_hz / indivisible)
        out["cpu"] = min((2 * u - phi) * sys_hz / (fec_sum + shape), u * sys_hz / indivisible)
    else:
        f0, f1 = _split(c, 0.0, partition, rate, fec, e)
        out["fec"] = min(u * sys_hz / f1, u * sys_hz / f0 if f0 else math.inf)
        c0, c1 = _split(c, shape, partition, rate, fec, e)
        out["cpu"] = min((u - phi) * sys_hz / c0 if c0 else math.inf, u * sys_hz / c1)
    if e["hw_lut"]:
        out["shape"] = sys_hz / e["pio"] if e.get("pio") else math.inf   # PIO address generation
    else:
        out["shape"] = (2 if partition in ("fp", "bal") else 1) * u * sys_hz / shape
    out["c_fec_sym"] = fec_sum
    out["c_shape_sym"] = shape
    out["stages"] = c
    return out


def loads_at(mod, rate, e, partition, fec, ingress, sys_hz, rs):
    """Predicted (core0, core1) work fractions at symbol rate rs."""
    c = stage_costs(mod, rate, fec, ingress)
    phi = e["phi"] / sys_hz
    shape = e["c_shape"]
    if partition in ("fp", "bal"):
        x = (rs * (sum(c.values()) + shape) / sys_hz + phi) / 2
        if partition == "bal":
            x = max(x, rs * (c["ingress"] + c["ldpc"]) / sys_hz)
        return x, x
    c0, c1 = _split(c, shape, partition, rate, fec, e)
    return rs * c0 / sys_hz + phi, rs * c1 / sys_hz


def dma_bounds(e, n_sps, sys_hz):
    if not e["hw_lut"]:
        return dict(dma_bw=DMA_XFER_PER_CYCLE * sys_hz / n_sps, handoff_ok=math.inf, handoff_fail=math.inf,
                    xfer_per_sym=n_sps)
    xfer = n_sps + 2 / e["R"] + 2 * e.get("src_bits", 0) / 32
    ch = e.get("chains", 1)
    return dict(dma_bw=DMA_XFER_PER_CYCLE * sys_hz / xfer, xfer_per_sym=xfer,
                handoff_ok=e["R"] * ch * sys_hz / T_ROW_OK, handoff_fail=e["R"] * ch * sys_hz / T_ROW_FAIL)


def afe_bound(n_sps):
    return min(F_DAC_MAX / n_sps, BW_RF_MAX / (1 + ALPHA))


def grid(rs_limit, n_sps, sys_hz):
    """Largest Rs = SYS/(2 N d), integer d >= 1, not above rs_limit (DDR clock / writer divider grid)."""
    if rs_limit <= 0 or not math.isfinite(rs_limit) and rs_limit < 0:
        return 0.0
    d = max(1, math.ceil(sys_hz / (2 * n_sps * rs_limit) - 1e-9))
    return sys_hz / (2 * n_sps * d)


def image_screen(n_sps, rs):
    """First DAC image start offset and typical AFE tune-0 attenuation there (screening only)."""
    fs = n_sps * rs
    f_img = fs - (1 + ALPHA) * rs / 2
    f, db = zip(*AFE_TUNE0)
    att = float(np.interp(np.log10(f_img), np.log10(f), db, left=None, right=db[-1]))
    if f_img < f[0]:
        att = float(np.interp(np.log10(f_img), [np.log10(f[0] / 2), np.log10(f[0])], [0.0, db[0]]))
    return f_img, att


def evaluate(mod, rate, ename, n_sps=None, partition="pipe1", fec="meas", ingress="GCS", sys_hz=SYS128,
             u=U_DEMO, spi="onchip32", sram="flight"):
    e = ENGINES[ename]
    n = n_sps or e["N"]
    r = dict(mod=mod, rate=rate, engine=ename, N=n, partition=partition, fec=fec, ingress=ingress,
             sys_mhz=sys_hz / 1e6, u=u, spi=spi, sram_scenario=sram, engine_status=e["status"])
    cb = cpu_bounds(mod, rate, e, n, partition, fec, ingress, sys_hz, u)
    db = dma_bounds(e, n, sys_hz)
    tb = engine_table(e, n)
    budget = sram_table_budget(sram) - SRAM_PARTITION_EXTRA.get(partition, 0)
    bounds = {"a_fec": cb["fec"], "b_shape": cb["shape"], "ab_cpu": cb["cpu"], "c_dma_bw": db["dma_bw"],
              "c_dma_handoff": db["handoff_ok"], "e_afe": afe_bound(n), "f_spi": SPI_CAPS[spi] * frame_symbols(mod) / dfl(rate)}
    sram_ok = tb <= budget
    r.update({f"rs_{k}": v / 1e6 for k, v in bounds.items()})
    r["rs_f_spi_ext25"] = SPI_CAPS["ext25"] * frame_symbols(mod) / dfl(rate) / 1e6
    r["table_kib"] = tb / 1024
    r["sram_budget_kib"] = budget / 1024
    r["d_sram_ok"] = sram_ok
    r["dma_xfer_per_sym"] = db["xfer_per_sym"]
    # Symbol rate is set by compute, DMA, AFE (and SRAM feasibility). Ingress caps delivered data, not Rs:
    # with READY flow control a short input stream is padded (GCS padding / dummy frames), the clock keeps running.
    binding_set = {k: v for k, v in bounds.items() if k in ("ab_cpu", "c_dma_bw", "c_dma_handoff", "e_afe")}
    if not sram_ok:
        r.update(rs_limit=0.0, rs_grid=0.0, bb_mbps=0.0, binding="d_sram", load_c0=float("nan"),
                 load_c1=float("nan"), bb_ext25_mbps=0.0, bb_onchip_mbps=0.0, grid_loss_pct=float("nan"),
                 flags=f"table {tb / 1024:.0f} KiB > budget {budget / 1024:.0f} KiB", esn0_db=ESN0.get((mod, rate)))
        return r
    key = min(binding_set, key=binding_set.get)
    lim = binding_set[key]
    rs_q = grid(lim, n, sys_hz)
    cap = bb_rate(mod, rate, rs_q)
    r.update(rs_limit=lim / 1e6, rs_grid=rs_q / 1e6, bb_mbps=cap / 1e6, binding_rs=key)
    r["grid_loss_pct"] = 100 * (1 - rs_q / lim) if lim > 0 else 0
    d_now = round(sys_hz / (2 * n * rs_q))
    r["rs_next_step"] = sys_hz / (2 * n * (d_now - 1)) / 1e6 if d_now > 1 else float("nan")
    r["next_step_needs_pct"] = 100 * (r["rs_next_step"] * 1e6 / lim - 1) if d_now > 1 else float("nan")
    r["bb_onchip_mbps"] = min(cap, SPI_CAPS["onchip32"]) / 1e6
    r["bb_ext25_mbps"] = min(cap, SPI_CAPS["ext25"]) / 1e6
    r["binding"] = "f_spi" if SPI_CAPS[spi] < cap * (1 - 1e-9) else key
    l0, l1 = loads_at(mod, rate, e, partition, fec, ingress, sys_hz, rs_q)
    r["load_c0"], r["load_c1"] = float(l0), float(l1)
    # unknown-territory flags
    flags = []
    if e["hw_lut"] and rs_q > db["handoff_ok"] * (1 + 1e-9):
        flags.append("DMA handoff beyond demonstrated")
    if e["hw_lut"] and rs_q * db["xfer_per_sym"] > ETA_DMA_DEMO * sys_hz * (1 + 1e-9):
        flags.append(f"DMA {100 * rs_q * db['xfer_per_sym'] / sys_hz:.0f}% > demonstrated {100 * ETA_DMA_DEMO:.0f}%")
    if max(l0, l1) > U_PLAN:
        flags.append(f"core load {100 * max(l0, l1):.0f}% > planning {100 * U_PLAN:.0f}%")
    if n == 2:
        f_img, att = image_screen(2, rs_q)
        flags.append(f"N2 image at {f_img / 1e6:.1f} MHz (AFE tune0 ~{att:.0f} dB), unqualified")
    if e["hw_lut"] and e.get("pio"):
        sms, dch = resources(e)
        r["pio_sms"], r["dma_channels"] = sms, dch
        if sms > 12 or dch > 16:
            flags.append(f"needs {sms} SMs / {dch} DMA ch (limit 12/16)")
    if not e["status"].startswith("measured"):
        flags.append(e["status"].split(":")[0])
    r["flags"] = "; ".join(flags)
    r["esn0_db"] = ESN0.get((mod, rate))
    return r


# ---------------------------------------------------------------------------
# 10. Validation against measured runs (predict from isolated stage costs + calibration)
# ---------------------------------------------------------------------------
def validate():
    rows = []

    def add(label, mod, rate, ename, partition, fec, ingress, sys_hz, rs, meas, src, ingress_scale=1.0):
        p0, p1 = loads_at(mod, rate, ENGINES[ename], partition, fec, ingress, sys_hz, rs)
        rows.append(dict(label=label, pred_c0=p0, pred_c1=p1, meas_c0=meas[0], meas_c1=meas[1], src=src))

    o = "results/optimizations/2026-10-15/runs.jsonl"
    add("QPSK 5/6 16M GCS pack1 bank1 (calibration run)", "QPSK", "5/6", "qpsk_desc_n4", "pipe1", "meas", "GCS",
        SYS128, 16e6, (0.8234, 0.8200), o + " gcs16-bank1-pack1")
    add("QPSK 5/6 16M TS pack1 bank1", "QPSK", "5/6", "qpsk_desc_n4", "pipe1", "meas", "TS", SYS128, 16e6,
        (0.8257, 0.9368), o + " ts16-bank1-pack1")
    add("QPSK 5/6 16M GCS pack0 bank0", "QPSK", "5/6", "qpsk_desc_n4", "pipe1", "base", "GCS", SYS128, 16e6,
        (0.8558, 0.8834), o + " gcs16-bank0-pack0")
    add("8PSK 5/6 8M (no split, pack0)", "8PSK", "5/6", "psk2plane_n2", "pipe0", "base_bank1", "GCS", SYS128,
        8e6, (0.7550, 0.9207), "results/8psk-live/2026-10-15/bch-placement-soak-1 (gen_busy, encoder_busy)")
    add("8PSK 5/6 9.375M @150", "8PSK", "5/6", "psk2plane_n2", "pipe0", "base_bank1", "GCS", SYS150, 9.375e6,
        (0.7562, 0.9209), "same, 150 MHz")
    add("16APSK 5/6 split 4.6875M @150", "16APSK", "5/6", "apsk16_sw_n2", "pipe1", "meas", "GCS", SYS150,
        4.6875e6, (0.8808, 0.8007), "results/optimizations/2026-10-15/apsk16-split-soak")
    add("32APSK 5/6 split 3.125M @150", "32APSK", "5/6", "apsk32_sw_n2", "pipe1", "meas", "GCS", SYS150,
        3.125e6, (0.8975, 0.8773), "results/optimizations/2026-10-15/apsk32-split-soak")
    add("16APSK 5/6 no-split 4.6875M @150 (FAILED, underrun)", "16APSK", "5/6", "apsk16_sw_n2", "pipe0", "meas",
        "GCS", SYS150, 4.6875e6, (float("nan"), 0.998), "results/optimizations/2026-10-15/README.md control row")
    add("16APSK 5/6 no-split 4.1667M @150", "16APSK", "5/6", "apsk16_sw_n2", "pipe0", "meas", "GCS", SYS150,
        4.16667e6, (float("nan"), 0.921), "results/optimizations/2026-10-15/README.md control row")
    for r in rows:
        r["err_c0_pts"] = 100 * (r["pred_c0"] - r["meas_c0"])
        r["err_c1_pts"] = 100 * (r["pred_c1"] - r["meas_c1"])
    return rows


# fec variant used only in validation: pack0 LDPC but bank1-like BCH placement (8PSK image dc9e67977a)
_orig_stage_costs = stage_costs


def stage_costs(mod, rate, fec, ingress):  # noqa: F811  (wrapper adds the validation-only variant)
    if fec == "base_bank1":
        c = _orig_stage_costs(mod, rate, "base", ingress)
        c["bch"] *= K_BCH["bank1"] / K_BCH["bank0"]
        return c
    return _orig_stage_costs(mod, rate, fec, ingress)


# ---------------------------------------------------------------------------
# 11. Hardware-only APSK feasibility
# ---------------------------------------------------------------------------
def axis_levels(mod, rate=None, rotate=0.0, unit_headers=True, norm="outer1"):
    """Distinct per-axis amplitudes over data points, PL header/pilot points (pi/2-BPSK at radius 1 in the
    repo's outer-radius-one convention) and PL-scrambling quarter turns."""
    pts = []
    if mod == "QPSK":
        pts += [np.exp(1j * (np.pi / 4 + k * np.pi / 2)) for k in range(4)]
    elif mod == "8PSK":
        pts += [np.exp(1j * k * np.pi / 4) for k in range(8)]
    elif mod == "16APSK":
        g = GAMMA16[rate]
        r2 = 1.0 if norm == "outer1" else math.sqrt(4 / (1 / g ** 2 + 3))
        r1 = r2 / g
        pts += [r1 * np.exp(1j * (np.pi / 4 + k * np.pi / 2)) for k in range(4)]
        pts += [r2 * np.exp(1j * (np.pi / 12 + k * np.pi / 6)) for k in range(12)]
    elif mod == "32APSK":
        g1, g2 = GAMMA32[rate]
        r3 = 1.0 if norm == "outer1" else math.sqrt(8 / (1 / g2 ** 2 + 3 * (g1 / g2) ** 2 + 4))
        pts += [r3 / g2 * np.exp(1j * (np.pi / 4 + k * np.pi / 2)) for k in range(4)]
        pts += [r3 * g1 / g2 * np.exp(1j * (np.pi / 12 + k * np.pi / 6)) for k in range(12)]
        pts += [r3 * np.exp(1j * k * np.pi / 8) for k in range(16)]
    if unit_headers:
        pts += [np.exp(1j * (np.pi / 4 + k * np.pi / 2)) for k in range(4)]
    pts = np.array(pts) * np.exp(1j * rotate)
    allp = np.concatenate([pts * (1j ** k) for k in range(4)])
    vals = np.concatenate([allp.real, allp.imag])
    return np.unique(np.round(vals, 9))


def hw_apsk_tables():
    out = {}
    lv = {"QPSK": len(axis_levels("QPSK")), "8PSK (native)": len(axis_levels("8PSK")),
          "8PSK/QPSK/headers, pi/8 rotated": len(axis_levels("8PSK", rotate=np.pi / 8)),
          **{f"16APSK {r} (R2=1)": len(axis_levels("16APSK", r)) for r in GAMMA16},
          "16APSK 5/6 (E=1, unit headers)": len(axis_levels("16APSK", "5/6", norm="E1")),
          **{f"32APSK {r} (R3=1)": len(axis_levels("32APSK", r)) for r in GAMMA32}}
    out["levels"] = lv
    sizes = []
    for name, m, b in (("2-level (QPSK)", 2, 1), ("4-level (QPSK/8PSK pi/8)", 4, 2), ("8-level (16APSK)", 8, 3),
                       ("17-level (32APSK)", 17, 5)):
        for n_sps in (2, 4):
            for span in (5, 6, 7, 8, 9):
                for rr in (1, 2, 4):
                    sizes.append(dict(alphabet=name, N=n_sps, L=span, R=rr,
                                      dense_bytes=table_bytes(m, span, rr, n_sps),
                                      pio_bytes=table_bytes(m, span, rr, n_sps, bits_per_level=b)))
    out["full_history"] = sizes
    # partial-table (split-span) schemes: chunks of the L+R-1 history, summed per output word
    part = []
    for name, m, span, rr, n_sps, chunks, src in (
            ("16APSK measured (3+4+4)", 8, 8, 4, 2, (3, 4, 4), "results/apsk-dual/2026-10-15/README.md 139,264 B"),
            ("16APSK L7 R1 N2 (4+3)", 8, 7, 1, 2, (4, 3), "this note"),
            ("16APSK L7 R1 N4 (4+3)", 8, 7, 1, 4, (4, 3), "this note"),
            ("16APSK L8 R4 N4 (3+4+4)", 8, 8, 4, 4, (3, 4, 4), "this note"),
            ("32APSK measured (3+3+3)", 17, 8, 2, 2, (3, 3, 3), "results/apsk-frontier/2026-10-15/README.md 117,912 B"),
            ("32APSK L7 R1 N2 (3+2+2)", 17, 7, 1, 2, (3, 2, 2), "this note"),
            ("32APSK L8 R2 N4 (3+3+3)", 17, 8, 2, 4, (3, 3, 3), "this note")):
        assert sum(chunks) == span + rr - 1
        b = sum(m ** c * rr * n_sps * 2 for c in chunks)
        part.append(dict(scheme=name, chunks=chunks, bytes=b, adds_per_output_word=len(chunks) - 1,
                         words_per_symbol=n_sps, src=src))
    out["partial"] = part
    # DMA-sniffer SUM as the only CPU-free adder: S data reads + S re-arm writes + 1 read-out + 1 reset per word
    snif = []
    for s_terms in (2, 3):
        for n_sps in (2, 4):
            xfer = 2 * s_terms + 2
            snif.append(dict(S=s_terms, N=n_sps, transfers_per_output_word=xfer,
                             rs_bw_bound_msym=SYS128 / (xfer * n_sps) / 1e6))
    out["sniffer"] = snif
    out["sniffer_src"] = ("docs/sources/rp2350.txt line 85588 (SNIFF_CTRL.CALC 0xf SUM, p1135); one sniffer, observes "
                          "one channel; PV-SPI already uses it for message CRC-32 (firmware/src/pvspi.c slot_crc, "
                          "docs/pv-spi-spec.md)")
    out["sio_src"] = ("docs/sources/rp2350.txt line 43724 (p596 'The DMA cannot access registers in the SIO') and "
                      "line 83218 (§12.6.7 p1104 'SIO is not visible from the DMA bus ports'): interpolator adders are "
                      "CPU-only")
    return out


# ---------------------------------------------------------------------------
# 12. Scenarios and levers
# ---------------------------------------------------------------------------
def best(mod, rate, n_sps, engines, partitions, **kw):
    cands = []
    for en in engines:
        e = ENGINES[en]
        if mod not in e["mods"] or (e["N"] not in (None, n_sps)):
            continue
        for p in partitions:
            cands.append(evaluate(mod, rate, en, n_sps=n_sps, partition=p, **kw))
    if not cands:
        return None
    return max(cands, key=lambda r: (round(r["rs_grid"], 6), -max(r["load_c0"], r["load_c1"])
                                     if r["rs_grid"] > 0 else 0))


MEASURED_ENGINES = ["qpsk_desc_n4", "psk2plane_n2", "apsk16_sw_n2", "apsk32_sw_n2"]
SCENARIOS = [  # cumulative ladder; each adds to the previous one
    ("S0 today", MEASURED_ENGINES, ["pipe0", "pipe1"], "meas",
     "measured engines, LDPC packer + scratch-Y BCH, current core split"),
    ("S1 +A1", ["qpsk_ptr_n4"], ["fp"], "meas", "PIO-pointer QPSK + live FEC + frame-parallel FEC"),
    ("S2 +B1", ["hw4_L7R1_n4"], [], "meas", "4-level L7 R1 full-history table at N4 (one DMA chain/axis)"),
    ("S3 +C1", [], [], "bchT", "FEC throughput: modelled only as t=8/t=12 BCH kernels (+frontend split for all t)"),
    ("S4 beyond", ["hw4_L7R1_n4_2ch", "hw4_L7R1_n2", "hw4_L7R1_n2_2ch", "qpsk_ptr_n2", "psk2plane_n4", "apsk16_sw_n4", "apsk32_sw_n4",
                   "apsk16_hw_L7", "apsk32_hw_L7"], ["bal"], "bchT",
     "this note: 2 DMA chains/axis, N2 4-level table, PL-job balance, software N4 APSK, hardware APSK"),
]


def best(mod, rate, n_sps, engines, partitions, **kw):
    cands = []
    for en in engines:
        e = ENGINES[en]
        if mod not in e["mods"] or (e["N"] not in (None, n_sps)):
            continue
        for p in partitions:
            cands.append(evaluate(mod, rate, en, n_sps=n_sps, partition=p, **kw))
    if not cands:
        return dict(mod=mod, rate=rate, N=n_sps, engine="(none)", partition="-", rs_grid=0.0, bb_mbps=0.0,
                    binding="no engine", flags="no engine for this modulation at this N", load_c0=float("nan"),
                    load_c1=float("nan"))
    feas = [c for c in cands if c["rs_grid"] > 0]
    if not feas:
        return cands[0]
    def risk(r):   # count risk flags other than the common N2-image note
        return sum(1 for x in r.get("flags", "").split("; ") if x and not x.startswith("N2 image"))
    return max(feas, key=lambda r: (round(r["rs_grid"], 6), -risk(r), -max(r["load_c0"], r["load_c1"])))


def scenarios(sys_hz=SYS128):
    rows = []
    for mod, rates in MODCODS.items():
        for rate in rates:
            for n_sps in (4, 2):
                engs, parts = [], []
                for name, add_e, add_p, fec, desc in SCENARIOS:
                    engs += add_e
                    parts += add_p
                    r = best(mod, rate, n_sps, engs, parts, fec=fec, sys_hz=sys_hz)
                    r["scenario"] = name
                    rows.append(r)
    return rows


def levers():
    """BB-capacity effect of individual levers, each relative to the measured QPSK 5/6 baseline."""
    out = []
    base = evaluate("QPSK", "5/6", "qpsk_desc_n4", partition="pipe1", fec="meas")

    def row(group, name, r, cost, exp):
        out.append(dict(group=group, lever=name, bb_mbps=r["bb_mbps"], ref_mbps=base["bb_mbps"],
                        bb_onchip=r.get("bb_onchip_mbps", 0.0),
                        gain_pct=100 * (r["bb_mbps"] / base["bb_mbps"] - 1),
                        bb_ext25=r.get("bb_ext25_mbps", 0.0), rs=r["rs_grid"], binding=r["binding"],
                        load=(round(r["load_c0"], 3), round(r["load_c1"], 3)), esn0=r.get("esn0_db"),
                        flags=r.get("flags", ""), cost_risk=cost, experiment=exp))

    row("ref", "QPSK 5/6 N4 16 Msym/s, measured (desc engine, split FEC)", base, "-", "-")
    r = evaluate("QPSK", "5/6", "qpsk_desc_n4", partition="pipe1", fec="meas", ingress="TS")
    row("i", "QPSK 5/6 16 Msym/s with TS input (today)", r, "core 1 at ~94%", "-")
    r = evaluate("QPSK", "5/6", "qpsk_ptr_n4", partition="fp", fec="meas", ingress="TS")
    row("i", "QPSK 5/6 16 Msym/s, TS input, A1 (pointers + frame-parallel)", r, "A1 scope", "A1 60 s TS run")
    r = evaluate("QPSK", "9/10", "qpsk_desc_n4", partition="pipe1", fec="meas")
    row("i", "QPSK 9/10, today's code (no t=8 frontend: BCH on core 1)", r, "-", "-")
    r = evaluate("QPSK", "9/10", "qpsk_desc_n4", partition="pipe1", fec="bchT")
    row("i", "QPSK 9/10, t=8 BCH frontend kernel, today's descriptor engine", r,
        "+1.24 dB Es/N0; ~300 B new kernel; SPI >= 29 MHz",
        "bench: bch128 kernel CRC vs dvbs2_bch on all t=8 frames; dualstream code 10, 60 s")
    r = evaluate("QPSK", "9/10", "qpsk_ptr_n4", partition="pipe1", fec="meas")
    row("i", "QPSK 9/10, PIO pointers + generic BCH sliced on core 0 (no frame-parallel, no faster kernel)", r,
        "A1 pointer path + slice the generic BCH (<=256 us chunks) or IRQ supervision",
        "A1 pointer path at code index 10 with sliced generic BCH, before frame-parallel")
    r = evaluate("QPSK", "9/10", "qpsk_ptr_n4", partition="fp", fec="meas")
    row("i", "QPSK 9/10 via A1 alone (frame-parallel, existing BCH)", r, "A1 scope; +49 KiB FEC context (est.)",
        "A1 run at code index 10")
    r = evaluate("QPSK", "9/10", "qpsk_ptr_n4", partition="fp", fec="bchT", sys_hz=130e6)
    row("i", "QPSK 9/10 at SYS130 (16.25 Msym/s, 65.0 MS/s)", r,
        "AFE at its 65 MSPS limit; PLL 12x130/(6x2); 7.69 ns DDR period", "PRBS writers + GP28 clock at SYS130")
    for rate in ("2/3", "3/4", "5/6"):
        r = evaluate("8PSK", rate, "hw4_L7R1_n4_2ch", partition="fp", fec="bchT")
        row("ii", f"8PSK {rate} N4 16 Msym/s: 4-level L7 R1, 2 chains/axis, frame-parallel FEC", r,
            "DMA ~77% of slots and 16-cycle rows (unproven); 11 of 12 SMs; 128 KiB",
            "any 128 KiB table, R1 rows, 4 writers, 2 chains/axis, PRBS 16 Msym/s 60 s")
    r = evaluate("8PSK", "5/6", "hw4_L7R1_n4", partition="fp", fec="meas")
    row("ii", "8PSK 5/6 N4 via B1 as specified (1 chain/axis)", r, "B1 scope", "B1")
    r = evaluate("8PSK", "5/6", "psk2plane_n2", partition="pipe0", fec="meas")
    row("ii", "8PSK 5/6 N2 today (2-plane software)", r, "N2 images unqualified", "-")
    r = evaluate("8PSK", "5/6", "psk2plane_n2", partition="pipe0", fec="meas", sys_hz=SYS150)
    row("ii", "8PSK 5/6 N2 today at SYS150", r, "150 MHz, fractional-free N2 grid", "-")
    for mod, en in (("16APSK", "apsk16_sw_n2"), ("32APSK", "apsk32_sw_n2")):
        r = evaluate(mod, "5/6", en, partition="pipe1", fec="meas")
        row("ii", f"{mod} 5/6 N2 today (split), SYS128", r, "-", "-")
        r = evaluate(mod, "5/6", en, partition="pipe1", fec="meas", sys_hz=SYS150)
        row("ii", f"{mod} 5/6 N2 today (split), SYS150", r, "-", "-")
        r = evaluate(mod, "5/6", en, partition="bal", fec="bchT")
        row("ii", f"{mod} 5/6 N2 PL-job balance, SYS128", r,
            "two FE slots + job ownership (next-experiments.md #1)", "next-experiments.md #1")
        r = evaluate(mod, "5/6", en, partition="bal", fec="bchT", sys_hz=SYS150)
        row("ii", f"{mod} 5/6 N2 PL-job balance, SYS150", r, "as above", "as above")
    return out, base


def apsk_mapper_sensitivity():
    """Rs and BB vs. APSK PL-mapper cost (cycles/symbol), PL-job balance, 5/6, SYS128, N2."""
    out = []
    for mod, en in (("16APSK", "apsk16_sw_n2"), ("32APSK", "apsk32_sw_n2")):
        s = frame_symbols(mod)
        orig = MAP_APSK[mod]
        for cmap in (orig / s, 12.0, 8.0, 4.0, 2.2):
            MAP_APSK[mod] = cmap * s
            r = evaluate(mod, "5/6", en, partition="bal", fec="bchT")
            out.append(dict(mod=mod, map_cyc_per_sym=cmap, rs=r["rs_grid"], bb=r["bb_mbps"], binding=r["binding_rs"],
                            rs_limit=r["rs_limit"]))
        MAP_APSK[mod] = orig
    return out


def cycle_budget_view():
    """Two-core cycles per on-air symbol needed vs available at each grid step d (SYS-independent):
    available = 2 cores x u x SYS/Rs = 2u x 2N d."""
    out = []
    for mod, en, en_hw in (("QPSK", "qpsk_desc_n4", "qpsk_ptr_n4"), ("8PSK", "psk2plane_n2", "hw4_L7R1_n4_2ch"),
                           ("16APSK", "apsk16_sw_n2", None), ("32APSK", "apsk32_sw_n2", None)):
        for rate in ("5/6", "9/10"):
            if rate not in MODCODS[mod]:
                continue
            for fec in ("meas", "bchT"):
                c = stage_costs(mod, rate, fec, "GCS")
                fecsum = sum(c.values())
                out.append(dict(mod=mod, rate=rate, fec=fec, fec_cyc=fecsum, map_cyc=c["map"],
                                sw_shape=ENGINES[en]["c_shape"], sw_shape_engine=en,
                                hw_shape=0.0 if en_hw else None,
                                total_sw=fecsum + ENGINES[en]["c_shape"] + ENGINES[en]["phi"] / 16e6,
                                total_hw=fecsum + (ENGINES[en_hw]["phi"] / 16e6 if en_hw else float("nan"))))
    avail = {f"N{n} d{d}": 2 * U_DEMO * 2 * n * d for n in (2, 4) for d in (1, 2, 3, 4)}
    return out, avail


def fec_caps():
    """BB capacity limited by FEC+map+ingress alone on two cores (fp), per modulation and rate, SYS128, u_demo."""
    out = []
    for mod, rates in MODCODS.items():
        for rate in rates:
            for fec in ("meas", "bchT"):
                c = stage_costs(mod, rate, fec, "GCS")
                rs = 2 * U_DEMO * SYS128 / sum(c.values())
                out.append(dict(mod=mod, rate=rate, fec=fec, c_sym=sum(c.values()),
                                c_per_bb_bit=sum(c.values()) * frame_symbols(mod) / dfl(rate),
                                rs_cap=rs / 1e6, bb_cap=bb_rate(mod, rate, rs) / 1e6))
    return out


# ---------------------------------------------------------------------------
# 13. Report
# ---------------------------------------------------------------------------
def jsonable(o):
    if isinstance(o, dict):
        return {(k if isinstance(k, (str, int, float, bool)) or k is None else "|".join(map(str, k))): jsonable(v)
                for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return str(o)
    return o


def fmt(x, n=2):
    if isinstance(x, float):
        if math.isinf(x):
            return "inf"
        return f"{x:.{n}f}"
    return str(x)


def main():
    val = validate()
    print("== CPU model validation (predicted vs measured work fraction, points) ==")
    for v in val:
        print(f"  {v['label']:52s} C0 {100 * v['pred_c0']:5.1f} vs {100 * v['meas_c0']:5.1f}  "
              f"C1 {100 * v['pred_c1']:5.1f} vs {100 * v['meas_c1']:5.1f}")
    pts = scenarios()
    print("\n== Frontier, SYS128, GCS, on-chip 32 MHz SPI, u = %.4f ==" % U_DEMO)
    hdr = ("scen", "mod", "rate", "N", "engine", "part", "a_fec", "b_shape", "ab_cpu", "c_dma", "c_hand", "e_afe",
           "f_spi32", "f_spi25", "tblKiB", "Rs", "BB", "BBspi32", "BBspi25", "maxload", "binding")
    print(" ".join(f"{h:>8s}" for h in hdr))
    for r in pts:
        if r["rate"] not in ("2/3", "5/6", "9/10") or r["engine"] == "(none)":
            continue
        vals = [r["scenario"][:8], r["mod"], r["rate"], r["N"], r["engine"][:8], r["partition"],
                r["rs_a_fec"], r["rs_b_shape"], r["rs_ab_cpu"], r["rs_c_dma_bw"], r["rs_c_dma_handoff"],
                r["rs_e_afe"], r["rs_f_spi"], r["rs_f_spi_ext25"], r["table_kib"], r["rs_grid"], r["bb_mbps"],
                r.get("bb_onchip_mbps", 0), r.get("bb_ext25_mbps", 0), max(r["load_c0"], r["load_c1"]), r["binding"]]
        print(" ".join(f"{fmt(v, 1):>8s}" for v in vals))
    lv, base = levers()
    print("\n== Levers ==")
    for x in lv:
        print(f"  [{x['group']}] {x['lever'][:80]:80s} {x['bb_mbps']:6.2f} Mb/s ({x['gain_pct']:+6.1f}%) "
              f"spi32 {x['bb_onchip']:5.2f} spi25 {x['bb_ext25']:5.2f} Rs {x['rs']:.3f} bind {x['binding']} "
              f"loads {x['load']} | {x['flags']}")
    hw = hw_apsk_tables()
    print("\n== Per-axis levels ==")
    for k, v in hw["levels"].items():
        print(f"  {k:40s} {v}")
    print("\n== Full-history table bytes (R=1) ==")
    for s in hw["full_history"]:
        if s["R"] == 1 and s["L"] in (6, 7, 8):
            print(f"  {s['alphabet']:28s} N{s['N']} L{s['L']}: dense {s['dense_bytes'] / 1024:12.0f} KiB, "
                  f"PIO-keyed {s['pio_bytes'] / 1024:14.0f} KiB")
    print("\n== Partial (split-span) tables ==")
    for p in hw["partial"]:
        print(f"  {p['scheme']:30s} {p['bytes'] / 1024:8.1f} KiB, {p['adds_per_output_word']} adds/word")
    sens = apsk_mapper_sensitivity()
    print("\n== APSK mapper sensitivity (fp, 5/6, SYS128 N2) ==")
    for s in sens:
        print(f"  {s['mod']} map {s['map_cyc_per_sym']:5.1f} c/sym -> Rs {s['rs']:.3f} ({s['rs_limit']:.2f} cont) "
              f"BB {s['bb']:.2f} [{s['binding']}]")
    fc = fec_caps()
    print("\n== FEC+map+ingress-only caps (fp, SYS128, u_demo) ==")
    for f in fc:
        if f["rate"] in ("2/3", "5/6", "9/10"):
            print(f"  {f['mod']:7s} {f['rate']:5s} {f['fec']:5s} {f['c_sym']:6.2f} c/sym {f['c_per_bb_bit']:5.2f} c/bit "
                  f"-> Rs {f['rs_cap']:6.2f} BB {f['bb_cap']:6.2f} Mb/s")
    cb, avail = cycle_budget_view()
    print("\n== Two-core cycles per on-air symbol: needed vs available (u_demo) ==")
    print("  available:", {k: round(v, 1) for k, v in avail.items()},
          "(16 Msym/s at SYS128 = N4 d1 = N2 d2)")
    for x in cb:
        print(f"  {x['mod']:7s} {x['rate']:5s} {x['fec']:5s} FEC+map+ingress {x['fec_cyc']:6.2f} (map {x['map_cyc']:5.2f}) "
              f"+ SW shape {x['sw_shape']:6.2f} = {x['total_sw']:6.2f}; with HW shaping {fmt(x['total_hw'], 2)}")
    print("\nSPI caps (Mb/s):", {k: round(v / 1e6, 2) for k, v in SPI_CAPS.items()})
    print("SRAM table budgets (KiB):", {s: round(sram_table_budget(s) / 1024, 1)
                                        for s in ("lab_image", "flight", "flight_fp")})

    # files
    (HERE / "frontier_inputs.json").write_text(json.dumps(jsonable(INPUTS), indent=1, default=str))
    keys = sorted({k for r in pts for k in r if k != "stages"})
    with open(HERE / "frontier_points.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in pts:
            w.writerow({k: r.get(k) for k in keys})
    summary = dict(validation=val, levers=lv, hw_apsk=hw, apsk_mapper_sensitivity=sens, fec_caps=fc,
                   cycle_budget=cb, cycle_budget_available=avail,
                   spi_caps_bps=SPI_CAPS,
                   sram_budget_bytes={s: sram_table_budget(s) for s in ("lab_image", "flight", "flight_fp")},
                   tool=dict(python=sys.version.split()[0], numpy=np.__version__))
    (HERE / "frontier_summary.json").write_text(json.dumps(jsonable(summary), indent=1, default=str))


if __name__ == "__main__":
    main()
