# Yapogee: RP2350B satellite downlink experiments

Can an RP2350 through the power of PIO replace the FPGA that generates a DVB-S2 QPSK waveform for a TI AFE7071 transmitter?

**Digital result: Yes, with margin!!!** Yapogee builds a complete DVB-S2 digital waveform on a
standalone RP2350B: BB framing and scrambling, BCH, LDPC, constellation mapping, PL headers,
pilots, PL scrambling and pulse shaping. DMA and PIO stream the I/Q samples over the parallel
interface proposed for the TI AFE7071, with the AFE's DDR clock generated on GPIO28. The payload
sends bytes over SPI; it does not generate the waveform or perform FEC.

QPSK now runs at **16 Msym/s, the AFE7071's limit at 4 samples/symbol**, with the pulse shaping
done entirely by PIO and DMA: no CPU touches a sample. The best measured capacity is
**27.94 Mb/s with QPSK 9/10**. A complete **8PSK** transmitter runs at 8 Msym/s on the same
hardware shaping, and the 8PSK waveform alone reaches 16 Msym/s. **16APSK and 32APSK** run as
complete SPI/FEC pipelines too, slower but working. Everything uses a 128 MHz system clock
(the RP2350 is rated to 150 MHz; 128 MHz gives integer dividers for the DAC clock) unless a row
says 150 MHz.

Tests use a Pimoroni Pico Plus 2, RP2350B rev A2, stock voltage, USB only and unconnected
external GPIOs. SPI traffic comes from an on-chip master unless stated; the external CM5 camera
test is recorded separately. This repository is the benchmark and the groundwork: flight
firmware will be ported from the best engines here.

## Measured modes

Complete digital pipelines: SPI source, BBFRAME, BCH, LDPC, mapping, PLFRAME, shaping, pins.
Every row passed 60 seconds with every captured sample checked against an independent reference.

| Pipeline | Shaping | Clock | Symbol rate | BB data capacity | Width at α = 0.20 |
|---|---|---:|---:|---:|---:|
| QPSK 9/10 + pilots, 4 SPS | PIO/DMA | 128 MHz | 16 Msym/s | **27.94 Mb/s** | 19.2 MHz |
| QPSK 5/6 + pilots, 4 SPS | PIO/DMA | 128 MHz | 16 Msym/s | **25.84 Mb/s** | 19.2 MHz |
| QPSK 5/6 + pilots, 4 SPS | PIO/DMA | 128 MHz | 8 Msym/s | 12.92 Mb/s | 9.6 MHz |
| QPSK 2/3 + pilots, 4 SPS (Pigeon Vision profile, `pvflight`) | CPU | 128 MHz | 8 Msym/s | 10.33 Mb/s | 9.6 MHz |
| 8PSK 5/6 + pilots, 4 SPS, 8-bit table | PIO/DMA | 128 MHz | 8 Msym/s | **19.38 Mb/s** | 9.6 MHz |
| 8PSK 5/6 + pilots, 2 SPS | CPU | 150 MHz | 9.375 Msym/s | **22.71 Mb/s** | 11.25 MHz |
| Alternating QPSK/8PSK 5/6 (VCM), 2 SPS | CPU | 150 MHz | 9.375 Msym/s | 18.17 Mb/s mean | 11.25 MHz |
| 16APSK 5/6 + pilots, 2 SPS | CPU | 128 / 150 MHz | 3.556 / 4.167 Msym/s | 11.46 / **13.42 Mb/s** | 4.27 / 5.00 MHz |
| 32APSK 5/6 + pilots, 2 SPS | CPU | 128 / 150 MHz | 2.286 / 2.679 Msym/s | 9.21 / **10.80 Mb/s** | 2.74 / 3.21 MHz |

Waveform only: **8PSK at 16 Msym/s, 4 SPS**, live runtime symbols through PIO/DMA shaping,
passed 6 of 6 60-second runs. It would carry 38.76 Mb/s at 5/6, but the FEC and the 47 Mb/s of
coded input would have to come from outside the chip.

![BB data capacity against nominal bandwidth for every measured mode](results/plots/summary-2026-10-16/capacity.png)

Filled markers are 128 MHz, open markers 150 MHz. Capacity includes PL header and pilot time
and subtracts the BBHEADER; application throughput is lower. Width is 1.2 × symbol rate,
calculated, not measured. Pigeon Vision needs a 9 Mb/s MPEG-TS, which fits QPSK 2/3 at 8 Msym/s.

## Why 4 samples per symbol

The shaper sends the DAC 4 I/Q samples for every symbol, so at 8 Msym/s the AFE7071's DAC runs
at 32 MS/s (64 M bus words/s, I and Q interleaved), and at 16 Msym/s it runs at 64 MS/s, just
under its 65 MSPS limit. A DAC's output also carries copies of the signal, called images, around
every multiple of its sample rate. The AFE7071 has no interpolator, only a gentle on-chip
low-pass, so the sample rate decides how far out those copies land and how much of them the
filter removes:
- 4 samples/symbol: first image at 27 MHz, modelled at −49 dBc with the widest filter setting.
- 2 samples/symbol: first image at 11 MHz, −19 dBc. It reaches −45 dBc only with the tune-4
  filter and a matching pre-equaliser, and moves ±4 dB for a ±10 % filter-corner error.

The E200 can take "2 samples/symbol" because its AD9363 interpolates to a much higher DAC rate
internally. Details are in [docs/derivations.md §2](docs/derivations.md#2-samples-per-symbol-at-the-afe7071).

![Modelled output spectra at 2 and 4 samples per symbol](results/plots/why_4_samples_per_symbol.png)

## How the hardware shaping works

Pulse shaping is a filter over the last 7 symbols. Each axis only ever takes a few levels: 2 for
QPSK, and 4 for 8PSK after a π/8 rotation. So every possible output is computed once at startup
into a table: 16,384 rows of four 8-bit samples (64 KiB) for 8PSK. At run time:

1. A PIO state machine shifts each symbol's 2-bit code into a rolling 14-bit history and emits
   the table row address (7 instructions per symbol).
2. A control DMA channel copies that address into a sample DMA channel's start register. The
   sample channel copies the row into a PIO writer FIFO, then chains back to the control channel.
3. PIO writers put one sample per 128 MHz clock on GPIO6–13, with IQ_FLAG on GPIO14. Another state
   machine toggles GPIO28 as the AFE's DDR clock: I on the rising edge, Q on the falling edge.
   One register write starts the writers and the clock on the same cycle.

The QPSK engine works the same way with four-symbol rows. The CPU-shaped engines (2 SPS 8PSK,
VCM, APSK) instead sum precomputed table contributions on core 0 and stream the blocks.

One row fetch is a chain of dependent bus operations, measured at 10 to 18 cycles depending on
the row size and how many chains share the bus. A chain must finish each row before its writer
needs it, so 16 Msym/s needs two chains per axis. This one measurement explains every stall we
saw:

![DMA row-loop time against each design's per-row deadline](results/plots/summary-2026-10-16/dma_loop.png)

## Where the CPU goes

With shaping in hardware, the remaining CPU work is the input, BB framing, BCH, LDPC and mapping.

![Measured work per core for complete pipelines](results/plots/summary-2026-10-16/cpu.png)

The FEC is what stops 8PSK at 16 Msym/s from running fully on-chip. Each core gets 355k cycles
per frame; one 8PSK 2/3 frame costs 348k on one core, and more when both cores encode at once and
contend for SRAM. Keeping the BCH code out of the single-bank scratch memory removed most of that
contention:

![Two-core FEC cycles per 8PSK 2/3 frame](results/plots/summary-2026-10-16/fec_cycles.png)

## Build and reproduce

Requires Pico SDK 2.2.0, GCC Arm 14.2, CMake, Ninja, picotool, Python, NumPy, Matplotlib and
pyserial. Set `PICO_SDK_PATH` and `PICO_TOOLCHAIN_PATH` if they are not in the Pico VS Code
extension's default locations.

```sh
make check        # native and model validation, no USB
make build        # firmware/build/{iqbench,pvflight}.uf2
# Experiment commands (four-level table, 8-bit rows, PIO producers, FEC and DMA diagnostics):
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
python3 host/plot_summary.py   # regenerates the figures above from results/
```

The runners never build or flash. Flash the matching image, use a fresh `--out` and pin the
firmware with `--expected-git`:

```sh
# QPSK, hardware shaping, 8 Msym/s (cpw 2), SPI TS and GCS input
python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 2 --sources 5 6 --faults 0 \
  --clock-modes 1 --control --build firmware/build-exp --out results/<run>
# Complete 8PSK transmitter, 8 Msym/s, 8-bit table
python3 host/iq4fec_trial.py --bits 8 --ms 60000 --cpws 2 --inverts 0 --prios 13 \
  --build firmware/build-exp --out results/<run>
# 8PSK waveform at 16 Msym/s with live runtime symbols
python3 host/iq4prod_trial.py --live --ms 60000 --cpws 1 --priorities 13 --inverts 0 \
  --build firmware/build-exp --out results/<run>
# Complete 16APSK SPI/FEC path. For 32APSK use --modulations 32 --cpw 14.
python3 host/apsk_fec_trial.py --ms 60000 --modulations 16 --cpw 9 \
  --build firmware/build --out results/<run>
```

Each results README lists its exact commands, image hashes and negative controls.
`pvflight` starts the stored QPSK 2/3 profile without USB; see
[autonomous startup](docs/autonomous-startup.md) and [PV-SPI v1](docs/pv-spi-spec.md).

## Where things are

| Need | Look in |
|---|---|
| Every results folder, grouped by type | [results/README.md](results/README.md) |
| R&D rounds 1–3: what each experiment tried and found | [docs/rd/README.md](docs/rd/README.md) |
| Bounds and strategy | [frontier model](docs/frontier/frontier.md), [strategy report](docs/frontier/strategy-report.md) |
| Filter and sampling derivations, DAC clock | [derivations](docs/derivations.md), [DAC clock](docs/dac-clock.md) |
| History | [benchmark history](docs/benchmark-history.md), [front page of 2026-10-15](docs/history/readme-2026-10-15.md) |

| Area | Firmware (`firmware/src/`) | Host |
|---|---|---|
| Command dispatch, default transmitter | `main.c`, `tx.c`, `iqout.c` | `iqbench.py` |
| DMA/PIO output, DDR clock, capture | `dual_dma.c` | `dual_trial.py`, `test_dacclk.py` |
| QPSK hardware shaping | `iqquadptr.c`, `bench_dual.c` (`dualptr`) | `dualptr_trial.py` |
| Four-level table, 8-bit rows, PIO producers | `iq4hist.c`, `iq4ptr.c`, `iq4quad.c`, `iq4prod*.c`, `bench4*.c` | `iq4fec_trial.py`, `iq4hist_trial.py`, `iq4quad_trial.py`, `iq4prod_trial.py` |
| DVB-S2 FEC and mapping | `dvbs2.c`, `dvbs2_asm.S`, `dvbs2_ldpc_asm.S`, `dvbs2_8psk.c`, `c3_fec.c` | `c3_fec_trial.py`, `reference/` |
| CPU-shaped 8PSK, VCM, APSK | `bench8live.c`, `dvbs2_qpsk8.c`, `iq8quad.c`, `bench_apsk_*.c`, `dvbs2_apsk.c` | `iq8live_trial.py`, `vcm_trial.py`, `apsk_*_trial.py` |
| SPI ingress | `pvspi.c`, `pvproto.c` | `cm5/` (external sender) |
| Diagnostics | `dmaloop.c`, `bench_c3_fec.c`, `bench_c3_pair.c`, `bench_r2grant.c` | `dmaloop_sweep.py`, `r2_grant_trial.py` |

All work lives on `main`. Each former experiment branch is kept as a tag `archive/<branch>` so
every recorded firmware commit stays reachable. The transmitter hardware is designed in
[Yapogee](https://github.com/Rivercraft911/yapogee).

Of course, none of this shows RF performance: spectrum and EVM after the AFE, clock jitter, LO leakage, PA
behaviour and so much more. This is purely the waveform generation.

No real receiver has decoded this output yet.... Coming soon to a repo near you!
