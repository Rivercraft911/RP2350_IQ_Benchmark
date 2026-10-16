# R3: review of the hot and shared paths

Reviewed `main` at `0296275`. Method: static reading of the firmware and validators listed
below, plus two CMake configures in a scratch directory (configure only) to confirm D1. No
Pico, serial port, picotool or flash access. No firmware or host code was changed.

**Result.** I found no defect in the DMA/PIO ownership, credit-grant, abort or cleanup logic of
the hardware-shaped engines (`dual_dma`, `iq4ptr`, `iq4prod_dma`, `iqquadptr`'s stop path),
nor in the cross-core handoffs of `bench4fec`, `bench_dual` (`dualptr`) and `c3fec`. No
validator builds its reference from firmware output. The confirmed defects are in the build
configuration, one diagnostic, the validators' capture-position checks and session hygiene:

| # | Severity | Finding |
|---|---|---|
| D1 | Medium | `CORE0_STACK_SRAM_HI` is read before it is declared, so a fresh `make build` reserves 2 KiB, not 4 KiB, for core 0; the default `firmware/build` (both `pvflight` and `iqbench`) was in that state |
| D2 | Low–Medium | `iq4prod_dma` keeps mutable state in `.scratch_y`, so `stackcheck` reports corrupted scratch code after any `bench4prod8*` run |
| D3 | Low–Medium | `dualptr`, `dual_trial` (SPI sources) and `iq4fec` validators trust the firmware-reported capture position; a capture taken at the wrong time, or one PLFRAME off, still passes |
| D4 | Low–Medium | `dualptr_trial` leaves seven experimental selectors set, and `txs2`/`pvtx`/`stream` records report only one of them |
| D5 | Low | `iqout` keeps two PIO0 SMs and three DMA channels for the rest of the boot; `bench4prod8*` and `dualptr4` then refuse to run |

The most important risk is provenance (R1): every validator checks firmware identity only when
`--expected-git` is given, and `git describe --dirty` does not distinguish two dirty builds.

Severity key. **High**: corrupts output or passes a broken run in current use. **Medium**: wrong
configuration or provenance in current builds, or a latent corruption path with a short
trigger. **Low**: diagnostics, hygiene, or needs an earlier failure first.

## Confirmed defects

### D1 (Medium): fresh configures get a 2 KiB core-0 stack

`firmware/CMakeLists.txt:67` tests `CORE0_STACK_SRAM_HI` inside the per-target loop to add
`PICO_STACK_SIZE=0x1000`, but `option(CORE0_STACK_SRAM_HI ... ON)` is only declared at line 110.
On the first configure of a build directory without `-DCORE0_STACK_SRAM_HI=ON` the variable is
undefined at line 67, so the 4 KiB define is omitted, while line 111 (after the `option`) does
move the stack into RAM_HI. A second configure of the same directory sees the cached `ON` and
adds the define. The image therefore depends on configure history.

Evidence:
- Fresh `cmake -S firmware -B <scratch> -DCMAKE_BUILD_TYPE=Release` (the `make build` line):
  `build.ninja` has no `PICO_STACK_SIZE`; the generated `memmap_iq.ld` has `__StackTop` at the top
  of RAM_HI. Re-running configure on the same directory adds `PICO_STACK_SIZE=0x1000` to all 290
  compile lines.
- The main checkout's `firmware/build` (local, untracked) has `CORE0_STACK_SRAM_HI:BOOL=ON` in its
  cache but no 4 KiB define. Its `pvflight.elf.map` and `iqbench.elf.map` show `.stack_dummy`
  size `0x800`, `__StackBottom = 0x2007f800`. `firmware/build-exp` (configured with the flag)
  shows `0x1000`.
- Measured core-0 depth is 2,312–2,360 bytes
  ([round-2 stack report](../../results/round2/2026-10-15/STACK.md)); core 1 peaks at 608 of
  2,048 bytes in the C4 sweep.

Why nothing breaks today: the link-time `ASSERT` keeps only `.stack_dummy`'s 2 KiB clear, but in
the current `pvflight` layout `.sram_hi` happens to end at `0x2007eec0`, 4,416 bytes below the
stack top. The 2.36 KiB stack spills into the unused reservation gap.

Failure scenario: `.sram_hi` in the default build grows by 2,056–2,368 bytes (one more table).
The `ASSERT` still passes, and the deepest measured stack (`0x2007f6c8`) overwrites the end of
`.sram_hi`, silently, in the flight image. Separately, `stackcheck` on these images paints only
2 KiB and reports `paint_exhausted: true` for normal deep prints, so it cannot distinguish this
from a real overflow. The B5/B6 runners already refuse such builds
(`host/iq4quad_trial.py:208`, `host/iq4prod_trial.py:375`); `iq4fec_trial`, `dualptr_trial` and
`dual_trial` do not check.

Fix: move the `option(CORE0_STACK_SRAM_HI ...)` line above the `foreach` at line 60. Also check the
`__StackTop` replacement (R9) and update the stale comment at lines 64–65 ("Keep both 2 KiB
stacks"). After the fix, reconfigure `firmware/build` and record the change in the images' stack
size.

For reference, `build-exp`'s `.sram_hi` ends at `0x2007efe8`, 24 bytes below its 4 KiB stack
reservation. RAM_HI has essentially no slack in the experiment image.

### D2 (Low–Medium): `bench4prod8*` makes `stackcheck` report corrupted code

`firmware/src/output/iq4prod_dma.c:33` places the mutable `producer_state_t state` in
`.scratch_y.iq4prod_state`. The SDK script collects `*(.scratch_y.*)` between
`__scratch_y_start__` and `__scratch_y_end__` and loads it from flash. `stack_report`
(`firmware/src/app/main.c:221-233`) compares exactly that range with its flash image. In the
`build-exp` map, `state` occupies `0x20081d30–0x20081e68`, offset 3,376 of 3,688 bytes.

Failure scenario: after any `bench4prod8` or `bench4prod8live`, `prepare` writes `state`, and every
later `stackcheck` in that boot reports `code_matches_flash: false` with
`first_corrupt_code_offset` ≥ 3376. The integrity check is then useless for the rest of the boot,
and a real overwrite of the last 312 bytes would be indistinguishable. No recorded stackcheck in
`results/` follows a B6 run, so this has not yet produced a misleading record.

Fix: put the state in a separate data output section after `__scratch_y_end__` (a one-line
linker patch beside the existing ones), or give `stack_report` an explicit code-end symbol.
Moving it to `.sram_hi` is not possible: RAM_HI has 24 bytes free in `build-exp`.

### D3 (Low–Medium): capture position is trusted, not checked

The sparse capture is the only content check on the output. The validators use the capture
position the firmware reports to choose the reference window, but they don't confirm that
position against the run time or against the pipeline depth:

- `host/dual_trial.py:138-178` (used by `dualptr_trial.py:137`) builds four PLFRAMEs around
  `cap_symbol_cursor` and accepts the best of *any* offset in that 133k-symbol window.
- `host/iq4fec_trial.py:107-124` bounds the window to the reported `cap_symbol_lo/hi` ±256
  symbols (tight), but never checks that `cap_symbol_lo` corresponds to `ms/2`.
- `host/iq4prod_trial.py:333-336` does check the capture cursor against time (±1,024 words).

Failure scenarios: (a) a regression that triggers the capture at the first poll instead of at
`ms/2` reports a consistent early cursor; the 4,096 snapshots then verify the two prefill frames
encoded before the timed run, and every other check (runtime frame counts, SPI CRC chain,
counters) still passes. (b) For SPI QPSK, a dropped or duplicated PLFRAME before the capture
moves the data by exactly one frame, which still aligns inside the four-frame window.

Fix: in all three validators, require (i) the aligned symbol within the producer-to-pin depth of
the cursor (a few hundred symbols, not a frame), (ii) the cursor within a stated tolerance of
`sym_rate × t_capture`, and (iii) the captured frame index ≥ the prefill frame count.

### D4 (Low–Medium): experimental selectors outlive the session

The selectors `bchkernel`, `mapkernel`, `ldpcpack`, `bchbank`, `dualwriters`, `dualprio`,
`quadprofile`, `dualclock` and `clock` are firmware globals that persist across commands.
`host/dualptr_trial.py:297-302` restores only `dualclock 0` and `clock 128000`, leaving
`dualprio`, `dualwriters 4`, `quadprofile 1`, `bchkernel 2|3`, `mapkernel 1`, `ldpcpack 1` and
`bchbank` set. `host/dual_trial.py:673-685` restores most but not `ldpcpack` or `bchbank`.
`tx.c`'s result (`firmware/src/app/tx.c:445-454`) reports `bch_kernel` but not `map_kernel`,
`ldpc_pack` or `bch_bank`.

Failure scenario: a `pvtx`/`txs2` measurement later in the same boot runs the opt-in mapper and
LDPC packer, and its record attributes the `s2_busy` to the defaults. The output bits are
identical by design, so this corrupts cycle attribution, not the waveform.

Fix: restore every selector the session set in `finally`; print all encoder and output selectors
in every result line; optionally add a `defaults` command.

### D5 (Low): `iqout` never releases its claims

`firmware/src/output/iqout.c:115-121` claims two PIO0 SMs, three DMA channels and the `DMA_IRQ_0`
handler on the first `tx_run` and never releases them. Afterwards `bench4prod8*` (requires PIO0
empty, `firmware/src/bench/bench4prod.c:49`) and `dualptr4` (requires all 16 DMA channels free,
`firmware/src/bench/bench_dual.c:716-721`) refuse with a generic resource error until reboot. It fails
closed, but it makes command order matter: `dual_trial`'s baseline runs `stream` and `txs2`
first. Fix: add `iqout_release()` at the end of `tx_run`, or have those prechecks count only
what they claim.

## Risks (no observed failure)

**R1 (Medium): firmware identity is optional and not unique.** All five runners compare `git`
only if `--expected-git` is passed (`iq4fec_trial.py:151`, `dualptr_trial.py:66`,
`iq4prod_trial.py:237`, `iq4quad_trial.py:145`, `dual_trial.py:521`). `git_rev.cmake` uses
`git describe --always --dirty`, so different dirty builds report the same string, and a
modified tracked file anywhere in the repository (results, docs) marks an unchanged firmware as
dirty. The
manifests hash the `--build` directory's UF2, but nothing ties it to the flashed image. Default
`--build` paths point at stale directories (`build-b1`, `build-limits`). Failure: a run without
`--expected-git` after flashing another image records the wrong hashes and passes. Fix: emit a
build ID (GNU `--build-id` or a hash of sources and flags) from `info` and every result. Have each
runner always compare it with the `--build` ELF and refuse dirty identities unless explicitly
allowed.

**R2 (Low–Medium): dangling LDPC leaf pointer.** `dvbs2_ldpc_prepare` stores the copied mode-1
leaf in the global `ldpc_group_prepared` (`firmware/src/dvbs2/dvbs2.c:496,511`) and nothing clears
it. `c3fec` copies the leaf into the borrowed `ring_buf` tail (`bench_c3_fec.c:42,169`), and
`c3pair` copies it into `ring_buf` itself (`bench_c3_pair.c:47,156`). `c3pair`'s default options
(`C4_ALL`) include `C4_LDPC`. After either command the next `tx_run` overwrites `ring_buf` with
samples, and the guard `mode && !ldpc_group_prepared` (`dvbs2.c:539`) no longer protects
anything. Today every mode-1 caller prepares first, so this is latent: a future caller that
skips `prepare` would execute sample data. Fix: clear the pointer at the end of each bench, or
drop the global and pass the leaf per job (`c3_job_t.ldpc_group` already exists).

**R3 (Medium for a flight port, Low for the bench): unbounded waits without a watchdog.**
`iqout_start` spins until the TX FIFO fills (`iqout.c:179`), `tx_run` joins core 1 with
`multicore_fifo_pop_blocking` (`tx.c:405`), and `dual_dma_stop` waits for ABORT without a limit
(`dual_dma.c:1029`). `iq4ptr`, `iqquadptr` and `iq4prod_dma` bound the same abort to 1 ms and
keep their claims. `pvflight` enables no watchdog, so any of these hangs needs a power cycle.

**R4 (Low–Medium): capture coverage is very sparse.** 4,096 snapshots at a 17-bus-word stride
cover 8,704 symbols: 1.09 ms of a 60 s run at 8 Msym/s, 0.54 ms at 16 Msym/s, and one bus word
in 17 within that window. Content errors that cause no stall or ownership flag outside that
window are invisible (for example, a wrong code word converted in `bench4fec`). A cheap
complement: CRC every published code word or group in firmware (one pass over data already in
registers) and compare it with the host reference over all runtime frames.

**R5 (Low): `iqout_stop` does not follow the SDK's RP2350-E5 sequence.** `iqout.c:184-209` breaks
the ping-pong chain by setting `CHAIN_TO` to self, but leaves `EN` set and aborts the two channels
one at a time. The SDK 2.2.0 `dma_channel_abort` comment asks to clear `EN` on the aborted
channel and any channel that chains to it. `dual_dma`, `iq4ptr`, `iqquadptr`, `iq4prod_dma` and
`pvspi` do that. Breaking the chain first probably suffices, and no failure has been seen. This
is the default and `pvflight` stop path, so align it with the shared helper (S1).

**R6 (Low): LUT cache after a failed cleanup.** `tx_shaper` rebuilds `tab_i`/`tab_q` only when the
coefficient set changes (`tx.c:77-81`). `dualptr` borrows both tables for PV-SPI and rebuilds them
only without `ERR_CLEANUP` (`bench_dual.c:875-876`). After such a failure, later `tx_run`s report
`tables_ok: 0` but still transmit. Fix: invalidate `cur_set` whenever a bench borrows the tables,
and refuse to start with `tables_ok` false.

**R7 (Low): core-1 joins carry no task identity.** On a 1 ms join timeout `iq4prod_dma_stop`
returns without popping (`iq4prod_dma.c:341-352`). The worker's late completion token then
satisfies the next unrelated join early, for example `tx.c:405`. This happens only after an
`ERR_STOP`. Fix: tag tokens with a sequence number, or drain the FIFO when no task is
outstanding.

**R8 (Low): missing consumer-side barrier in `tx.c`.** The producer reads `*run.avail` and then
`link_ring` without a `__dmb()` (`tx.c:152,185-194`); `bench4fec` and `bench_dual` include the
acquire barrier. This is safe on the in-order Cortex-M33 but not guaranteed by the Armv8-M
memory model.

**R9 (Low): unchecked linker patches.** `CMakeLists.txt:118` verifies the `.stack_dummy` move but
not the `__StackTop` replacement (lines 115–117). If a future SDK changes that line, the
reservation moves to RAM_HI while SP stays at the top of SCRATCH_Y, with no configure error. The
`.waveform_ring` and `.input_ring` insertions are also unchecked; the 128 KiB alignment is
covered at run time (`bench4fec.c:153`).

**R10 (Low): `clock` lower bound.** `main.c:495` accepts any reachable frequency from 1 kHz,
for example `clock 48000`. `docs/rp2350-notes.md` lists RP2350-E12 as "USB needs
clk_sys ≥ 1.1 × clk_usb" (not re-verified here), which would put the lab image out of USB
reach. Bound it to ≥ 53 MHz or to the qualified set.

**R11 (very low, relevant to a flight port): long-run counters.** `SEQ_IDLE = 0xFFFFFFFF`
collides with block sequence 2³²−1, after about 6.4 days at 8 Msym/s (`iqout.c:86,95`).
`c3_job_begin_options` computes `index × 22194` in 32 bits, which wraps after 193k frames
(`c3_fec.c:64`). Neither is reachable in ≤ 60 s commands.

## Simplification and consolidation

- **S1. One E5-safe teardown helper.** The claim-time `prepare_channel` exists four times
  (`dual_dma.c:425`, `iq4ptr.c:105`, `iq4prod_dma.c:129`, `iqquadptr.c:95`) plus inline in
  `pvspi.c:166-172`. The "clear EN and CHAIN_TO, one ABORT mask, bounded wait, check AHB errors,
  cleanup" teardown exists in five variants (`dual_dma.c:1007-1043`, `iq4ptr.c:314-349`,
  `iqquadptr.c:334-374`, `iq4prod_dma.c:357-381`, `pvspi.c:296-302`), and `iqout` has a sixth
  that differs (R5). One helper with a timeout would also fix R3's unbounded abort.
- **S2. `iq4ptr.c` is a near-clone of `iqquadptr.c`.** Credit grants, cursor sampling, fault
  sampling, halt and stop are about 250 lines of shared logic. Factor out a credit-gated pointer
  producer core.
- **S3. Small duplicates.** `iq4prod_seed` is identical to `iq4ptr_seed` (`iq4prod.c:3`,
  `iq4ptr.c:4`). `claims()` is duplicated in `bench4prod.c:27` and `bench4quad.c:27`. The inline
  64-bit timer read appears three times (`tx.c:139`, `bench4fec.c:59`, `bench_dual.c:72`).
- **S4. Selector save/restore.** The same block, with the same "set writers to 2 first" ordering
  trick, is repeated in `bench4fec.c:163-172,321-323`, `bench4prod.c:53-72,163-165`,
  `bench4quad.c:51-72,137-140` and `bench_dual.c:736-737,872-874`. A
  `dual_dma_selectors_save/restore` pair would keep that rule in one place.
- **S5. Dead code.** `bench_c3_fec()` (`bench_c3_fec.c:139`), `dual_dma_start()`
  (`dual_dma.c:548`), `dvbs2_frontend_begin_reentrant()` (`dvbs2.c:941`) and `c3_job_begin()`
  (`c3_fec.c:52`) have no callers in firmware or host. The 256 MHz branches are unreachable
  because `clock` caps at 150,000 kHz: `bench4prod.c:44`, `bench_c3_fec.c:149`,
  `bench4live.c:37`, and `iq4prod_trial.py --clock-khz 256000`.
- **S6. One validator core.** Manifest and hash collection, build checks, identity, capture parsing
  and alignment are re-implemented in each runner, with drifting rules: window width (D3), stack
  check (D1), identity (R1) and selector restore (D4). A shared module would fix those four
  together.

## Checked and found sound

- `dual_dma_stop` (`dual_dma.c:995-1099`) stops writers and DDR clock in one PIO1 CTRL write and
  latches the capture count before ABORT. It clears `EN` and `CHAIN_TO` on every owned channel,
  aborts with one mask, checks AHB errors afterwards, then restores bus priority, GPIO/pad
  state, HSTX and its clock. All owned channels use `IRQ_QUIET`, so raw `INTR` is a valid fault
  signal.
- Credit grants (`iqquadptr_grant_until`, used by `iq4ptr`) write the next sentinel before the
  grant. A late publisher therefore produces a detected null trigger (`SOURCE_EMPTY`), never a
  replay. Reuse uses the lower axis's issued-read cursor minus an 8-word guard. Ring-wrap
  ambiguity is caught by the time-based `POLL_GAP` limit derived from the PIO's fastest rate
  (145 or 112 cycles per word).
- The `iq4prod_dma` worker's seqlock snapshot is correct. It grants only after `BUSY` clears, and
  `publish` rejects decreases and overruns. The live source (`iq4prod_source`) gives exactly one
  large and one small axis per symbol.
- `bench4fec` bounds conversion by the last polled `reusable_words`. The encoder-converter
  handoff has release and acquire barriers. Cleanup order is output, producer, encoder join,
  SPI stop, buffer restore, and `in_buf` is not refilled if a DMA abort failed to retire.
- In `bench_dual`'s split FEC only core 1 uses the shared LDPC scratch (`P`, `lpar`); core 0
  uses the reentrant frontend. `dvbs2.c`'s reentrant frontend and LDPC steps write no shared
  state. Running degree-128 codes through the 160-bit leaf is correct because table words 4–5 are
  zero.
- `dvbs2_8psk.c`: the ring-wrap pairing in `map_normal`, the deferred boundary word for
  frame-parallel publication, and the LUT arena layout (224 words, then a leaf of at most
  1,024 bytes).
- `pvspi`/`pvproto`: `pvspi_next_packet` never blocks; a slot is released only after its last
  packet has been copied, so the in-place CRC-8 rewrite never touches a re-armed slot. The ISR and
  consumer are serialised by a spinlock, and pause/stop break DMA chains before ABORT. Every
  caller joins the consumer before `pvspi_stop`.
- `tx.c`: `input_ready` is wrap-safe. The encoder's ownership bound leaves one word of slack even
  when `ring.done × biw` wraps to 0. The capture wait is bounded (free-running sampler), and
  output stops before the core-1 join.
- Host references are independent of the firmware. `iq4fec` uses the Python FEC, geometric 8PSK
  codes and a once-rounded Q38 FIR. `iq4prod` live uses its own xorshift model and checks the
  final ring CRC. `iq4quad` uses geometric codes and an independently built pointer CRC.
  `dual`/`dualptr` go from pattern packets through `reference/dvbs2` to the FIR. Firmware-reported
  reference parameters (rate, row format, profile) are pinned to the request.
- No variable with an initialiser lives in the NOLOAD `.sram_hi`, `.waveform_ring` or
  `.input_ring` sections. Every NOLOAD buffer is written before use.

## Not covered

- No device run, no RF. D1 was confirmed by configure output and existing map files. The other
  findings rest on code reading and are not measured.
- Not reviewed in full: `iqquadptr.c` apart from its halt/stop/fault paths, `iq4hist.c`,
  `link.c`, `bench_c3_pair.c` apart from its leaf handling, all assembly kernels, and the other
  host runners.
- RP2350-E5 was checked against the SDK 2.2.0 `dma.h` comment, not re-read in the datasheet
  errata. E12 is taken from `docs/rp2350-notes.md`.
