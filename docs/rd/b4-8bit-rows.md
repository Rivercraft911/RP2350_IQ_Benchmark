# B4: 8-bit one-word L7/N4 rows

**The model gate passes, and an opt-in cyclic 16-Msym/s experiment is built. Sustained SYS128 output remains unmeasured. The existing live-FEC pipeline does not fit 16 Msym/s at any requested code rate, even with ideal frame balancing.** No attached Pico or serial port was accessed; no device measurement, flashing, clock/VREG/flash change, Git commit or evidence-file modification was made.

The branch is `rd/b4-8bit-rows`, based on `0b89b341ce`. The selected table is 64 KiB and uses four signed bytes per row. Its held-DAC upper-mask margin is +0.025890 dB, modeled EVM 1.493026% and MER 36.518655 dB. At SYS128/cpw1, the one-chain-per-axis graph uses 76.563% of FASTPERI, temporarily 82.445% during sparse capture. The seven-cycle pointer SM removes B1's independent 9.0625-cycle limit. DMA handoff/arbitration is the outstanding digital throughput gate.

## What changed

- `iq4hist8_build()` and independent host references generate every once-rounded row from B1's unchanged frozen taps. Existing 14-bit generation stays available.
- `iq4ptr_prepare8()` selects a seven-instruction, starvation-safe PIO pointer engine with a 64-KiB-aligned table. The original cyclic/live 14-bit engine retains its ten instructions and explicit PULL/PUSH behavior.
- `dual_dma` has an experiment-only 8-bit sample selector and separate OUT8 writers on GPIO6..13. GPIO0..5 and 15 are held zero; GPIO14 IQ_FLAG and the atomic GPIO28 DDR start/stop keep their existing schedule. The sample DMA transfers exactly one word per row.
- `bench4live8 cpw ms invert priority` supplies immutable cyclic 8PSK PRBS with one control/sample chain per axis, core 1 idle, default priority 13, and the existing stride 17 4096-word pins32 capture. It restores output selectors and the PRBS input buffer after a successful stop. No live FEC path was added.
- `host/iq4hist_trial.py --bits 8` adds a separately rounded scalar FIR reference, strict waveform/low-bit/clock/metadata checks, priority sweeps and clock-ratio metadata. Its default remains the original 14-bit B1 trial. The ordinary firmware startup/output defaults are unchanged; the new command is compiled only with `IQ4HIST_EXPERIMENT=ON` into `iqbench`.

The B4 command accepts cpw1/2/4 at SYS128/256, rejecting combinations above 65 MSPS per axis. At SYS128 use cpw2/1 for 8/16 Msym/s. At SYS256 use cpw4/2 for the same rates after the coordinating engineer's separate clock-support work. This branch neither enables SYS256 nor edits `bench4fec.c` or clock checks there. Budget equations use ratios, so SYS256/cpw2 has the same port/cycle loads as SYS128/cpw2 while its absolute symbol rate doubles.

## Table, AFE format and spectrum gate

The **plain once-rounded, full-scale 8-bit table passes the existing held-DAC amplitude-mask certificate**. This is native/model evidence; no Pico, serial port, AFE or RF measurement was taken. The 16-Msym/s timing path remains a device experiment. The positive upper-mask reserve is only **0.02588977 dB**.

`iq4hist8_build()` in `firmware/src/iq4hist.c` uses B1's unchanged 28 frozen Q8 taps and direct Q30 levels `[992008094, -410903207, 410903207, -992008094]`. Let `S` be the int64 Q8-times-Q30 FIR sum. The byte value is exactly

```
q8 = floor((S + 3399*2^31)/(3399*2^32)).
```

This is nearest rounding with ties toward positive infinity, performed once after the complete FIR sum. C's negative-division truncation is explicitly corrected. No clipping, partial-plane rounding or floating-point arithmetic is used by the firmware generator. The 8-bit divisor relative to B1's unrounded 14-bit value is `3399/64 = 53.109375`. The unrounded extreme with the exact real constellation is 6744.5407305 at B1 scale; `ceil(6744.5407305*64/127) = 3399` chooses a simple rational scale that puts the largest unrounded byte near 127 without clipping. The unrounded 8-bit extreme is 126.9934118. The exhaustive once-rounded peak is **±127**.

Each history key retains B1's newest two-bit code in bits 1:0. One uint32 contains four chronological signed bytes, least-significant byte first. There are 16,384 rows, 65,536 samples and **65,536 bytes**. Little-endian table CRC32 is **0x5e13245c** (1578312796). All phase means are exactly zero. All rows are checked against a separately written scalar Python FIR; the optimized NumPy reference `iq4hist8_rows()` also agrees. Native guard/capacity tests reject invalid builds without writing. Four cyclic PRBS seeds and five arbitrary publication block sizes check continuous shaping against direct FIR history, including block boundaries.

`host/test_iq4hist8.py` reports maximum quantization error against real cos/sin and the frozen taps of **0.4999648899 8-bit LSB**, below the conservative Q30-level plus nearest-rounding bound of **0.5000000641 LSB**. That bound excludes original tap-design quantization and physical DAC error. The test verifies the frozen tap manifest/hash, every row and all bus sign/low-bit mappings. It compiles C with `-Wall -Wextra -Werror`.

## AFE input mapping checked against SLOS789C

The local AFE7071 SLOS789C text, **CONFIG1 p.18**, states that `twos=1` selects two's complement; its default zero selects offset binary. This table requires **two's-complement mode**. Bytes drive D13..D6 (GPIO13..GPIO6); D5..D0 remain zero. Interpreting the 14-bit bus as signed gives `q14 = 64*q8` exactly, including negative samples and zero. The peak AFE code is **±8128**, 0.06706 dB below positive code 8191. Physical sample gain versus the 14-bit table is `4096/3399 = 1.205060312`, or **+1.62018 dB**; this change must be retained when comparing analog amplitudes.

A low-six-bit constant `c` gives `q14 = 64*q8 + c` for every signed byte. In particular `c=32` adds one half of an 8-bit LSB as a DC offset; it does not improve the quantization of this nearest-rounded table. B4 selects `c=0`. SLOS789C **CONFIG5 p.20** documents `offset_ena`; **QMC block p.29** documents signed 13-bit `qmc_offseta/b` values −4096..4095 added before conversion, with gain backoff available to avoid saturation. Thus a deliberate constant offset has a correction mechanism, but no such offset or AFE register programming is needed by this digital test. Actual AFE configuration/calibration remains with the hardware owner.

## Same model and certificate as B1

The audit calls B1's `ensemble_spectrum()` on the **actual rounded 8-bit rows**, not on the underlying linear taps. Exact overlapping-history conditional means give the iid-uniform four-level ensemble covariance and PSD. An independent orthogonal sign-product decomposition checks probe PSDs and includes all nonlinear rounding terms in receiver variance. Ideal α=0.20 RRC receiver integration uses 16,384 and 65,536 frequencies; held 8-bit EVM changes by only 2.35×10⁻⁹ percentage points on refinement.

The locally checked ETSI EN 302 307-1 V1.4.1 **Annex A p.37 and Table A.1 p.38** give the α=0.20 amplitude-mask points; Annex A p.37 also states the rectangular-input `x/sin x` correction. The repository convention linearly interpolates mask power in dB. Frequency is `f_n=f/(Rs/2)`; upper comparisons cover `[0,6]`, lower `[0,1.11]`, and far reserve is above 40 dB on `[1.7,6]`. The ideal N4 per-axis DAC hold multiplies amplitude by `sinc(f_n/8)`. These normalized models are independent of SYS and symbol rate while N4 and the clock ratios are preserved.

| Actual rounded table / model | Upper margin | Lower margin | Far reserve above 40 dB | Ideal-RRC EVM including ISI | MER |
|---|---:|---:|---:|---:|---:|
| B1 14-bit / digital | −1.035085 dB | +0.250000 dB | −1.035085 dB | 1.224266% | 38.242484 dB |
| B4 8-bit / digital | −1.237880 dB | +0.250000 dB | −1.237880 dB | 1.251770% | 38.049511 dB |
| B1 14-bit / ideal hold | **+0.099513 dB** | +0.216889 dB | +2.092444 dB | 1.469870% | 36.654423 dB |
| B4 8-bit / ideal hold | **+0.025890 dB** | +0.217036 dB | +1.713347 dB | 1.493026% | 36.518655 dB |

The upper/lower continuous-frequency certificate subtracts a global amplitude-derivative bound times half the greatest grid gap from minimum amplitude slack over **1,048,586 frequencies**, including exact mask knots. Held 8-bit certified slacks are **+5.9302528×10⁻⁶ upper / +0.02226113 lower**. The worst upper point is `f_n=1.49823761`. The digital-only version fails, as B1 did. MER is `−20*log10(EVM_fraction)` and includes modeled pulse ISI, held response and nonlinear quantization, rather than quantization alone.

The model assumes iid uniform coordinates and an ideal held DAC. It does not certify deterministic PLHEADER/pilot/coded-frame spectra, the Annex A group-delay mask, all DAC images, physical AFE magnitude/phase response, RF compliance or receiver acquisition. The small positive certificate supports trying the digital runtime; it is not an analog safety margin.

## Rejected choices and reproduction

Simple division of B1's unrounded sample by 64 (peak 105) **fails** the held upper mask by **0.000285806 dB**. Division by 54 (peak 125) fails by **0.003670577 dB**. A fixed DC bias cannot repair these positive-frequency PSD differences. Full-scale scaling followed by ordinary nearest rounding passes, so mask-aware integer optimization is unnecessary for the selected table. No pulse taps were retuned.

```
python3 host/test_iq4hist8.py
python3 host/test_iq4hist8.py --spectrum
python3 host/test_iq4hist.py --spectrum
```

All completed successfully in this worktree. [`docs/rd/b4-model.json`](docs/rd/b4-model.json) preserves all numeric gate outputs, rejected-scale results and host Python/NumPy/platform versions. Host runtime is not a Cortex-M33 timing measurement. The new generator's int64 division runs only during setup; output does no FIR arithmetic or division.

## First-principles budgets

### Checked sources

The checked RP2350 source is the project's read-only
`docs/sources/rp2350.txt`, datasheet build 2025-07-29, in the main checkout.
Page numbers below are printed page numbers, checked against page footers.

- RP2350 §2.1, p24: PIO0/1/2, USB, DMA control registers, XIP DMA FIFOs,
  HSTX FIFO and trace DMA FIFO share one fast-peripheral port.
- RP2350 §2.1.1, p25: a zero-wait subordinate accepts one access per SYS
  cycle. DMA read and write are distinct managers, but they compete for this
  same subordinate. Higher priority does not increase its capacity.
- RP2350 §12.6, p1095: all sixteen DMA channels collectively support one
  read and one write, up to 32 bits each, per clock. This is **p1095** in the
  checked text; the older frontier input's p1094 citation is off by one.
- RP2350 §12.6.1, pp1095–1096: ENDLESS transfer counts do not decrement;
  completion/CHAIN_TO is ordered after the last write completes.
- RP2350 §12.6.3.2, p1100: chains trigger on completion; single-word control
  blocks can use register aliases. This specifies ordering, not a guaranteed
  number of SYS cycles for a complete short-block round trip.
- RP2350 §12.6.4.2, pp1101–1102: each channel has its own DREQ credit
  counter; multiple channels must not connect to one DREQ.
- RP2350 §11.1, pp876–877: three PIO blocks, four SMs per block, 32 shared
  instruction words per block; each SM has four words per FIFO direction or
  eight joined in one direction.
- RP2350 §11.5.4/§11.5.4.1, p909: MOV ISR resets the input shift count;
  autopush occurs in the final IN cycle unless stalled.
- RP2350 §11.5.4.2, p910: an OUT can refill the OSR in parallel with its
  final shift, but an already-empty OSR cannot refill and OUT in one cycle.
  Non-OUT instructions permit refill without stalling. Reading OSR before an
  OUT fence can race autopull and must not be used as a starvation-safe key
  update.
- RP2350 §4.2, pp337–338: 520 KiB SRAM, two 256 KiB striped regions and
  two 4 KiB unstriped scratch banks. Placement remains a contention variable.
- TI AFE7071 SLOS789C, p5 electrical-characteristics table: 65 MSPS/axis,
  130 million interleaved input words/s maximum. N4 at 16 Msym/s uses
  64 MSPS/axis and 128 million bus words/s, within those limits; this does
  not establish electrical timing or RF performance.

## Units and accounting

Let `d = cpw`, `c = output chains per axis`, and `fSYS = 128 MHz` here.
Every symbol has four samples per axis, eight physical bus slots, and lasts
`8d` SYS cycles. Thus `Rs = fSYS/(8d)`; d=1/2 gives 16/8 Msym/s. An
alternate-symbol chain has `8dc` SYS cycles between its rows. These ratios
also apply at SYS256 with the same d; there is no clock/VREG/flash change.

One DMA transfer means one read/write pair. A sample word from SRAM to a
writer FIFO uses one FASTPERI access. A PIO-generated pointer from RX FIFO
to a DMA trigger register uses **two**, because both endpoints share
FASTPERI. A pointer from SRAM to the same trigger uses **one**. The packed
source uses one 32-bit word/16 symbols/axis, hence 0.125 transfers and port
accesses per symbol for two pointer SMs.

Stride-17 pins32 capture performs one FIFO-to-SRAM transfer per 17 physical
bus slots: `8/17 = 0.470588235` extra transfers **and** port accesses per
symbol. Its extra utilization is `1/(17d)`: 5.882353 percentage points at
d=1, 2.941176 at d=2. At d=1 it is 7.529412 M transfers/s and 30.117647
MB/s of SRAM writes, for about 544 us per 4096-observation capture. d=2
halves the rate and doubles that duration. CPU polls, USB, SPI, credits and
arbitration are outside the cyclic budget below.

## One chain per axis

Rates in the following table are for d=1; transfer/access counts per symbol
are clock independent. Percentages are of the ideal 128 M/s capacity.

| Row/source | DMA transfers/symbol | FASTPERI accesses/symbol | DMA load, capture off/on | FASTPERI load, capture off/on |
|---|---:|---:|---:|---:|
| 14-bit, PIO pointers | 6.125 | 8.125 | 76.563% / 82.445% | **101.563% / 107.445%** |
| 8-bit, PIO pointers | 4.125 | 6.125 | 51.563% / 57.445% | **76.563% / 82.445%** |
| 8-bit, SRAM cyclic pointers | 4.000 | 4.000 | 50.000% / 55.882% | **50.000% / 55.882%** |

All percentages halve at d=2, including the capture increment. The 14-bit
one-symbol PIO-pointer path therefore cannot run at d=1 even without
capture, independently of chaining latency or software. Counting just
6.125 DMA transfers/symbol misses the stronger 8.125-port-access bound.

For the 8-bit PIO path at d=1, the component rates are 32 M sample
transfers/s, 32 M pointer transfers/s, and 2 M packed-input transfers/s:
66 M DMA transfers/s but 98 M FASTPERI accesses/s. During capture those
become 73.529 M transfers/s and 105.529 M accesses/s. Sample/source SRAM
reads total 136 MB/s before capture. SRAM cyclic pointers instead need
64 M transfers/s and 64 M port accesses/s, but 256 MB/s of SRAM reads:
128 MB/s of samples plus 128 MB/s of pointers. The port saving trades for
more SRAM read traffic; it is not free bandwidth everywhere.

## Two chains per axis: distinguish the graphs

Merely doubling writer/control/sample chains does not change the number of
rows or samples sent, and gives each alternating chain 16d cycles per row.
It does not remove the 14-bit port impossibility. PIO pointers require a
specific legal way to give each control channel its own DREQ.

**Independent alternate-row producers:** use four pointer SMs, each reading
the full axis code stream but emitting only even or odd rows. Histories
cannot be computed from decimated symbols. The duplicate input FIFOs raise
packed-source traffic from 0.125 to 0.25 transfers/accesses per symbol;
each same-axis DMA may read the same immutable SRAM ring, so it need not
duplicate the storage.

| Two independent chains/axis | DMA transfers/symbol | FASTPERI accesses/symbol | DMA load, capture off/on at d=1 | FASTPERI load, capture off/on at d=1 |
|---|---:|---:|---:|---:|
| 14-bit, four PIO producers | 6.250 | 8.250 | 78.125% / 84.007% | **103.125% / 109.007%** |
| 8-bit, four PIO producers | 4.250 | 6.250 | 53.125% / 59.007% | **78.125% / 84.007%** |
| 8-bit, two SRAM pointer rings | 4.000 | 4.000 | 50.000% / 55.882% | **50.000% / 55.882%** |

The SRAM graph reads even/odd entries with stride two into separate control
channels and requires no shared peripheral DREQ. It has the same global
traffic as one chain, with two independent handoff paths per axis.
Four deinterleaved contiguous pointer rings are an equivalent alternative;
they contain the same total number of pointer words and give each control
channel its own ordinary incrementing, naturally aligned address ring.

**A2 sole-RX router graph:** preserve two pointer SMs and route each next
pointer alternately to two sample channels. Each axis row adds one selector
SRAM-to-DMA-register transfer and one FASTPERI access. For 8-bit rows this is
6.125 DMA transfers and **8.125 FASTPERI accesses/symbol**, or 76.563% DMA
and **101.563% FASTPERI** at d=1 without capture. For 14-bit rows the counts
are 8.125 and 10.125, or 101.563% and 126.563%. Add the same 8/17 capture
increment. This legally fits the DREQ rule but cannot sustain B4 d=1. Its
two sample channels are serialized by C/S/R, so it is not equivalent to
two independent control/sample chains.

Sharing one RX DREQ between two active control channels is rejected by the
checked datasheet, including when software expects alternating activity.

## SMs, channels and SRAM

Counts below are for immutable cyclic sources, including the separate DDR
clock but no SPI. “Off” removes the capture service from the logical graph;
the implemented one-chain backend reserves its capture SM/channel even
before the capture DMA starts. Capture adds one SM and one DMA channel.

| Source / chains per axis | Writer SMs | Pointer SMs | Total SMs, capture off/on | DMA channels, capture off/on |
|---|---:|---:|---:|---:|
| PIO, one | 2 | 2 | 5 / 6 | 6 / 7 |
| PIO independent, two | 4 | 4 | 9 / 10 | 12 / 13 |
| PIO A2 router, two | 4 | 2 | 7 / 8 | 10 / 11 |
| SRAM cyclic, one | 2 | 0 | 3 / 4 | 4 / 5 |
| SRAM cyclic, two | 4 | 0 | 5 / 6 | 8 / 9 |

Each independent control/sample pair costs two DMA channels. Each cyclic
pointer SM needs one input channel; the router uses four output DMA
channels per axis (C/R/even-sample/odd-sample). The 14-bit/8-bit row width
does not change channel or SM counts. These counts do not claim that the
hypothetical two-chain programs or all block placements are implemented.

The implemented B4 layout is PIO0 two byte writers, PIO2 two pointer SMs
and the DDR clock, and PIO1 capture: six SMs/seven reserved DMA channels.
The seven-instruction pointer program plus two clock instructions occupies
nine PIO2 instruction words. It has no four-producer resource requirement.

For comparison, adding today's three-SM/three-DMA on-chip PV-SPI source and
one live credit DMA per pointer SM gives the following live totals with
capture: one-chain PIO = 9 SM/12 DMA; four-producer two-chain = **13 SM/20
DMA**, exceeding both limits; router two-chain = 11 SM/16 DMA, fitting
resources but failing the d=1 port budget for this one-symbol row size.
Thus the four-producer cyclic fallback does not establish a live-FEC fit.

Memory arithmetic and explicit storage choices:

- 14-bit table: `4^7 * 4 samples * 2 bytes = 131072 B`, aligned 128 KiB.
- 8-bit table: `4^7 * 4 samples = 65536 B`, aligned 64 KiB. Reuse the
  aligned `ring_buf`; do not allocate a second LUT. A uint32 row is four
  chronological bytes.
- PIO cyclic source as in B1: two 4096-word axis rings = 32768 B, encoding
  65536 symbols. B4 table plus source = 98304 B; B1 = 163840 B. Four pointer
  SMs can share those immutable rings.
- SRAM-pointer alternative: two 4096-pointer rings = 32768 B for a
  4096-symbol cyclic waveform; table plus pointers = 98304 B, fitting the
  128 KiB borrowed arena. Generate six-symbol wrap history from the chosen
  cyclic sequence. A 32768-byte DMA address ring permits up to 8192
  pointers/axis; even/odd chains stride by two through it.
  Alternatively, four naturally 8192-byte-aligned rings of 2048 pointers
  each use the same 32768 B, with no stride-two configuration.
- Preserving B1's full 65536-symbol cyclic source as precomputed pointers
  would need **524288 B for pointers alone**, before the 64 KiB table. The
  short SRAM-pointer experiment must change the cyclic period, not silently
  assume the full source fits.
- The 4096-entry pins32 capture reuses 16384 B of existing capture storage.
  Logical table shrinkage is 64 KiB; placement and the existing allocations
  determine how much becomes available for another live workspace.
- The frontier's second-FEC-context 48648 B is an old planning assumption,
  not a linker measurement. A1's later report identifies a 1664 B additional
  normal-5/6 pack1 LDPC workspace plus staged-frame buffers and ownership
  state; other rates/context choices require their own accounting. No
  frame-parallel image or its placement is claimed here.

## PIO and short-row deadlines

Each row represents one symbol on one axis. The following row work excludes
amortized packed-source input and capture; capture increases contention but
does not extend the row deadline. Two chains mean independent alternating
control/sample pairs, not the serialized A2 router.

| Path | Sample words/row | Control+sample DMA transfers/row | FASTPERI accesses/row | Available SYS cycles, cpw1 one/two chains | Available SYS cycles, cpw2 one/two chains |
|---|---:|---:|---:|---:|---:|
| 14-bit PIO pointer | 2 | 3 | 4 | 8 / 16 | 16 / 32 |
| 8-bit PIO pointer | 1 | 2 | 3 | 8 / 16 | 16 / 32 |
| 8-bit SRAM pointer | 1 | 2 | 2 | 8 / 16 | 16 / 32 |

PIO source input adds 1/16 DMA transfer and port access per axis row for one
producer, or 1/8 for duplicated independent producers. Capture adds 8/17
transfers and accesses per full I/Q symbol to the global budget. The table
states work and deadlines; none of its access counts is a measured handoff
latency.

B1's current key engine is exactly 145 instructions/16 symbols = 9.0625
non-stalled PIO cycles/symbol. It fails the eight-cycle d=1 deadline even
with unlimited DMA service. Its ideal SYS128 ceiling is 14.124 Msym/s.
Shrinking rows alone does not fix that.

A starvation-safe seven-instruction B4 key update uses OUT autopull32,
IN autopush16, Y=table-base>>16 and a rolling history in X:

```text
MOV ISR, X       ; save previous history
OUT X, 2         ; get next code, safely fenced by autopull
IN X, 2          ; append code to history
MOV X, ISR       ; preserve rolled history
MOV ISR, Y       ; reset shift counter, start address prefix
IN X, 14
IN NULL, 2       ; address = base | key<<2; autopush16
```

The exact non-stalled occupancy is 7/(8d): 87.5% at d=1, 43.75% at d=2.
All throughput loss from FIFO starvation/backpressure/arbitration is extra.
The OUT must precede reading its new code; replacing it with IN OSR before
a later OUT is unsafe when the source FIFO empties at a word boundary.

For a hypothetical independent even/odd producer, one output update costs
seven instructions, while the intervening history-only update costs four:
eleven instructions/two full-rate symbols, 68.75% at d=1. This is an
instruction-count feasibility observation, not a verified alternate-row
implementation. It assumes the output/history-only pair is unrolled in
eleven program words and uses free wrap, with the two SMs starting at
different positions in the pair. Startup history and first-row parity must
be proved. Simply running the complete seven-instruction producer on every
symbol and adding a parity branch costs at least another instruction per
symbol (8, or 9 with an additional branch/control step), and cannot inherit
the 5.5-cycle estimate. Two chains get 16d cycles between each chain's rows;
every producer still must track every input symbol correctly.

One chain's critical dependency is control RX/SRAM read → trigger write →
sample read → sample FIFO write → completion chain → next control read.
The 8-bit row removes one sample transfer and one port access per axis row
relative to 14-bit. Both control/sample transitions remain. It is plausible
that the shorter burst changes the eight-cycle handoff limit, but **the
number of SYS cycles saved in steady state is unmeasured**. Average port
slack cannot prove that this loop completes in time.

Joined eight-word writer FIFOs hold 32 axis samples/eight symbols for
8-bit rows, versus 16 samples/four symbols for 14-bit rows. A preloaded OSR
adds one symbol or half a symbol respectively. Thus the ideal initially
full one-chain runway at d=1 is 72 versus 36 SYS cycles, before bus-service
details. A two-chain alternating layout gives each writer a silent symbol
between rows and further FIFO runway, but does not create port capacity.

## Live 8PSK FEC at 16 Msym/s, SYS128

**No requested rate fits the existing measured conversion cost, even with
perfect frame-parallel balancing.** This is a model conclusion, not a new
M33 measurement. The cyclic B4 gate can succeed independently of this.

All normal pilot-enabled 8PSK frames contain 22194 symbols. At 16 Msym/s
their duration is 1.387125 ms and each core has 177552 SYS cycles/frame;
two cores together have 355104. Stage inputs are the measured optimized
`fec_stage_A` BB+BCH and pack1 LDPC counts and `fec_stage_B` packed-8PSK
mapping counts from [frontier inputs](docs/frontier/frontier_inputs.json),
with provenance in that file. 3/4 retains its measured t=12 kernel; the
merged t=8 change does not reduce it.

Use the prior measured GCS ingress cost 0.606207857 cycles/BB data-field
bit, conversion 3.45–3.59 cycles/symbol from
[B2](results/b2-8psk-live/2026-10-15/README.md), 3.3% of one core for
supervision and the observed approximately six-point core-1 penalty from
mask 13. The formula for ideally balanced per-core utilization is

```text
U = (C_BB+BCH + C_LDPC + C_map + 0.606207857*(Kbch-80)
     + C_convert*22194 + (0.033+0.060)*177552) / (2*177552).
```

| Code | BB+BCH | Pack1 LDPC | 8PSK map | GCS ingress | BB capacity | Ideal balanced/core, conversion 3.45–3.59 |
|---|---:|---:|---:|---:|---:|---:|
| 2/3 | 91907 | 107834 | 47952.9 | 26042.7 | 30.9705 Mb/s | **103.30–104.17%** |
| 3/4 | 139286 | 114667 | 47948.4 | 29296.8 | 34.8404 Mb/s | **119.48–120.36%** |
| 5/6 | 113948 | 111314 | 47951.8 | 32589.7 | 38.7564 Mb/s | **112.33–113.20%** |

Cycles are per frame; capacity excludes BBHEADER and application overhead.
The table assumes ideal division of all work and adds no staging copy,
atomic ownership, queue coordination, frame-time variation or new memory
contention. It already exceeds 100%, so ordinary scheduling cannot rescue
the current pipeline. Applying the frontier's prior measured bank1 BCH
factor 1.05593 and LDPC/map factor 1.02102 raises these intervals to
105.67–106.54%, 122.64–123.51%, and 115.07–115.94%, respectively. Those
factors are estimates for the B4 placement, not universal constants.

The maximum conversion cost at the absolute two-core 100% boundary is
2.922 cycles/symbol for 2/3, 0.333 for 3/4, and 1.477 for 5/6 using isolated
FEC. With the prior contention factors it is 2.543, negative, and 1.039.
B1's packer has 43 M33 instructions/16 symbols and an instruction-based
45–60-cycle/group estimate (2.8–3.75 cycles/symbol), consistent with B2's
device result. Native host timing is not used as an M33 measurement.

If conversion were eliminated by directly producing the common two-bit
codes in the mapper, the isolated-stage ideal loads would instead be
81.74%, 97.92% and 90.77% before new staging/contention. Only 2/3 has
comfortable scope to investigate; this would be a different implementation
and must be measured. Frame-parallel FEC is still unimplemented in the
merged firmware and must contextize workspaces, bound supervision gaps
and prove publication ordering. The current on-chip32 PV-SPI capacity in
the frontier is only 30.8697 Mb/s, slightly below even 2/3's 30.9705 Mb/s;
ingress is another independent gate.

As a measurement sanity check, B2's 5/6 8-Msym/s positive run recorded
0.8924 encoder, 0.215895 conversion and 0.033001 supervision fractions.
Doubling the useful work rate while keeping supervision time based gives
`(2*(0.8924+0.215895)+0.033001)/2 = 112.48%` ideally balanced/core.
That measurement already includes mask13 effects; adding another six
points there would double-count them. Agreement with the isolated-stage
estimate supports the negative compute conclusion, not a B4 timing claim.

## Actual linked memory and validation

The final experiment build uses Pico SDK 2.2.0, GCC Arm 14.2.1, Release/O3 and explicit `CORE0_STACK_SRAM_HI=ON`. Its `iqbench.elf.map` gives:

| Allocation | Address / size |
|---|---|
| Borrowed output ring | `0x20000000`, 131072 B reserved |
| B4 table inside that ring | `0x20000000..0x2000ffff`, 65536 B |
| Unused second half during B4 | `0x20010000..0x2001ffff`, 65536 B; still statically reserved |
| I/Q packed source | `0x20020000..0x20027fff`, 32768 B |
| Existing link ring | `0x20028000..0x2002ffff`, 32768 B |
| Lower SRAM data / BSS | `0x20030110` / 40496 B, `0x20039f40` / 23224 B |
| Minimum heap | `0x2003f9f8` / 1024 B, 520 B beyond minimum |
| Existing `.sram_hi` | `0x20040000` / 258016 B; ends `0x2007efe0` |
| Core0 stack | `0x2007f000..0x2007ffff`, 4096 B; 32 B gap below |
| SCRATCH_X code / core1 stack | 2032 B / 2048 B, 16 B gap |
| SCRATCH_Y code | 1952 B, 2144 B remaining; no core0 stack there |
| Capture used by B4 | first 16384 B of existing `cap_buf` at `0x20051fac` |

There is no new static table/source/capture allocation. B4's 64 KiB logical saving stays inside the borrowed ring; it has not increased linker free space. A future extra FEC context could deliberately reuse that region, but would need explicit ownership and placement work. The 32 B high-SRAM gap and prior 31% BCH bank-placement effect make additional static state and code placement material risks.

A fresh configure of the base CMake file can move the stack while retaining the SDK's 2 KiB default: `option(CORE0_STACK_SRAM_HI ...)` occurs after the compile-definition conditional. This branch leaves that unrelated default configuration unchanged. The commands below explicitly supply the option on the first configure; the final map proves 4096 B, and the B4 runner checks the generated Ninja definitions for `PICO_STACK_SIZE=0x1000` before opening a device. The firmware build in `firmware/build` was also completed successfully with the ordinary experiment-OFF configuration.

Validation completed without device access:

- `make build`: iqbench and pvflight pass; no introduced warnings.
- Explicit experiment configure plus `make build BUILD=firmware/build-b4-runtime`: both targets pass; no compiler warnings.
- `make check`: the final complete native/model/reference/link suite exits 0, including the new exhaustive table/pointer tests, the 96-case native DMA setup test and the expanded ten-test runner validation.
- `python3 host/test_iq4hist8.py --spectrum` and `python3 host/test_iq4hist.py --spectrum`: selected 8-bit and baseline 14-bit certificates pass as reported above.
- `test_iq4ptr8.py`: all 16384 keys at 8 SRAM bases, 90 delayed/undelayed stream cases, five cyclic laps, and 32768 byte-writer/clock slots. TX starvation is inserted at source-word seams and RX push backpressure is admitted. The abstract PIO model does not simulate bus arbitration.
- `test_iq4writer8.py`: native compilation of the actual selector/axis-start/control/sample DMA functions checks 96 format/rate/writer combinations, COUNT 1 sample rows, RX DREQ ownership, chain configuration, invalid sources/selectors and rejection of one-word rows in the ordinary descriptor entry point. Native C compiles with `-Wall -Wextra -Werror`.
- The runner's synthetic captures span a source wrap, both clock phases, SYS128/cpw1/2/4 and ratio-equivalent SYS256/cpw2/4. Corrupt low/high sample bits, IQ_FLAG, spare, clock, source identity, transport, priority, row format, duration/accounting and cleanup fail validation. SYS256 cases are synthetic tests, not a clock qualification.
- `git diff --check` and relative Markdown links pass. No files under `results/` were modified.

Native host environment: Python 3.13.2, NumPy 2.2.5 and Apple clang 17. Host duration is never used as an M33 cycle measurement. Full numeric spectrum output and environment provenance are in [b4-model.json](docs/rd/b4-model.json).

## Reproduction and coordinating-engineer device commands

Build only; these commands do not connect to the Pico:

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-b4-runtime -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON \
  -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-b4-runtime
python3 host/test_iq4hist8.py --spectrum
python3 host/test_iq4hist.py --spectrum
make check
B4_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-b4-runtime/git_rev.h)
```

The coordinating engineer loads **`firmware/build-b4-runtime/iqbench.uf2`** using their normal procedure. The locally verified final build identifies as `0b89b341ce-dirty`; UF2 SHA256 is `9732473f8f56c5e8365e33559e99a66916999be7d2c52e1c21be86421f018154`, ELF SHA256 `2c83becbfa13ae569044b5ad6f090cc41d808a9c05e2aaf9a2844031ecdc46ee`. Rebuilding after edits/commit can change hashes. A dirty revision is not a unique source identity; retain the runner's binary/config/source manifest. Use fresh result directories. With an actual AFE attached, configure its input `twos=1`; this digital firmware does not program AFE registers.

```sh
# 8 Msym/s cyclic8, SYS128, 32 MHz GPIO28: expected clean, still unmeasured.
python3 host/iq4hist_trial.py --bits 8 --clock-khz 128000 \
  --ms 60000 --cpws 2 --inverts 0 --priorities 13 --reps 3 \
  --build firmware/build-b4-runtime --expected-git "$B4_FW_GIT" \
  --out results/b4-8bit-rows/control8-local

# 16 Msym/s cyclic8, one chain/axis, 64 MHz GPIO28: record pass OR failure.
python3 host/iq4hist_trial.py --bits 8 --clock-khz 128000 \
  --ms 60000 --cpws 1 --inverts 0 --priorities 13 --reps 3 \
  --build firmware/build-b4-runtime --expected-git "$B4_FW_GIT" \
  --out results/b4-8bit-rows/target16-local

# Inverted-clock negative control at both rates, separately archived.
python3 host/iq4hist_trial.py --bits 8 --clock-khz 128000 \
  --ms 3000 --cpws 2 1 --inverts 1 --priorities 13 \
  --build firmware/build-b4-runtime --expected-git "$B4_FW_GIT" \
  --out results/b4-8bit-rows/inverted-local
```

Equivalent device lines after `clock 128000`: `bench4live8 2 60000 0 13`, `bench4live8 1 60000 0 13`, `bench4live8 2 3000 1 13`, `bench4live8 1 3000 1 13`. No second-chain or SRAM-pointer backend is claimed by these commands. `--bits 14 --cpws 2 --inverts 0 1` retains the existing B1 cyclic control if wanted. An optional priority 0 comparison can use `--priorities 0 13`; cyclic core 1 idle behavior must be recorded rather than assumed to reproduce B2's live-FEC priority dependence.

Expected B4 metadata is `cmd=bench4live8`, `sample_bits=8`, `low_bits=0`, `row_words=1`, `table_bytes=65536`, `table_crc=1578312796`, `writers=2`, `bus_priority=13`, `sym_rate=8000000/16000000`, `input_crc=input_after_crc=782600642`, and `previous_words=[1555404632,903819826]`. GPIO28 must equal IQ_FLAG in every positive snapshot. All 4096 snapshots must match the independent cyclic scalar FIR at **one fixed stride 17 offset**, including all low 6 bits and spare 15; capture transport CRC must match. Errors, pointer errors, output errors, writer TX stalls and pointer TX stalls must be zero. RX push backpressure is expected and is recorded.

Pass also requires the complete requested elapsed duration, correct source read-issue rate (prefetch allowance 32 words plus 0.1%), unchanged source CRC, supervision gap 128..256 us, finite/correct cycle fractions, selectors restored and source restored. At 16 Msym/s each axis issues about 1 M source words/s, versus 0.5 M at 8. Capture begins halfway through the run; retain fault times to distinguish startup, capture-triggered and rare later failures. Positive classification is `digital_live_clean`, `faults=[]`, runner exit 0. A failed target intentionally returns nonzero while preserving logs/capture/manifest; keep it as a negative result.

The inverted control must retain correct data and show **4096/4096 clock-parity mismatches**, both clock levels present, and `classification=clock_inversion_detected`, `faults=[]`, exit 0. A TX stall or data mismatch does not count as a successful inversion control. If 16 Msym/s cannot keep data clean, the 8 Msym/s inversion remains the usable parity control and the 16 Msym/s record remains a throughput failure. Three clean 60 s runs are an initial digital qualification, not proof of indefinite operation, temperature behavior, every transmitted sample or RF performance.

## Remaining risks and dead ends

The measured B2/merged-image 40–51 s stalls show that port averages and short clean runs do not guarantee enough DMA latency margin. The one-word sample burst and doubled FIFO symbol runway improve the graph but do not establish an eight-cycle control/sample round trip. Capture itself temporarily adds 5.88 port points at cpw1. The PIO key SM has one spare cycle/symbol before any stall; unmeasured DMA arbitration can consume it. Bank placement, channel numbering, USB and supervision add traffic beyond the cyclic average budgets.

The table's held upper margin is only 0.026 dB and its far reserve is below the original pulse's 2 dB design reserve, although it passes the 40 dB mask. The digital-only mask fails. No group-delay, real AFE hold/reconstruction, pad timing, jitter, RF spectrum/EVM, LO leakage or receiver result is claimed. Full-scale byte mapping raises analog amplitude 1.62 dB relative to B1 and needs the explicitly selected AFE format.

Rejected: retaining the 14-bit one-symbol PIO rows at cpw1 (shared-port impossibility); shrinking rows while keeping B1's 145-cycle pointer word (PIO impossibility); IN OSR before an OUT fence (autopull race); duplicating one RX DREQ (forbidden credit graph); A2 routing at 16 Msym/s (port impossibility); precomputing all 65536-symbol pointers (SRAM overflow); fixed low6=32 (unnecessary DC); the /64 and /54 nearest-rounding candidates (held-mask failure). Entrywise mask-aware rounding was unnecessary because full-scale plain rounding passed. Two independent chains or short SRAM-pointer rings are budgeted alternatives, not implemented or measured fallbacks.

Live 16 Msym/s needs a separate fused common-code mapper/converter, frame-context ownership and ingress work before a FEC claim is credible. At the current conversion cost, additional cores do not exist to close the computed deficit. This branch's honest result is a passing 8-bit waveform model and a reviewable cyclic-output hardware experiment.

## Intended small logical commits

Git metadata remains read-only; these are proposed commits for the coordinating engineer, not commits created here. No co-author trailers are intended.

1. `firmware/src/iq4hist.c`, `firmware/src/iq4hist.h`, `host/pulse_profiles.py`, `host/native/iq4hist8_test.c`, `host/test_iq4hist8.py`, `host/test_iq4hist.py`: **Add exact full-scale 8-bit L7 rows and held-mask audit.**
2. `firmware/src/iq4ptr.c`, `firmware/src/iq4ptr.h`, `firmware/src/dual_dma.c`, `firmware/src/dual_dma.h`, `host/test_iq4ptr8.py`, `host/test_iq4writer8.py`: **Add seven-cycle cyclic pointers and opt-in one-word OUT8 writers.**
3. `firmware/src/bench4live.c`, `firmware/src/bench4live.h`, `firmware/src/main.c`, `host/iq4hist_trial.py`, `host/test_iq4hist_trial.py`, `Makefile`: **Expose cyclic 8-bit trials with strict bus and inverted-clock validation.**
4. `REPORT.md`, `docs/rd/b4-model.json`: **Record B4 model evidence, resource limits and coordinator trials.**
