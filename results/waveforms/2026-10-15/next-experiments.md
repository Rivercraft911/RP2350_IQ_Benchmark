# Experiments with a plausible material payoff

Use the measured campaign as the baseline. Keep a change only when it removes
whole work, materially improves scheduling margin, or saves useful SRAM without
hiding waveform/ownership costs. Five percent is a useful threshold; a new lookup
or special case for a tiny instruction gain is usually a poor trade.

## 1. Connect the hardware address engine to real FEC/SPI production

The live `quadlive` test now connects two raw-symbol PIO pointer generators to the
FIFO-fed sample-DMA backend. Its cyclic PRBS source is immutable. This removes
CPU descriptor generation and sample arithmetic; the CPU publishes finite source
credits and checks faults. It has its own live-rate/capture evidence, separate
from the complete transmitter.

The next experiment replaces that source with an actual continuous QPSK encoder
ring. Publish only completed packed words after a release barrier. Reuse storage
only below the minimum I/Q source-issue cursor minus the conservative eight-word
retirement guard. Reserve space for the worst next PLFRAME, including its partial
packed tail; do not flush history or pad between frames. Input credit zero must
stop the source in hardware before unpublished data can be fetched.

Require a 60-second full 16-Msym/s input→FEC→mapper→pointer→sample-DMA run with
independent SPI CRC-chain and waveform checks, positive credit runway, deliberate
source-starvation controls and safe post-abort counters. Compare like-for-like
CPU timing scopes. A raw-source supervision percentage cannot replace the old
whole-radio active-loop result. Count actual retained legacy DMA/SM claims before
adding the emulated SPI master and capture.

## 2. Balance the two 8PSK cores with bounded BCH work

The fixed-8PSK 60-second baseline reports roughly 75.5% shaper work on core 0 and
92.1% ingress/encoder work on core 1. Moving about 29,420 BCH cycles/frame from
core 1 to core 0 would balance their measured useful work near 83.8%, before
additional handoff/control overhead. This is an approximately eight-point
bottleneck-margin opportunity, not a total-cycle saving or measured speedup.

Keep GCS construction and its borrowed packet cursor on core 1. Move
`frontend_begin` plus about 256 BCH input words to core 0 in two bounded 128-word
steps, scheduled only when the output queue has spare blocks. Core 1 resumes
that frontend state and performs LDPC/mapping. Two frontend slots can reuse the
existing FEC arena and `cfg.tq + PV_EMU_WORDS`: 5,328 + 2,027 = 7,355 of 8,192
words. Configuration and BCH tables remain fixed/read-only.

Release the single BB slot only after copying it into its frontend slot. Transfer
cursor/parity/state with explicit ownership, and reclaim neither slot until
LDPC/mapping completes. Record whole core-0 active time as well as stage work.
Require exact existing CRCs/captures, 60 seconds at both clocks and at least five
points less busy-core work. If handoff costs erase that margin, keep the simpler
full-core-1 encoder. Higher FEC rates need their own measured budgets.

## 3. Put the exact 16APSK component under real output traffic

The native L8 3+4+4-coordinate kernel measures 21.106 cycles/symbol in isolation.
At 150 MHz / 6.25 Msym/s the budget is 24 cycles/symbol. First stream its actual
samples through DMA/PIO and validate every captured word against the separate
native-chunk quantizer. Count load/store contention and complete output
supervision; component timing alone leaves little margin for those costs.

Only then add the standard four-column interleaver/mapper, code-specific ring
ratio, MODCOD/header/pilots/Gold and continuous partial-frame history. Direct
FEC-column gathers avoid materializing an interleaved frame. Encode final
three-bit coordinate indexes directly instead of gathering bit planes again
inside the shaper. Keep the explicit `R2=1` amplitude policy.

The symmetric 72-KiB L8 table layout is a memory-oriented alternative; the tested
native layout uses 136 KiB. These have different separately rounded partials.
Do not substitute one without independent references and new golden CRCs.

## 4. Measure the analog chain before choosing a pulse

The L8/L9 models include finite taps, fixed-point rounding and DAC hold. Two-SPS
Nyquist-region mask checks do not qualify the first reconstruction image or the
physical AFE response. Measure clock/data setup, clipping/headroom, filter tuning,
images, spectrum/EVM and actual receiver decoding. Treat the narrow model margins
as quantities to verify. Pre-emphasis can compensate a known response within
available headroom; it cannot guarantee a perfect inverse of unknown hardware.

## Lower priority

The [3-SPS review](three-sps.md) finds a plausible digital row-six experiment,
but 21.333 Msym/s needs 25.6 MHz nominal RF width, beyond the proposed AFE7071's
stated 20-MHz signal bandwidth. Staying within that width limits the theoretical
5/6 capacity improvement to 4.17%. The faster row handoffs, input capacity,
encoder balance and odd/fractional clock duty also need separate tests. Integrate
the proven 4-SPS hardware-address engine with real FEC before adding this mode.

Exact 32APSK coordinate algebra and native references exist, but the larger
alphabet/indexing burden has no measured throughput benefit. Approximate
constellation planes add an EVM/interoperability budget without a demonstrated
advantage over exact chunks. Further overclocking is unnecessary to establish
these architectural questions; the current campaign stays at stock voltage and
128/150-MHz clocks.

A [common native 8PSK/16APSK alphabet](native8-16-common-alphabet.md) is another
memory-oriented option: 11 axis levels, four three-coordinate chunks and FIR
symmetry give 49.59 KiB including the key map. The independent math checks all
six ring ratios separately. It requires eight row lookups per quad instead of
six, a new mapper/history representation and one fixed gamma/orientation/profile.
It could support future coherent native-mode transitions, but is unimplemented
and has no cycle or live-transition result. It cannot directly reuse either the
current rotated-8PSK tables or the fixed eight-level 16APSK tables.
