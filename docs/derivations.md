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

## 2. Samples per symbol at the AFE7071

**What N means here.** N is the DAC's own rate divided by the symbol rate. Through the E200 the
host also sends "2 samples/symbol", but its AD9363 interpolates digitally before its DAC (TX FIR
and half-band stages; ADI UG-570, not re-checked here), so its DAC runs far faster. The AFE7071
has no interpolation: the bus rate *is* the DAC rate, f_s = N R_s.

**Why that matters.** A DAC's output repeats the baseband spectrum at every multiple of f_s
(images). The zero-order hold weights them by |sinc(f/f_s)|. Between the DAC and the modulator
there is only the integrated 4th-order low-pass. Signal half-width B = R_s(1+α)/2 = 4.8 MHz, and
the first image starts at f_s − B:

| N | f_s | first image | frequency ratio image/edge | ideal 4th-order at edge corner |
|---|---|---|---|---|
| 2 | 16 MS/s | 11.2 MHz | 2.33 | 80·log10(2.33) ≈ 29 dB |
| 4 | 32 MS/s | 27.2 MHz | 5.67 | 80·log10(5.67) ≈ 60 dB |

The filter must pass 4.8 MHz and reject 11.2 MHz. Tune 0 (−1 dB at 10 MHz) is too wide.
Tune 8 (−18 dB at 5 MHz) cuts into the signal. Tune 4, plotted in SLOS789C Figure 36 but not
tabulated, sits between them.

**Modelled** (`reference/analyze_sps.py`): ZOH, AFE filter magnitude (typical curves, tune 4 read
from Figure 36 at ±1 dB), then an ideal RRC receiver.

| option | bus | worst image | ACLR | EVM |
|---|---|---|---|---|
| N = 2, tune 0 | 32 MW/s | −19 dBc | −25 dB | −30 dB |
| N = 2, tune 4 + digital pre-EQ | 32 MW/s | −45 dBc | −35 dB | −30 dB |
| same, real corner ×0.9 / ×1.1 | | −49 / −42 dBc | −37 / −34 dB | −24 / −37 dB |
| N = 4, tune 0 (current) | 64 MW/s | −49 dBc | −40 dB | −39 dB |

Pre-EQ divides the RRC target by the modelled ZOH and filter response over the signal band. It is
folded into the LUT, so it costs nothing at run time. EVM near −30 dB costs < 0.01 dB at the
QPSK 2/3 threshold, so it is not the constraint.

**Conclusion.** N = 2 is feasible with tune 4 and pre-EQ, but only against a filter response that
exists as one typical plot. A ±10 % corner error moves the images by ±4 dB, and part-to-part and
temperature spread are unknown. N = 4 has margin without relying on the filter shape, and it
fits the RP2350 (67.8 % of a core measured). Keep N = 4 as the baseline. Treat N = 2 + tune 4 +
EQ as a halved-bus fallback to measure on real AFE7071 parts (gate G6).

The emission limit that sets "enough" (IREC / FCC Part 97 at 1.28 GHz) is not yet written into
the project requirements.

## 3. LUT pulse shaper

For symbol n, phase p and span L symbols: y[nN + p] = Σ_{a=0}^{L−1} s[n−a] g[aN + p], with
s = ±1. The sum depends only on the L most recent bits h of that axis, so T[h][p] can be
precomputed. Memory per axis is 2^L · N · 2 bytes (L = 10, N = 4: 8 KiB; I and Q tables are separate
because the I table carries IQ_FLAG in bit 14).

Window: a Kaiser window (β = 1) on the truncated RRC. With it, L = 12 passes the EN 302 307-1
Annex A mask at every point, with the far sidelobes 7.6 dB under the −40 dB line; L = 10 without a
window fails by 1.3 dB at 6.8 MHz, and β = 2–3 at L = 10 droops below the limit at 0.89 f_N
(`reference/analyze.py`, `etsi_mask` in `results/reference/filter_sweep.json`). `pvtx` uses L = 12.

Scaling: the exact worst case over all histories is max_p Σ_a |g[aN+p]|. It is scaled to
(2^13 − 1)·10^(−1/20), so no input sequence can clip.

Exactness: integer coefficients c = round(g·2^8). The sum is rounded to nearest (add 2^(F−1),
then shift right), so the error against float convolution is at most 0.5 + L·2^−9 LSB. Measured:
0.497–0.507 LSB (bound 0.512–0.523). Firmware, host-native C and Python produce identical CRCs
for all 6 variants and all kernels.

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

Kernel `lut_asm_p` (v5), 20 s per row, firmware f51d833. Each row: 156 249 blocks, 0 underruns,
28 656 captured bus words exact.

| configuration | core 0 busy | core 1 busy |
|---|---|---|
| core 0 only | 67.7 % | – |
| cores 0+1 alternating blocks | 34.7 % | 35.4 % |
| core 1 only (core 0 free) | – | 69.7 % |
| core 0 only, L = 12 | 68.3 % | – |

The kernel alone needs 10.80 / 16 = 67.5 %, so the DMA ISR and bus contention add about 0.2 %.
Alternating blocks needs no shared filter state: block k reads input word k·64 − 1 for history,
which is already in memory.

Block period: 2 · 1024 · N · cpw = 16 384 cycles (128 µs). Ring: 8 blocks, 128 KiB.

## 6. Host link and encoder

- Host link: PV-SPI at 20 MHz, measured clean from a real CM5 with camera TS
  (`host-link.md`, `../results/cm5-spi/README.md`).
- DVB-S2 FEC on the RP2350: estimated 0.17–0.2 M cycles/frame, measured 353 k (normal 2/3). See
  `sats-self-contained.md` §2 and the README optimization table.
