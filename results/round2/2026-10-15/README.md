# Round 2: combined image, DMA read priority and the core-0 stack fix

**Results on one merged image** (`rd/round2-merge`: B2 + C2 + A2 on `rd/round2-base`, with
the core-0 stack moved out of SCRATCH_Y; `e13b0026c3`, experiment build, `qual-60s.uf2`,
`qual-60s-flash.txt`). Pico Plus 2, RP2350B A2, SYS128, on-chip PV-SPI master at 32 MHz,
GPIO28 DDR clock, every positive run checked against independent references (4096 sparse
GPIO0..31 snapshots, GPIO28 == IQ_FLAG):

| 60 s | Rate / BB capacity | Core 0 | Core 1 | Result |
|---|---|---:|---:|---|
| QPSK 9/10, hardware pointers (`dualptr`, two writers, mask 13), GCS | 16 Msym/s / 27.94 Mb/s | 45.6% | 82.8% | clean; inverted clock detected |
| Same, TS input | 16 Msym/s / 27.94 Mb/s | 45.6% | 97.0% | clean; inverted clock detected |
| QPSK 9/10, CPU descriptors, t=8 BCH (`bchkernel 3`), TS | 16 Msym/s / 27.94 Mb/s | 81.9% | 92.2% | clean |
| Same, GCS | 16 Msym/s / 27.94 Mb/s | 81.8% | 79.5% | clean; wrong-row, starvation and inverted-clock controls detected |
| 8PSK 5/6 on the four-level table (`bench4fec`, mask 13) | 8 Msym/s / 19.38 Mb/s | 24.9% | 89.6% | **writer TX stall at 40.9 s** (passed 4×60 s on the B2 image) |

`q1-*/`, `q2-*/`, `q3-*/` hold those runs. [STACK.md](STACK.md) explains the hangs found on
the way: a pre-existing core-0 stack overflow into SCRATCH_Y code, exposed by C2's new code.

## The 8PSK stall is a margin problem, not the stack move

`b2-ab/` repeats 60 s `bench4fec 2 60000 0 13` on three images, reflashing before every run:

| Image | Core-0 stack | 60 s runs clean | Stall times |
|---|---|---|---|
| B2 `2081cb740b` (`orig-b2.uf2`) | SCRATCH_Y | 2/2 (6/6 including results/b2-8psk-live) | — |
| merged `e13b0026c3` (`fixed-sramhi.uf2`) | SRAM4-7 | 1/2 (1/3 with `q2`) | 47.2 s, 40.9 s |
| merged `e13b0026c3`, same source (`fixed-scratchy.uf2`) | SCRATCH_Y | 0/2 | 49.0 s, 50.9 s |

Both stack placements stall on the merged code, so the merge (A2's shared output-path
changes, channel numbering or code layout) reduced the margin; the stack move did not
cause it. A stall rate of roughly one per 40–50 s also means the B2 image's six clean
runs do not prove a long soak. One DMA chain per axis needs a new 2-word row every
16 SYS cycles per axis at 8 Msym/s; giving each writer twice that time (A2-style
alternating rows for the one-symbol table) is the obvious next experiment.

## Four pointer-fed writers (A2, `dualptr4`)

3 s probes on `9a3fb1b860` (`m1-*/`, `m2-*/`): clean at 8 Msym/s (core 0 25.6%). At
16 Msym/s every writer stalled: after 0.17 ms with mask 0, and at exactly 1.50 s with mask
13, the moment the sparse capture DMA starts (about 7.5 M extra transfers/s with all 16
channels in use). The simpler two-writer path with DMA read priority is the qualified
16 Msym/s design; A2's router remains an option if capture-free operation needs margin.

## What the priority mask does

`dualprio`/`prio` sets the BUS_PRIORITY register fields PROC0, PROC1, DMA_R, DMA_W (mask
bits 1, 2, 4, 8; RP2350-E27 changes which ports some affect). Pointer-fed writers with one
DMA chain per axis need DMA reads above core 1 (masks 12/13); see
`results/b2-8psk-live/2026-10-15/README.md`. The same setting slows core 1 by about 6
points, which matters for core-1-bound profiles (TS input at 16 Msym/s).

Not established: AFE pin timing, spectrum/EVM, receiver decode, temperature, long soaks.
