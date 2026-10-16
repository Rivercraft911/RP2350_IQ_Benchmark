# Yapogee: RP2350B DVB-S2 waveform benchmark

Can an RP2350B replace the FPGA that turns framed, coded bits into pulse-shaped I/Q samples
for a TI AFE7071? This repository is the **benchmark and groundwork**: it measures how far one
RP2350B at its rated 128 MHz can go, with every positive result checked against an independent
reference. Flight software will be ported from the best engines here later.

Tests use a Pimoroni Pico Plus 2 (RP2350B rev A2), stock voltage, USB only. Input comes from an
on-chip SPI master unless stated. **Everything here is digital**: AFE pad timing, spectrum, EVM
and receiver decoding are not measured yet.

## Best measured option per transmitter type

| Type | Best approach | Rate, BB capacity | CPU (core 0 / core 1) | Evidence |
|---|---|---|---|---|
| **IREC video**: QPSK at 8 Msym/s, TS over SPI | QPSK hardware shaping (`dualptr`): PIO computes LUT addresses, DMA fetches rows | 8 Msym/s; 12.92 Mb/s at 5/6 (IREC plans 2/3: 10.33 Mb/s) | 25.8% / 46.4% (5/6, TS) | 60 s clean, GCS and TS ([dualptr](results/dualptr/2026-10-15/README.md)) |
| **QPSK, maximum rate** | same engine, DMA read bus priority (mask 13) | 16 Msym/s; 27.94 Mb/s at 9/10, 25.84 at 5/6 | 45.6% / 82.8% (9/10 GCS); TS 97% core 1 | 60 s clean ([round 2](results/round2/2026-10-15/README.md), [B2](results/b2-8psk-live/2026-10-15/README.md), [C2](results/c2-qpsk-910/2026-10-15/README.md)) |
| **8PSK, complete on-chip transmitter** | live FEC into the 8-bit four-level table (`bench4fec ... 8`) | 8 Msym/s; 19.38 Mb/s at 5/6 in 9.6 MHz | 24–25% / 87–88% | 6/6 60-s runs clean, bus priority 13 and 0 ([B7](results/b7-live8/2026-10-15/README.md)) |
| **8PSK at the DAC limit** (waveform only) | four PIO address producers, four DMA chains, 8-bit rows (`bench4prod8live`) | 16 Msym/s; 38.76 Mb/s at 5/6 if FEC and 47 Mb/s of coded input came from outside | 19% source + 3.5% / reserved | 6/6 60-s runs clean with runtime data ([B6](results/b6-pio-producers/2026-10-15/README.md)) |
| QPSK/8PSK frame switching (VCM) | CPU-shaped two-plane engine, 2 SPS | 9.375 Msym/s at 150 MHz; 18.17 Mb/s mean | — | 60 s ([VCM](results/waveforms/2026-10-15/README.md)) |
| 16APSK / 32APSK | complete SPI/FEC, CPU shaping, 2 SPS | 4.167 / 2.679 Msym/s at 150 MHz; 13.42 / 10.80 Mb/s | encoder 96% / 94% of core 1 | 60 s ([APSK](results/apsk-fec/2026-10-15/README.md)); below QPSK at 16 Msym/s on this chip |
| Autonomous flight image (`pvflight`) | older CPU-shaped QPSK 2/3 path, starts without USB | 8 Msym/s; 10.33 Mb/s | see [autonomous startup](docs/autonomous-startup.md) | to be moved onto the `dualptr` engine |

Capacity includes PL header and pilot time and subtracts the BBHEADER; application throughput
is lower. Nominal width is 1.2 × symbol rate at α = 0.20, calculated, not measured. Pigeon Vision
needs a 9 Mb/s MPEG-TS (9.81 Mb/s application budget at QPSK 2/3, 8 Msym/s). The CM5 SPI link is
proven at 25 MHz, about 24 Mb/s ([cm5-spi](results/cm5-spi/)).

**What limits each type.** QPSK is capped by the AFE (65 MSPS per axis = 16.25 Msym/s at 4
samples/symbol) and by SPI ingress. 8PSK at 16 Msym/s is capped by on-chip FEC: the best
two-core encoder needs 364k cycles per 2/3 frame against a 355k budget, before supervision
([C4](results/c4-fec-speed/2026-10-15/README.md), [SRAM banks](results/ldpc-banks/2026-10-15/README.md)).
APSK is capped by CPU shaping. The [frontier model](docs/frontier/frontier.md) derives these bounds.

## How the hardware shaping engine works

Pulse shaping is a filter over the last 7 symbols. Each axis only takes a few levels (2 for QPSK,
4 for 8PSK after a π/8 rotation), so every possible output is precomputed into a table at
startup: 16,384 rows of four 8-bit samples (64 KiB) for the four-level table. At run time:

1. A PIO state machine shifts each 2-bit symbol code into a rolling 14-bit history and emits a
   table row address (7 instructions per symbol).
2. A control DMA channel copies that address into a sample DMA channel's start register; the
   sample channel copies the row into a PIO writer FIFO, then chains back.
3. PIO writers put one sample per 128 MHz clock on GPIO6–13 with IQ_FLAG on GPIO14; another state
   machine toggles GPIO28 as the AFE's DDR clock (I on the rising edge, Q on the falling edge).
   One register write starts writers and clock on the same cycle.

No CPU touches a sample. The binding limits are the shared fast-peripheral bus (one access per
cycle; a PIO-sourced address costs two) and the DMA row loop, measured at 10–18 cycles
([dmaloop](results/dmaloop/2026-10-15/README.md)): 16 Msym/s needs two chains per axis so each
chain has 16 cycles per row. The QPSK engine uses the same idea with four-symbol rows.

## Build and reproduce

Requires Pico SDK 2.2.0, GCC Arm 14.2, CMake, Ninja, picotool, Python, NumPy, Matplotlib and
pyserial (`PICO_SDK_PATH`, `PICO_TOOLCHAIN_PATH` if not in the VS Code extension's locations).

```sh
make check        # native and model validation, no USB
make build        # firmware/build/{iqbench,pvflight}.uf2 (stable commands)
# Experiment commands (four-level, 8-bit, B6, C3/C4, dmaloop, c3pair, r2grant):
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
```

Runners never build or flash; flash the matching image, use a fresh `--out`, and pin the firmware
with `--expected-git`. One canonical run per type:

```sh
# IREC-class QPSK, hardware shaping, 8 Msym/s (cpw 2), SPI TS and GCS
python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 2 --sources 5 6 --faults 0 \
  --clock-modes 1 --control --build firmware/build-exp --out results/<run>
# QPSK 9/10 at 16 Msym/s, hardware shaping, DMA read priority
python3 host/dualptr_trial.py --ms 60000 --cpws 1 --priority 13 --code-index 10 ...
# Complete on-chip 8PSK transmitter, 8 Msym/s, 8-bit rows
python3 host/iq4fec_trial.py --bits 8 --ms 60000 --cpws 2 --inverts 0 --prios 13 \
  --build firmware/build-exp --out results/<run>
# 8PSK at 16 Msym/s, live runtime data through PIO producers
python3 host/iq4prod_trial.py --live --ms 60000 --cpws 1 --priorities 13 --inverts 0 \
  --build firmware/build-exp --out results/<run>
# Two-core FEC cost per frame, no output (code 5 = 8PSK 2/3)
#   on the device: c3fec 5 3000 0 2 4096 27      c3pair 5 40 0 2 27
```

Each results README lists the exact commands, image hashes and negative controls used.

## Where things are

| Need | Look in |
|---|---|
| Every results folder, grouped by type | [results/README.md](results/README.md) |
| R&D rounds 1–3: what each experiment tried and found | [docs/rd/README.md](docs/rd/README.md) |
| Bounds and strategy | [frontier model](docs/frontier/frontier.md), [strategy report](docs/frontier/strategy-report.md) |
| Payload protocol, DAC clock, startup | [PV-SPI v1](docs/pv-spi-spec.md), [DAC clock](docs/dac-clock.md), [autonomous startup](docs/autonomous-startup.md) |
| Earlier front page and history | [README of 2026-10-15](docs/history/readme-2026-10-15.md), [benchmark history](docs/benchmark-history.md) |

| Area | Firmware (`firmware/src/`) | Host |
|---|---|---|
| Command dispatch, default transmitter | `main.c`, `tx.c`, `iqout.c` | `iqbench.py` |
| DMA/PIO output, DDR clock, capture | `dual_dma.c` | `dual_trial.py`, `test_dacclk.py` |
| QPSK hardware shaping | `iqquadptr.c`, `bench_dual.c` (`dualptr`) | `dualptr_trial.py` |
| Four-level table, 8-bit rows, producers | `iq4hist.c`, `iq4ptr.c`, `iq4quad.c`, `iq4prod*.c`, `bench4fec.c`, `bench4live.c`, `bench4quad.c`, `bench4prod.c` | `iq4fec_trial.py`, `iq4hist_trial.py`, `iq4quad_trial.py`, `iq4prod_trial.py` |
| DVB-S2 FEC and mapping | `dvbs2.c`, `dvbs2_asm.S`, `dvbs2_ldpc_asm.S`, `dvbs2_8psk.c`, `c3_fec.c` | `c3_fec_trial.py`, `reference/` |
| SPI ingress | `pvspi.c`, `pvproto.c` | `cm5/` (external sender) |
| CPU-shaped QPSK/8PSK/VCM/APSK engines | `bench8live.c`, `dvbs2_qpsk8.c`, `iq8quad.c`, `bench_apsk_*.c`, `dvbs2_apsk.c` | `iq8live_trial.py`, `vcm_trial.py`, `apsk_*_trial.py` |
| Diagnostics | `dmaloop.c`, `bench_c3_pair.c`, `bench_r2grant.c`, `bench_c3_fec.c` | `dmaloop_sweep.py`, `r2_grant_trial.py` |

## Branches

`main` holds everything. Every former experiment branch head is kept as a tag
`archive/<branch>` (for example `archive/rd/b6-pio-producers`), so each result's recorded
firmware commit stays reachable. Two experiments were never merged and live only in their tags:
`archive/rd/b3-8psk-rows` (four-writer router for live 8PSK, superseded by B5/B6) and
`archive/rd/c1-fec-throughput` (scheduled LDPC, slower live).

The transmitter hardware is designed in [Yapogee](https://github.com/Rivercraft911/yapogee).
