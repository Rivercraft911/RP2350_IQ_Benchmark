# Derivations

The short math behind the design. Measured values cite their results folder; everything else is
analysis. The earlier CPU-shaper kernel model is in [benchmark history](benchmark-history.md) and
in git history.

## 1. Rates

The AFE7071 takes one I and one Q word per complex sample, and has no interpolator, so the DAC
rate is f_s = N R_s ≤ 65 MS/s and the bus carries 2 f_s words/s (SLOS789C p.5). At 128 MHz one
bus word per SYS cycle gives f_s = 64 MS/s:

| Samples/symbol N | Symbol rate at f_s = 64 MS/s | SYS cycles per symbol |
|---:|---:|---:|
| 4 | 16 Msym/s | 8 |
| 5 | 12.8 Msym/s | 10 |

Payload capacity is (Kbch − 80) R_s / S, with Kbch from EN 302 307-1 Table 5a, the 80-bit
BBHEADER removed, and S the PLFRAME length with pilots: 33,282 symbols (QPSK), 22,194 (8PSK).
8PSK 2/3 at 16 Msym/s: 42,960 × 16e6 / 22,194 = 30.97 Mb/s.

## 2. Samples per symbol at the AFE7071

A DAC repeats the signal around every multiple of f_s, weighted by the hold's sinc. The only
filter before the modulator is the AFE's 4th-order low-pass. With signal half-width
B = R_s(1 + α)/2, the first image starts at f_s − B. Modelled (`reference/analyze_sps.py`:
hold, AFE filter curves from SLOS789C, ideal RRC receiver) at 8 Msym/s:

| Option | Worst image | ACLR | EVM |
|---|---:|---:|---:|
| N = 2, filter tune 0 | −19 dBc | −25 dB | −30 dB |
| N = 2, tune 4 + digital pre-EQ | −45 dBc | −35 dB | −30 dB |
| same, filter corner ±10 % | −49 / −42 dBc | | |
| **N = 4, tune 0** | **−49 dBc** | **−40 dB** | **−39 dB** |

N = 2 works only against a filter shape known from one typical plot, and moves ±4 dB with a
±10 % corner error. N = 4 has margin without relying on the filter, so it is the baseline.

## 3. Table pulse shaping

Each output sample is y[nN + p] = Σ_a s[n − a] g[aN + p] over the last L symbols. It depends only
on those L symbol levels per axis, so every output is precomputed into a table indexed by that
history. QPSK has 2 levels per axis; 8PSK has 4 after a global π/8 rotation, so an L = 7 table has
4⁷ = 16,384 rows. With 8-bit samples a row of 4 samples is one 32-bit word (64 KiB); 5 samples
need two words (128 KiB).

- No clipping: the table is scaled so the worst case over all histories, max_p Σ_a |g[aN + p]|,
  stays under full scale.
- Exact: the sum is rounded once, so every sample is within 0.5 LSB of the real-valued filter, and
  firmware, native C and Python produce the same CRCs.
- Spectrum: each pulse is designed against the EN 302 307-1 Annex A mask with the DAC hold
  included. The 5-SPS pulse passes by only +0.027 dB ([n5 model](rd/n5-model.json)).
- EVM is not the limit: with EVM ε and SNR ρ the effective SNR is 1/(1/ρ + ε²); ε = −40 dB at
  Es/N0 = 3 dB costs 0.001 dB.

## 4. Cycle budgets at 128 MHz

- **DMA row loop.** Fetching one row is a chain of dependent bus operations measured at 10 to 18
  cycles ([dmaloop](../results/dmaloop/2026-10-15/README.md)). At 16 Msym/s a chain has 8 cycles
  per row with one chain per axis, 16 with two, so 16 Msym/s needs two chains per axis.
- **Peripheral port.** PIO, DMA control and USB share one access per cycle. A PIO-sourced
  pointer costs 2 accesses, an SRAM pointer 1, each sample word 1. 14-bit one-symbol rows with PIO
  pointers need 8.125 accesses per 8 cycles, which is impossible; 8-bit rows fit.
- **FEC.** The two cores take alternate frames, so each has 2 × 22,194 × 8 = 355,104 cycles per
  8PSK frame at 16 Msym/s (443,880 at 12.8 Msym/s), including supervision.
- **LDPC floor.** Normal 2/3 has 480 parity-table entries; each XORs a rotated 360-bit vector,
  12 words. At 3 to 5 instructions per word that is 17k to 29k cycles, against 58k measured on
  one core for that step ([c8](../results/c8-fec-combined/2026-10-16/README.md)). The floor ignores
  memory stalls and the bit transposes around it, so it bounds the gap, not what is reachable.
