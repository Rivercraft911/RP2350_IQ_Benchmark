# Results index

Every folder holds immutable device records (`runs.jsonl`, manifests, captures); flashed images
(`*.uf2`, `*.elf`) are kept locally and identified by the SHA-256 in each manifest. Folders are
grouped by transmitter type; the newest, best result of each type comes first. All runs:
Pico Plus 2, RP2350B A2, SYS128 unless noted, digital only.

## QPSK

| Folder | What it shows |
|---|---|
| [round2/2026-10-15](round2/2026-10-15/README.md) | Merged round-2 image: QPSK 9/10 at 16 Msym/s, 60 s clean on hardware pointers and CPU descriptors; core-0 stack fix ([STACK.md](round2/2026-10-15/STACK.md)) |
| [c2-qpsk-910/2026-10-15](c2-qpsk-910/2026-10-15/README.md) | QPSK 9/10 and the t=8 BCH kernel (−36% BCH); two hangs later traced to the stack overflow |
| [b2-8psk-live/2026-10-15](b2-8psk-live/2026-10-15/README.md) | DMA read bus priority makes hardware-pointer QPSK run 60 s at 16 Msym/s (and 8PSK at 8) |
| [dualptr/2026-10-15](dualptr/2026-10-15/README.md) | Live SPI/FEC with PIO-generated QPSK table addresses: 8 Msym/s clean, core 0 47% -> 26% |
| [dac-clock/2026-10-15](dac-clock/2026-10-15/README.md) | GPIO28 DDR DAC clock with atomic start: parity checked on every writer layout |
| [dual-dma/2026-10-15](dual-dma/2026-10-15/README.md), [dual-dma/2026-10-14](dual-dma/2026-10-14/README.md) | Direct LUT reads by DMA (CPU descriptors) vs the CPU sample-copy shaper; 16 Msym/s encoder headroom and PIO/DMA SPI ingress |
| [optimizations/2026-10-15](optimizations/2026-10-15/README.md), [integration/2026-10-15](integration/2026-10-15/README.md) | LDPC streaming packer and scratch-Y BCH copy (−3.2 / −6.3 points at 16 Msym/s), then combined with the DDR clock and APSK split on one image |
| [limits/2026-10-14](limits/2026-10-14/README.md) | First limits campaign on the original optimized CPU-shaped transmitter |
| `qpsk-codes/`, `quad-pointers/` | Raw run sets for the code-rate sweep and the cyclic PIO-pointer engine (see dac-clock and waveforms READMEs) |

## 8PSK and the four-level table

| Folder | What it shows |
|---|---|
| [b7-live8/2026-10-15](b7-live8/2026-10-15/README.md) | **Complete on-chip 8PSK transmitter at 8 Msym/s** with 8-bit rows: 6/6 60-s runs clean; 14-bit rows stall at priority 0 |
| [b6-pio-producers/2026-10-15](b6-pio-producers/2026-10-15/README.md) | **Live 8PSK at 16 Msym/s** through four PIO address producers: 6/6 60-s runs clean |
| [b5-quad-rows/2026-10-15](b5-quad-rows/2026-10-15/README.md) | Cyclic 8PSK at 16 Msym/s on four SRAM-fed DMA chains: 12/12 (8-bit), 5/6 (14-bit) |
| [b4-8bit-rows/2026-10-15](b4-8bit-rows/2026-10-15/README.md) | 8-bit one-word rows: clean at 8 Msym/s, one chain per axis stalls at 16 |
| [b1-four-level/2026-10-15](b1-four-level/2026-10-15/README.md) | Four-level L7/N4 full-history table, cyclic 8PSK at 8 Msym/s |
| [waveforms/2026-10-15](waveforms/2026-10-15/README.md) | CPU-shaped 8PSK (9.375 Msym/s at 150 MHz) and QPSK/8PSK VCM campaign; raw sets in `8psk-live/`, `8psk-quad/`, `vcm/`, `quad-rrc/`, `gcs-ingress/`, `pvspi-gate/`, `higher-mapping/`, `memory-placement/`, `pulse-design/`, `16apsk-chunk/` |

## On-chip FEC cost (no output path)

| Folder | What it shows |
|---|---|
| [ldpc-banks/2026-10-15](ldpc-banks/2026-10-15/README.md) | Two-core FEC and SRAM banks: scratch-bank BCH code is the big conflict; LDPC is instruction-bound |
| [c4-fec-speed/2026-10-15](c4-fec-speed/2026-10-15/README.md) | Five selectable speedups: 8PSK 2/3 frame 364k cycles (−11–14%) |
| [c3-8psk-fec16/2026-10-15](c3-8psk-fec16/2026-10-15/README.md) | Frame-parallel two-core 8PSK FEC: fits 8 Msym/s, 13–39% over at 16 |

## APSK

| Folder | What it shows |
|---|---|
| [apsk-fec/2026-10-15](apsk-fec/2026-10-15/README.md) | Complete 16APSK/32APSK SPI/FEC: 4.167 / 2.679 Msym/s at 150 MHz |
| [apsk-dual/2026-10-15](apsk-dual/2026-10-15/README.md) | Two-core APSK mapping and output components at 8–9.375 Msym/s |
| [apsk-frontier/2026-10-15](apsk-frontier/2026-10-15/README.md) | Earlier APSK kernel experiments |

## Platform diagnostics

| Folder | What it shows |
|---|---|
| [dmaloop/2026-10-15](dmaloop/2026-10-15/README.md) | DMA row-loop latency (10–18 cycles) explains every one-symbol-row stall |
| [r2-hw-offload/2026-10-15](r2-hw-offload/2026-10-15/README.md) | Self-re-arming DMA input grants, no CPU: 24/24 exact |
| `cm5-spi/` | External CM5 SPI sender tests at 25 MHz (see [PV-SPI](../docs/pv-spi-spec.md)) |
| `reference/` | Filter and SPS analysis outputs (see [derivations](../docs/derivations.md)) |
| `plots/`, `optimization-log.jsonl` | Generated figures; optimization log |
