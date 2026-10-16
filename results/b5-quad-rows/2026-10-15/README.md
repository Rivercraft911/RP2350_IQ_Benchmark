# 8PSK at 16 Msym/s on the RP2350 at its rated clock (cyclic, hardware shaping)

**Result.** With four independent control/sample DMA chains (two per axis, alternate
one-symbol rows), four PIO writers and table-row pointers read from SRAM, the RP2350B
sustained pulse-shaped 8PSK at **16 Msym/s, N4 (64 MS/s per axis, 64 MHz GPIO28 DDR clock)
at SYS = 128 MHz**: 12 of 12 60-s runs clean with 8-bit rows. No overclock, no CPU work per
symbol (core 1 idle, core 0 supervision 0.76%). The 14-bit variant passed 5 of 6 60-s runs; the
failure was a writer stall during the sparse capture, which raises its port load from 75% to 81%.

Pico Plus 2, RP2350B A2, USB only, image `5d7ddab57b` (`rd/b5-quad-rows`, experiment build,
`CORE0_STACK_SRAM_HI=ON`; `flash-short.txt`). Source: B4's 8PSK PRBS, a 4096-symbol cycle whose
2 x 4096 table pointers are precomputed and immutable during output. Every positive run checks
4096 stride-17 GPIO0..31 snapshots (taken halfway through the run) against an independent scalar
FIR reference of the same symbols, plus GPIO28 against IQ_FLAG (`host/iq4quad_trial.py`).
Design and budgets: [docs/rd/b5-quad-rows.md](../../../docs/rd/b5-quad-rows.md).

| Run set | Rows | Rate | Priority mask | Duration | Result |
|---|---|---:|---:|---:|---|
| `short/` | 8-bit, 1 word | 8 and 16 Msym/s | 0, 13 | 3 s | 4/4 clean |
| `inverted/` | 8-bit | 8 and 16 Msym/s | 0, 13 | 3 s | 4/4 inversion detected (4096/4096 clock mismatches, data exact) |
| `qual-60s/` | 8-bit | 8 Msym/s | 0, 13 | 60 s x 3 each | 6/6 clean |
| `qual-60s/` | 8-bit | **16 Msym/s** | 0, 13 | 60 s x 3 each | **6/6 clean** |
| `rows14/` | 14-bit, 2 words | 8 and 16 Msym/s | 0, 13 | 3 s | 4/4 clean, 4/4 inversion detected |
| `qual14-60s/` | 14-bit | 16 Msym/s | 0 | 60 s x 3 | 3/3 clean |
| `qual14-60s/` | 14-bit | 16 Msym/s | 13 | 60 s x 3 | 2/3 clean; rep 1 **writer TX stall at 30.0004 s** (I 2, Q 3), 2793 of 4096 capture words in |

Clean = `digital_quad_clean`: full duration, zero pointer/output/stall errors, 0/4096 bus and
0/4096 clock mismatches, pointer and table CRCs unchanged (8-bit table 0x5e13245c, 14-bit
0x19bf3c1d).

## Reading the numbers

- Port arithmetic (RP2350 §2.1, one fast-peripheral access per SYS cycle): an SRAM pointer
  costs one access, each sample word one. 8-bit rows: 4 accesses per symbol = 50% of the
  port at 16 Msym/s, 55.9% while the stride-17 capture runs. 14-bit rows: 6 = 75%, 80.9%
  with capture.
- The 14-bit stall happened about 370 us into the 544-us capture, the same signature as A2's
  four-writer QPSK path at 16 Msym/s. At 81% port load the hand-off margin is thin; at 56% none
  of 12 runs failed. One failure in six is not a failure rate, and twelve clean runs do not
  prove a long soak.
- Earlier one-chain-per-axis graphs (8 cycles per row per chain) stalled within microseconds at
  16 Msym/s, with PIO or SRAM pointers. Two chains per axis restore the 16 cycles per row per
  chain that ran clean at 8 Msym/s.

## What this does and does not show

Shown: the peripheral datapath (SRAM pointers, DMA row fetch, four alternating writers,
atomic DDR clock) produces the correct sample sequence at 16 Msym/s with the CPUs free.

Not shown: live data (pointers are an immutable cycle; a live source must write 32 M pointers/s
or use PIO pointer producers, see B6), on-chip FEC at this rate, AFE pad timing, the analog
spectrum (the 8-bit table's modeled upper-mask margin is only +0.026 dB in an ideal-hold model),
EVM, receiver decode, temperature and power.
