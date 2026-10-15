# RP2350B DVB-S2 feasibility frontier (digital baseband)

2026-10-15. Read-only analysis of `integration/yapogee-2026-10-15` at `8014ed3`. Nothing in
the repository, firmware or the Pico was touched. The model is `frontier.py` (Python 3.13,
NumPy 2.2.5). Every input is registered with its value, unit, kind (spec, measured, derived,
assumed) and source in `frontier_inputs.json`. `frontier_points.csv` holds every evaluated
point with all bounds. `frontier_tables.md` holds the full generated tables, and
`frontier.png` the summary plot. `pulse_check.py` (SciPy LP) screens filter spans. This is a
digital-throughput model: it says nothing about spectrum, EVM, AFE timing or link closure.

## Bottom line

1. **QPSK at N4 is capped by the AFE, not the MCU.**
   - At 16 Msym/s, N4 uses 64 of 65 MSPS per axis and 19.2 of 20 MHz.
   - The only capacity left at N4 is code rate: QPSK 9/10 gives 27.94 Mb/s (+8.1%) for
     +1.24 dB Es/N0 (EN 302 307-1 Table 13).
   - SYS = 130 MHz runs the DAC at exactly 65.0 MSPS (no rate margin) and adds 1.6%, for
     **28.37 Mb/s**. That is the hard ceiling for QPSK/N4 on this AFE.
2. **For a real external payload, ingress binds first.**
   - The CM5 is proven only at 25 MHz SCK (`results/cm5-spi`). Its 1332-byte message plus
     10 µs gap carries at most **24.13 Mb/s**, below today's 25.84 Mb/s.
   - 5/6 needs ≥ 26.8 MHz on one lane; 9/10 needs ≥ 29.0 MHz. A shorter CS gap does not
     close the gap: 24.70 Mb/s at 25 MHz even with no gap.
   - The on-chip 32 MHz source caps at 30.87 Mb/s (`three-sps.md`).
3. **At 16 Msym/s on SYS128, each core has 8 cycles per on-air symbol, whatever N is.**
   With the 96.3% demonstrated load ceiling, two cores give 15.4 cycles/symbol in total.
   Measured per-symbol work (`T-cyc`, 5/6):

   | Modulation | Encoder chain (ingress+BCH+LDPC+map), cycles/symbol | Shaping, cycles/symbol | Result |
   |---|---:|---:|---|
   | QPSK | 10.2 | ~0.3 with hardware shaping | Fits |
   | 8PSK | 14.2 | ~0.3 with hardware shaping | Fits **only** with hardware shaping (91% load) |
   | 16APSK | 33.2 | 20.8 (software) | 54 total: 3.5× over budget |
   | 32APSK | 51.7 | 33.9 (software) | 86 total: 5.6× over budget |

   - APSK's PL mapper alone, 17.3 and 31.8 cycles/symbol, exceeds the whole per-core budget.
   - So 16/32APSK top out near 4.0–4.6 / 2.7–2.9 Msym/s at SYS128: **9–16 Mb/s, below
     QPSK at 16 Msym/s**. On this chip, higher-order APSK buys narrower bandwidth, not
     capacity.
4. **Hardware-only (no-CPU-summation) APSK shaping does not fit, at any span that passes
   the mask.**
   - The DMA/PIO datapath has no adder, so a table must be indexed by the full axis history.
   - The LP screen shows **no linear-phase pulse of span L ≤ 6 can pass Annex A** at N2 or
     N4, even with zero margin. This is a grid relaxation, so the infeasibility is rigorous.
   - At L = 7, 16APSK (8 levels/axis) needs 8–16 MiB and 32APSK (17 levels) needs 1.5–3 GiB.
     Total SRAM is 520 KiB.
5. **The one higher-order path toward the AFE limit is 8PSK at 16 Msym/s, N4, on B1's
   4-level L7 table.**
   - One DMA chain per axis is hand-off-limited to 8 Msym/s: R1 rows at 16 Msym/s stalled in
     every record. 8 Msym/s gives 19.4 Mb/s, *below* QPSK-16.
   - A second chain per axis reaches 16 Msym/s on paper, at 31.0 Mb/s (2/3) to 41.9 Mb/s
     (9/10).
   - The cost: 77% of DMA slots (57% demonstrated), 11 of 12 SMs, and 17 DMA channels with
     today's credit scheme (16 available). Frame-parallel FEC must hold 81–93% load.
   - Deliverable data is ingress-capped at 30.9 / 24.1 Mb/s, so a multi-lane ingress is
     also required.
6. **The integer clock grid matters as much as any optimisation.**
   - At SYS128, N4 offers only 16 and 8 Msym/s, so any bound slightly below 16 halves the
     rate.
   - For compute-bound points the divider is SYS-independent: d ≥ c_total / (4·u·N).
     Raising SYS scales those points linearly, but leaves them on the same grid step.

## Model and validation

- **Encoder costs** are isolated per-frame stage cycles from the `dvbs2` and `bench8map`
  records (git `5e38990269`, `b29d1a7ec2`):
  - BB+BCH and LDPC, with and without the packer, for 2/3, 3/4, 5/6, 9/10, plus 3/5 and 8/9.
  - 4/5 is interpolated. Ingress is charged per BB bit.
- **Live calibration** comes from one run (QPSK 5/6, 16 Msym/s, packer + scratch-Y BCH):
  - BCH ×1.056 (×1.147 without the scratch-Y bank); LDPC+map ×1.021.
  - Ingress 0.61 cycles/bit (GCS) or 1.18 cycles/bit (TS).
  - Supervision: 12.1% of a core (descriptor engine) or 3.38% (PIO pointers).
- **Shaping costs** are the live per-symbol figures of each engine. N4 versions of software
  engines scale by 1.81 per doubling of N, from `lut_asm` 11.55 → 20.9 (derived).
- **Utilisation ceiling:** u = 0.9628, the highest 60 s pass. Failures occur at ≥ 99.2%.
  Loads above 0.90 are flagged; 0.90 is the assumed planning reserve.
- **Validation:** predicted and measured per-core loads for six independent runs (TS input,
  pack0/bank0, 8PSK at 128/150 MHz, 16/32APSK split) agree within **1.3 points** (`T-val`).
  The failed 16APSK no-split control is predicted at 104% on core 1, against 99.8% measured
  and underrun.

## Constraint inputs

| Constraint | Value | Kind and source |
|---|---|---|
| AFE sample rate | ≤ 65 MSPS per axis | spec, SLOS789C p5 (`afe7071.txt:285`) |
| AFE RF bandwidth | ≤ 20 MHz → Rs ≤ 16.67 Msym/s at α 0.20 | spec, SLOS789C p1 |
| Clock grid | Rs = SYS/(2·N·d), d integer (DDR clock and writers) | `docs/dac-clock.md` |
| DMA | 1 transfer/cycle in total; 57% demonstrated (PIO pointers, 16 Msym/s) | RP2350 §12.6 p1094; `quad-pointers` |
| DMA row hand-off | 16 cycles/row/chain passes; 8 cycles/row (R1 at 16 Msym/s) always stalled | `dac-clock` layout coverage; `dual-dma/2026-10-14` |
| SRAM for tables | 69 KiB (lab image); 181 KiB (single-profile flight image, est.); 134 KiB with a second FEC context | ELF symbol sizes, `iqbench.elf` at `8014ed3`; classes assumed |
| PV-SPI | 1316 of 1332 B per message, 10 µs gap; 24.13 Mb/s at 25 MHz; 30.87 Mb/s on-chip at 32 MHz | `pv-spi-spec.md`; `cm5-spi`; `three-sps.md` |

## Frontier (SYS128, GCS input, u = 0.9628; Rs Msym/s / BB Mb/s / binding)

Scenarios are cumulative:

- **S0:** measured engines and gains.
- **S1:** adds A1 (PIO pointers + frame-parallel FEC).
- **S2:** adds B1 (one chain per axis).
- **S3:** adds C1, modelled only as specialised t = 8/12 BCH kernels.
- **S4:** adds two DMA chains per axis, the N2 4-level table and PL-job balance.

`*` = not measured. The full set of rates and the per-constraint bounds for every point are
in `frontier_tables.md` (`T-front`, `T-bounds`).

| MODCOD | N | S0 today | S1 +A1 | S2 +B1 | S3 +C1 | S4 beyond |
|---|---|---|---|---|---|---|
| QPSK 5/6 | N4 | 16 / 25.8 / AFE | 16 / 25.8 / AFE | same | same | same |
| QPSK 9/10 | N4 | 8 / 14.0 / CPU | 16 / 27.9 / AFE | same | same | same |
| QPSK 5/6 | N2 | 8 / 12.9 / CPU | 10.67 / 17.2 / CPU | same | same | 16 / 25.8 / AFE* |
| 8PSK 2/3 | N4 | no engine | no engine | 8 / 15.5 / DMA hand-off* | same | 16 / 31.0 / ingress* |
| 8PSK 5/6 | N4 | no engine | no engine | 8 / 19.4 / DMA hand-off* | same | 16 / 38.8 / ingress* |
| 8PSK 5/6 | N2 | 8 / 19.4 / CPU | 8 / 19.4 / CPU | same | same | 16 / 38.8 / ingress* |
| 8PSK 9/10 | N4 | no engine | no engine | 8 / 20.9 / DMA hand-off* | same | 16 / 41.9 / ingress* |
| 16APSK 5/6 | N2 | 4 / 12.9 / CPU | same | same | same | 4 / 12.9 / CPU (bal: 4.56 continuous) |
| 16APSK 9/10 | N2 | 3.2 / 11.1 / CPU | same | same | 4 / 13.9 / CPU | 4.57 / 15.9 / CPU |
| 32APSK 5/6 | N2 | 2.67 / 10.7 / CPU | same | same | same | same |
| 16/32APSK any | N4 | no engine | | | | SRAM: 272 / 230 KiB tables |

**Per-constraint view (S4, 5/6):**

- **QPSK N4:** FEC 24.2, joint CPU 23.8, DMA 28.1 / hand-off 32, AFE 16.25 → AFE binds, at
  65% load per core.
- **8PSK N4 (two chains):** FEC 17.3, joint CPU 17.0, DMA 20.9 / hand-off 16.0, AFE 16.25.
  The grid gives 16 Msym/s at 91% load; ingress binds for data.
- **16APSK N2:** FEC 7.4, shaping 11.9 (two cores), joint 4.6 → CPU.

FEC-only caps (`T-fec`) for comparison, with *all* of both cores spent on
ingress+FEC+mapping and shaping assumed free:

| MODCOD | FEC-only cap, Mb/s |
|---|---:|
| QPSK 5/6 | 39 |
| 8PSK 5/6 | 42 |
| 8PSK 9/10, with t=8 BCH | 49 |
| 16APSK 5/6 | 24 |
| 32APSK 5/6 | 19 |

So even free shaping would not lift APSK above QPSK-16.

## Hardware-only shaping for 16APSK/32APSK

**Per-axis levels** (`axis_levels()`, ETSI §5.4.3–5.4.4 geometry, repo outer-radius-one
normalisation, unit-radius π/2-BPSK headers and pilots, Gold quarter turns):

| Constellation | Levels per axis | Detail |
|---|---:|---|
| QPSK | 2 | |
| 8PSK native | 5 | |
| 8PSK/QPSK/headers, π/8-rotated | 4 | |
| 16APSK | 8 | ±sin15°, ±1/√2, ±cos15°, ±1/(γ√2), for every code rate; 10 with E = 1 normalisation, because unit headers no longer coincide |
| 32APSK | 17 | Reproduces `apsk-frontier` |

Quarter turns only swap or negate axes, so I and Q share one alphabet.

**Full-history table bytes** = M^(L+R−1) · R · N · 2. A PIO-built address needs
2^(⌈log2 M⌉·(L+R−1)) rows. The formula reproduces the measured tables:

- QPSK L8 R4 N4: 64 KiB.
- 16APSK 3+4+4 chunks: 139,264 B.
- 32APSK 3×17³ rows: 117,912 B.

Minimum span (`pulse_check.py`; the LP reproduces all five pulse-design outcomes in the
repo):

| Span | N4 | N2 |
|---|---|---|
| L ≤ 6 | Infeasible in every model | Infeasible in every model |
| L7 | Feasible with DAC-hold co-design: ISI 1.29% (0 margin), 1.50% (0.1 dB / 2 dB far reserve) | Hold-only feasible: ISI 1.35% / 1.55% |

| Alphabet | L7, N2 | L7, N4 | L8, N2 | Verdict |
|---|---:|---:|---:|---|
| 4-level | 64 KiB | 128 KiB | 256 KiB | L7 fits |
| 8-level (16APSK) | 8 MiB | 16 MiB | 64 MiB | Does not fit |
| 17-level (32APSK) | 1.5 GiB (dense; PIO keys 128 GiB) | — | — | Does not fit |

**Split-span (partial) tables** fit easily:

| Scheme | Table size |
|---|---:|
| 16APSK L7 R1, 4+3 chunks, N2 | 18 KiB |
| 16APSK L7 R1, 4+3 chunks, N4 | 36 KiB |
| 32APSK L7 R1, 3+2+2 chunks, N2 | 21 KiB |

But every output word then needs S−1 additions: 1–2 per word, at N words per symbol.

The hardware has no adder that can serve this:

- PIO has no ADD.
- DMA moves data and cannot combine it.
- The interpolators are in the SIO, which the DMA cannot reach (RP2350 p596, §12.6.7 p1104).
- The only DMA arithmetic is the single sniffer's SUM mode (p1135):
  - It observes one channel.
  - PV-SPI already uses it for message CRC-32.
  - It needs ≥ 2S+2 transfers per summed word. That bounds Rs to 4–11 Msym/s on transfer
    count alone, before chain latency (Cornell measured ~8 M chained blocks/s, strategy
    report).

Flash or PSRAM could hold an 8 MiB L7 16APSK table. But random 4-byte row reads over QSPI
(est. ~3 M rows/s, not measured) give about 1.5 Msym/s, below today's software engine.

**Verdict:** no hardware-only 16/32APSK scheme fits SRAM at any mask-compliant L. APSK
shaping stays a CPU job at ~20 cycles/symbol (N2).

## Ranked levers

### (i) Yapogee QPSK, N4

Ceiling: 28.37 Mb/s (9/10 at 16.25 Msym/s). Today: 25.84 Mb/s.

1. **Ingress ≥ 26.8 MHz, or two lanes, from the real payload.**
   - Gain: turns 24.1 into 25.8 Mb/s (+7%) today, and is required for 9/10 (29.0 MHz).
   - Risk: the async PIO receiver is derived good to ~20–25 MHz (`rp2350-notes.md`).
     `link.c` already has an N-lane `in pins, lanes` receiver; a 2-lane link at 13–15 MHz
     suffices.
   - Whether the CM5/RP1 can drive dual-lane SPI is unverified.
   - Smallest experiment: the native CM5 sender back-to-back at 25 MHz, which should
     measure ≤ 24.1 Mb/s; then a 2-lane PIO receiver at 15 MHz from the on-chip emulator.
2. **QPSK 9/10: +8.1%, 27.94 Mb/s, +1.24 dB Es/N0.**
   - Today 9/10 is CPU-bound at 8 Msym/s. There is no sliced t=8 frontend, so core 0
     would need ~103% at 16 Msym/s (descriptors + supervision + whole-frame BCH). Moving
     the BCH to core 1 needs 145%.
   - Two cheap routes:
     - (a) A1's pointer engine, plus the existing generic BCH kernel *sliced* into
       ≤ 256 µs chunks on core 0. Loads 69% / 79%; frame-parallel is not needed. Slicing
       (or interrupt-driven supervision) is required: a 1.37 ms whole-frame BCH call breaks
       the pointer credit supervision limit of 256 µs (`pointer-polling-proof.md`).
     - (b) A faster sliced t=8 BCH kernel on today's descriptor engine: 78% / 79% (derived
       kernel speed).
   - Smallest experiment: A1's live pointer path at code index 10 with a 128-word-sliced
     generic BCH, before its frame-parallel step.
3. **A1 (pointers + frame-parallel FEC): 0% capacity, large margin.**
   - GCS loads drop from 82/82% to 65/65%; TS from 82/94% to 71/71%.
   - It enables TS at 16 Msym/s, 9/10 and VCM headroom. It costs about 48 KiB SRAM for a
     second FEC context (assumed); the model leaves 134 KiB for tables.
4. **SYS130 (16.25 Msym/s): +1.6%.**
   - The AFE runs at its 65 MSPS maximum with no margin, and the DDR period shrinks to
     7.69 ns. Low priority.
5. **B1 and C1:** no QPSK capacity gain (AFE-bound). C1's t=8 kernel cuts 9/10 encoder work
   by 18% (11.6 → 9.5 cycles/symbol).

### (ii) Higher order

1. **Second DMA chain per axis on B1's 4-level L7 table → 8PSK at 16 Msym/s, N4.**
   - Capacity: 2/3 gives 30.97 Mb/s (+20%, +1.44 dB vs QPSK 5/6; +0.2 dB vs QPSK 9/10 for
     +11%). 9/10 gives 41.89 Mb/s.
   - Load with frame-parallel FEC: 81% (2/3) to 93% (3/4).
   - Risks: DMA at 77%; 17 channels unless the per-SM credit channels are shared or dropped
     (13 without them); 11 SMs, which leaves no room for a second SPI lane's SMs.
   - Smallest experiment: any 128 KiB table, R1 rows, 4 writers, two control/sample chains
     per axis from precomputed pointer rings, PRBS, 16 Msym/s, 60 s; then repeat under FEC
     load. If it stalls, 8PSK on N4 stays at 8 Msym/s, which is below QPSK-16.
2. **Multi-lane ingress.** Necessary for any point above 30.9 Mb/s on-chip or 24.1 Mb/s
   external: 8PSK 5/6 at 16 Msym/s needs 40.7 MHz on one lane, or 20.0 MHz on two.
3. **APSK PL mapper (C1 "planar mapper").** The largest APSK lever.
   - 16APSK 5/6: 4.0 → 5.33 Msym/s (12.9 → 17.2 Mb/s) once the mapper reaches ≤ 8
     cycles/symbol from 17.3.
   - 32APSK 5/6: 2.67 → 4.0 Msym/s (10.7 → 16.1 Mb/s) at ≤ 4 cycles/symbol.
   - Gate: a standalone mapper with identical CRC at or below those cycle counts. A smaller
     gain does not move a SYS128 grid step.
4. **SYS150 for N2 APSK/8PSK: +17%, measured** (16APSK 15.1, 32APSK 12.6, 8PSK 22.7 Mb/s).
   It sits outside Yapogee's SYS128 plan, and the N2 images are unqualified.
5. **PL-job balance** (`apsk-fec/next-experiments.md` #1). +3.6% in continuous rate;
   16APSK 5/6 lands 0.2% short of the next SYS128 step (5.35 vs 5.357 at 150 MHz), which
   is inside model error. Worth doing only together with lever 3.
6. **B1 as specified:** 8 Msym/s, hand-off-bound, so no capacity over 8PSK N2 today.
   - Value: 8PSK/VCM moves onto the N4 image plan, and about 76% of core 0 is freed from
     the 2-plane shaper.
   - At SYS150 it would give 9.375 Msym/s (22.7 Mb/s) at the same 16-cycle row period.

**Rejected:**

- Hardware APSK tables: SRAM, above.
- Sniffer adder: shared, needed for the CRC, and too slow.
- Software N4 APSK: 230–272 KiB of tables exceeds the 181 KiB budget, and it would be
  ≤ 3.3 Msym/s in any case.
- APSK at 16 Msym/s: 3.5–5.6× over the cycle budget.

## Facts, estimates and unknowns

- **Measured:** stage cycles, live loads, engine shaping costs, failing hand-off layouts,
  25 MHz external SPI, and 60 s APSK/8PSK/QPSK rates.
- **Derived:** contention factors, ingress cost per bit, 4/5 and 8/9-pack1 costs, the N4
  shaping scale, table sizes, level counts, SPI caps, and every projected Rs.
- **Assumed:**
  - SRAM classes for a flight image and the second FEC context size.
  - t = 8/12 BCH kernel speed (scaled by remainder words).
  - 4-level pointer program at ≤ 4 PIO cycles/symbol.
  - Ideal balance in `fp`/`bal`.
  - u = 0.90 planning reserve.
- **Unknown:**
  - DMA behaviour at 77% with FEC traffic.
  - Whether an L7 4-level pulse meets ISI needs on real hardware.
  - External SPI above 25 MHz.
  - N2 image levels.
  - AFE operation at 64–65 MSPS.
  - Every RF quantity.
  - Model error is about ±1.5 load points against measurements; points within ~2% of a
    grid step are calls the model cannot make.
