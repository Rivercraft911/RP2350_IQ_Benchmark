# B4 8-bit rows on the Pico: clean at 8 Msym/s, hand-off-bound at 16 Msym/s

Pico Plus 2, RP2350B A2, USB only, SYS128, GPIO28 DDR clock, image `1a81a8cdc4`
(`rd/b4-8bit-rows`, experiment build with `CORE0_STACK_SRAM_HI=ON`; `flash-quick.txt`).
Cyclic 8PSK PRBS, B4's 64 KiB 8-bit four-level L7/N4 table, seven-instruction PIO pointer
SMs, one control/sample chain and one OUT8 writer per axis, priority mask 13. Every run checks
4096 stride-17 GPIO0..31 snapshots against B4's independent 8-bit scalar FIR reference and
GPIO28 against IQ_FLAG (`host/iq4hist_trial.py --bits 8`).

| 3 s run (`quick-1/`) | Symbol rate | Row interval per chain | Result |
|---|---:|---:|---|
| cpw2 | 8 Msym/s | 16 SYS cycles | clean, 0 / 4096 bus and 0 / 4096 clock mismatches; core 1 idle, supervision 2.1% |
| cpw2, inverted clock | 8 Msym/s | 16 | detected (4096 / 4096 clock mismatches, data exact) |
| cpw1 | 16 Msym/s | 8 | **writer TX stall on both axes after ~20 us**; pointer SMs healthy (`pointer_txstall_mask` 0) |
| cpw1, inverted clock | 16 Msym/s | 8 | same stall |

The 16 Msym/s failure is the one-symbol-row DMA hand-off limit seen in every earlier record
at 8 cycles per row per chain, not the port budget or the pointer program.

`quad1-probe.jsonl` records that the existing four-writer descriptor engine rejects
one-symbol rows (`four writers require four- or eight-word rows`), which motivated B5
(`rd/b5-quad-rows`, `docs/rd/b5-quad-rows.md` on that
branch).

Not established: AFE pad timing, spectrum, EVM, receiver decode, temperature, long soaks.
