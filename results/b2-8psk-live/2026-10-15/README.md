# Live hardware shaping: 8PSK on the four-level table, and DMA read priority

**Results.** (1) Live SPI/GCS 8PSK 5/6 FEC through the four-level L7/N4 table (`bench4fec`)
ran 60 s clean at **8 Msym/s, N4, 32 MHz GPIO28 DDR clock**, with no CPU sample arithmetic:
core 0 24.9–25.7% (conversion 21.6–22.4% plus 3.3% supervision), core 1 encoder 88.8–89.2%.
(2) Both live pointer paths stalled their writers within milliseconds until DMA reads were
given bus priority over core 1. With `dualprio`/`prio` 12 or 13, A1's two-writer QPSK
`dualptr` also ran 60 s clean at **16 Msym/s** (GCS and TS), core 0 47.7% versus about 81%
for the CPU-descriptor path. The priority costs core 1 about 6 points, which breaks the
core-1-bound TS descriptor control at 16 Msym/s.

Pico Plus 2, RP2350B A2, USB only, on-chip PV-SPI master at 32 MHz, SYS128. Branch
`rd/b2-8psk-live`. 60 s runs use image `2081cb740b` (`flash-60s.txt`, `iqbench-60s.uf2`
sha256 in each runner manifest); earlier probes `eea74941b9` (`quick-1/`) and
`eea74941b9-dirty` (`prio-sweep-1/`, same sources plus the priority option).

## 8PSK on the four-level table (`host/iq4fec_trial.py`)

Every run checks 4096 sparse GPIO0..31 snapshots against independent GCS BBFRAMEs, scalar
BCH/LDPC, rotated 8PSK PLFRAMEs and the frozen L7 FIR, plus GPIO28 against IQ_FLAG.

| Run | Rate | Mask | Duration | Bus / clock | Core 0 | Core 1 | Result |
|---|---:|---:|---:|---|---:|---:|---|
| quick-1 | 4 Msym/s | 0 | 3 s | 0 / 4096 of 4096 | 16.2% | 43.8% | clean |
| quick-1, inverted | 4 Msym/s | 0 | 3 s | 0 / 0 of 4096 | 16.2% | 43.8% | detected |
| quick-1 | 8 Msym/s | 0 | 6.5 ms | — | — | — | writer TX stall |
| prio-sweep-1 | 8 Msym/s | 0, 1, 9, 15 | 1–13 ms | — | — | — | writer TX stall |
| prio-sweep-1 | 8 Msym/s | 12, 13 | 3 s | 0 / 4096 of 4096 | 25.8%, 24.9% | 89.1%, 89.6% | clean |
| b2-8msym-60s | 8 Msym/s | 13 | 60 s | 0 / 4096 of 4096 | 24.9% | 89.2% | clean |
| b2-8msym-60s, inverted | 8 Msym/s | 13 | 60 s | 0 / 0 of 4096 | 24.9% | 89.2% | detected |
| b2-8msym-60s | 8 Msym/s | 12 | 60 s | 0 / 4096 of 4096 | 25.7% | 88.8% | clean |
| b2-8msym-60s, inverted | 8 Msym/s | 12 | 60 s | 0 / 0 of 4096 | 25.7% | 88.8% | detected |

Mask bits are the BUS_PRIORITY register fields PROC0 (1), PROC1 (2), DMA_R (4), DMA_W (8);
RP2350-E27 changes which ports some of them affect. Runs pass only with DMA_R set and PROC1
clear. Conversion measured 3.45–3.59 cycles/symbol from flash, inside B1's 2.8–3.75
estimate. Minimum credit runway was 1073 words; maximum supervision gap 162 µs.

## QPSK `dualptr` at 16 Msym/s with priority 13 (`host/dualptr_trial.py --priority`)

| Run | Duration | Core 0 | of which BCH | Core 1 | Result |
|---|---:|---:|---:|---:|---|
| CPU-descriptor control, GCS | 60 s | 81.4% | 44.3% | 87.0% | clean |
| `dualptr`, GCS | 60 s | 47.7% | 44.5% | 85.7% | clean, 0 bus / 0 clock mismatches |
| `dualptr`, GCS, inverted clock | 60 s | 47.7% | 44.5% | 85.7% | detected |
| CPU-descriptor control, TS | 0.26 s | 80.5% | 43.6% | 99.9% | underrun (core 1 bound) |
| `dualptr`, TS | 60 s | 47.8% | 44.5% | 98.9% | clean, on the edge |
| `dualptr`, TS, inverted clock | 60 s | 47.8% | 44.5% | 98.9% | detected |

`a1-dualptr16-prio1{2,3}*/` hold the 3 s probes: with mask 0 `dualptr` reported 0 because it
forced the mask, which this branch fixes. The TS descriptor control passed 60 s at 93.5%
core 1 with mask 0 (results/integration/2026-10-15); with mask 13 it underruns. Choose the
mask per profile until core-1 headroom grows (frame-parallel FEC, faster TS ingress).

Not established: pad timing, AFE capture, spectrum/EVM, receiver decode, temperature,
long soaks. Sparse snapshots are 4096 per run, not every word.
