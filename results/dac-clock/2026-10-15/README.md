# GPIO28 DDR DAC clock: digital qualification

**Result:** the continuous PIO DDR clock on GPIO28 started with the writers and stayed in
I/Q parity for the complete SPI/FEC QPSK 5/6 pipeline at 8 and 16 Msym/s (32 and 64 MHz
DACCLK), 60 s each, with no change in measured CPU load. Every captured snapshot had the
clock high in exactly the I words, and the inverted-start control failed every snapshot.
How the clock works and what it does not establish: [docs/dac-clock.md](../../../docs/dac-clock.md).

Setup: Pimoroni Pico Plus 2, RP2350B A2, USB only, external GPIOs unconnected, stock
voltage, SYS = 128 MHz. Input comes from the on-chip SPI master. Firmware
`34195cedfc-dirty` = HEAD `34195ce` plus the uncommitted DDR clock change; the exact
sources are in `gcs-ddr-soak-1/firmware-source.tar.gz` and their hashes in its
`manifest.json`. UF2 sha256 `933141bb…7bf4`, flashed and verified (`flash.txt`).

## Campaign `gcs-ddr-soak-1`

```sh
python3 host/dual_trial.py gcs --clocks 128000 --ms 60000 --writers 4 --row-symbols 4 \
  --quad-profile 1 --code-index 8 --map-kernel 1 --dac-clock ddr \
  --expected-git 34195cedfc-dirty --build firmware/build --out results/dac-clock/2026-10-15/gcs-ddr-soak-1
python3 results/dac-clock/2026-10-15/summarize.py results/dac-clock/2026-10-15/gcs-ddr-soak-1
```

QPSK 5/6 + pilots, L8 four-symbol pulse, four PIO2 writers, clock on PIO0 SM2.
Each capture: 4096 GPIO0..31 snapshots, 17 SYS cycles apart, checked against the
independent reference (bus) and for GPIO28 == IQ_FLAG (clock).

| Source | Symbol rate | DACCLK | Duration | Bus mismatches | Clock parity | Core 0 active | Core 1 work | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| SPI TS | 8 Msym/s | 32 MHz | 60.0 s | 0 / 4096 | 4096 / 4096 | 49.7% | 49.3% | clean |
| SPI GCS | 8 Msym/s | 32 MHz | 60.0 s | 0 / 4096 | 4096 / 4096 | 49.8% | 43.6% | clean |
| SPI GCS | 16 Msym/s | 64 MHz | 60.0 s | 0 / 4096 | 4096 / 4096 | 86.2% | 88.2% | clean |
| SPI TS | 16 Msym/s | 64 MHz | 36.9 s | 0 / 4096 | 4096 / 4096 | 86.0% | 99.9% | underrun (ceiling probe) |

The 16 Msym/s GCS loads match the earlier HSTX-clock run of the same configuration
(86.0% / 88.2%, `results/pulse-design/2026-10-15/pico-matched-soak-1`). The clock uses
no CPU or DMA. "Core" figures are the runner's instrumented work and active-loop
fractions, not measured idle or power.

Negative controls, all detected:

| Control | Expected detection | Observed |
|---|---|---|
| Wrong valid Q row (fault 1) | data reference fails | no matching offset; clock parity still 4096/4096 |
| Publication stopped (fault 2) | ownership/underrun | 2 underruns, run halted |
| Clock started on the low instruction (`dualclock 2`) | every snapshot fails parity | 4096/4096 parity failures, data 0 mismatches |

The parity check therefore distinguishes a one-slot clock offset from correct data,
independently of the bus check.

## 16 Msym/s TS underrun is an existing core-1 ceiling

TS input at 16 Msym/s 5/6 saturates core 1 and underruns. Earlier 3 s probes of this
configuration already ran core 1 at 99.0–99.7% (`results/qpsk-codes/2026-10-15`); no
60 s TS pass at this rate was ever recorded. The qualified 16 Msym/s result uses GCS
input. `ts-ceiling-ab/` repeats the 60 s TS run (`driver.py`, `runs.jsonl`, flash
records, `manifest.json` with both UF2 hashes):

| Firmware | DAC clock | Underrun after | Core 1 work |
|---|---|---:|---:|
| this change | HSTX (old default) | 39.7 s, 38.3 s | 99.94% |
| this change | GPIO28 DDR | 37.1 s (and 36.9 s in the campaign) | 99.93% |
| clean HEAD `34195ce` | HSTX | 2.0 s, 2.3 s | 99.88% |

Every condition fails, including firmware without the DDR code, so the clock is not
the cause. Time to underrun is roughly the initial queue lead divided by a small
per-frame deficit, so it is very sensitive to small cycle differences. The two builds
differ in RAM code placement: the core-1 encoder functions (`dvbs2_*`, BCH, LDPC) moved
by −64 bytes, which keeps each word in the same SRAM stripe, while the core-0 poll
functions `sample_reads`/`sample_faults` moved by +116 bytes, which does not. That
sensitivity is an explanation, not a measured cause. Closing TS at 16 Msym/s needs
core-1 work removed, not a different clock.

## Layout coverage

`layout-coverage/` runs 3 s cyclic-PRBS streams on every writer layout with the clock,
plus the inverted control. Two writers put the clock SM on PIO2, four writers on PIO0.

| Writers / rows | PIO programs | 8 Msym/s | 16 Msym/s | Inverted control |
|---|---|---|---|---|
| 2 / 1 symbol | `out_code` | 0 bus, 0 clock mismatches | TX stall before capture | detected (4096/4096) |
| 2 / 4 symbols | `paired_i/q_code` | clean | clean | detected |
| 4 / 2 symbols | `four_i/q_code` | clean | clean | detected |
| 4 / 4 symbols | `quad_four_i/q_code` | clean | clean | detected |

Two writers with one-symbol rows at divider 1 has stalled in every earlier record
(`results/dual-dma`, all kernels and schedules, both clocks): a DMA handoff ceiling,
not a clock effect.

After the campaign, one cleanup line changed in `dual_dma.c` (stop releases the clock SM
only if this engine claimed it). `final-source-check/` repeats the layout coverage with
that final source (UF2 sha256 `ca576f3a…8790`, archived sources and flash record): the
same pattern, every layout clean and every inverted control detected.

## What is not established

No SN65, AFE or RF hardware was connected. The capture resolves clock/data alignment to
one SYS cycle at the RP2350 pins (input synchronizers bypassed), not pad skew, edge rate,
duty cycle or jitter. Snapshots are sparse: 4096 per run, not every word. One device.
