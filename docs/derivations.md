# Derivations

Inputs, formulas and results behind the benchmark design. "Measured" values come from
`results/optimization-log.jsonl` (git revision in each record); everything else is analysis.

## 1. Rates

QPSK carries 2 coded bits per symbol. With symbol rate R_s, samples per symbol N and the AFE7071's
interleaved bus (one I word and one Q word per complex sample, SLOS789C p.5):

| quantity | formula | R_s = 8 Msym/s, N = 2 | N = 4 |
|---|---|---|---|
| coded input | 2 R_s | 16 Mb/s = 2 MB/s | same |
| DAC sample rate f_s | N R_s | 16 MS/s | 32 MS/s |
| bus word rate | 2 f_s | 32 MW/s | 64 MW/s |
| buffer traffic (16-bit slots) | 4 f_s bytes/s | 64 MB/s | 128 MB/s |
| AFE limits | f_s ≤ 65 MS/s, words ≤ 130 MW/s | ok | ok |

In dual-input clock mode DACCLK = 2 f_s, CLK_IO = 2 f_s, and the two must be frequency-locked (p.25).

## 2. Why N = 4, not 2

The AFE7071 has no digital interpolation and no access to baseband between its DAC and modulator.
The only reconstruction filtering is the zero-order-hold sinc and the integrated 4th-order filter.
Signal half-width B = R_s(1+α)/2 = 4.8 MHz (α = 0.20), and the first image's inner edge sits at
f_s − B.

- Filter setting: tune 8 attenuates 18 dB at 5 MHz (p.6), which is inside the signal. Only tune 0
  (1 dB at 10 MHz, 18 dB at 20 MHz) passes it.
- N = 2: image edge at 11.2 MHz. ZOH gives 20 log|sinc(11.2/16)| = −8.7 dB; tune 0 gives about
  −3.8 dB (log-f interpolation between the typical datasheet points). Modelled worst image
  −18.7 dBc, ACLR −25 dB, independent of filter length (`results/plots/filter_sweep.png`).
- N = 4: image edge at 27.2 MHz. Modelled worst image −49 dBc; ACLR is then set by pulse-shaper
  truncation: −39.6 dB at L = 10 and −43.0 dB at L = 12 (rectangular window).
- An RF filter cannot remove an image 11 MHz from a 1.28 or 2.2 GHz carrier. The fractional
  bandwidth needed is below 1 %.

Model limits: the filter curve is typical, interpolated between four datasheet points, and ignores
modulator nonlinearity, LO leakage and sideband error. Measure the spectrum on hardware.

## 3. LUT pulse shaper

For symbol n, phase p and span L symbols: y[nN + p] = Σ_{a=0}^{L−1} s[n−a] g[aN + p], with
s = ±1. The sum depends only on the L most recent bits h of that axis, so T[h][p] can be
precomputed. Memory per axis is 2^L · N · 2 bytes (L = 10, N = 4: 8 KiB; I and Q tables are separate
because the I table carries IQ_FLAG in bit 14).

Scaling: the exact worst case over all histories is max_p Σ_a |g[aN+p]|. It is scaled to
(2^13 − 1)·10^(−1/20), so no input sequence can clip.

Exactness: integer coefficients c = round(g·2^8). With floor rounding after the sum, the error
against float convolution is at most 0.5 + L·2^−9 LSB. Measured: 0.497–0.507 LSB (bound
0.512–0.523). Firmware, host-native C and Python produce identical CRCs for all 6 variants and all
kernels.

EVM is not the binding constraint. With EVM ε and channel SNR ρ, the effective SNR is
1/(1/ρ + ε²). At Es/N0 = 3 dB (roughly QPSK 2/3 threshold) and ε = −40 dB the loss is
10 log(1 + 2·10^−4) = 0.001 dB. The spectrum (ACLR, mask) sets L.

## 4. CPU budget and kernel cost

Per-core budget B = f_clk / R_s cycles per symbol: 16 at 128 MHz, 18.75 at 150 MHz.
128 MHz is chosen because 128/(2·N·8) is an integer (4 cycles/word at N = 2, 2 at N = 4).
At 150 MHz the PIO would need a fractional divider (about ±1 clk = 6.7 ns edge jitter).

Instruction model for `lut_asm` at N = 4, per symbol:

| op | count | cycles (M33, RP2350 §3.7.4.9) |
|---|---|---|
| UBFX (index I, Q) | 2 | 2 |
| LDR register offset (I01, Q01, I23, Q23) | 4 | 4 |
| STRD post-increment (2 words each, 32-bit bus) | 2 | about 4 |
| load-use stall before the last STRD | – | about 1 |
| per input word (16 symbols): load, 2 packs, loop | – | about 25/16 ≈ 1.6 |
| **predicted** | | **≈ 11.5** |
| **measured** (128 MHz, L = 10) | | **11.55** |

Floor for this algorithm on a 32-bit bus: 8 words moved per symbol plus 2 extracts ≈ 10 cycles.

Moving the I/Q interleave into the PIO removed 4 pack instructions per symbol. The PIO stashes
I(2k+1) in X while emitting Q(2k) (8 instructions per 4 bus words, 2 cycles per word).

## 5. Streaming (measured, 128 MHz, 8.000 Msym/s, N = 4, L = 10)

| configuration | core 0 busy | core 1 busy | underruns | pin capture |
|---|---|---|---|---|
| core 0 only | 75.3 % | – | 0 | 8 176 words exact |
| cores 0+1 alternating blocks | 38.1 % | 38.2 % | 0 | exact |
| core 1 only (core 0 free) | – | 75.9 % | 0 | exact |
| core 0 only, 20 s, L = 12 | 73.5 % | – | 0 in 156 249 blocks | 32 752 words exact |

The extra ~3.5 % over the kernel-only 72 % is the DMA ISR and bus contention. Alternating blocks
needs no shared filter state: block k reads input word k·64 − 1 for history, which is already in
memory.

Block period: 2 · 1024 · N · cpw = 16 384 cycles (128 µs). Ring: 8 blocks, 128 KiB.

## 6. Estimates not yet measured

- Pi/payload input at 16 Mb/s: RP1 PIO 4 lanes × 10 MHz (primary), SPI 20 MHz (fallback);
  see `host-link-and-devboard-research.md`.
- DVB-S2 FEC on the RP2350: see `sats-self-contained.md`.
