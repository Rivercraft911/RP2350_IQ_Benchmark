# A1: live QPSK hardware shaping and frame-order model

Delivered an opt-in live SPI/FEC pointer-output experiment, `dualptr cpw ms source fault`.
It removes CPU descriptor generation and sample arithmetic from shaping. It retains the
existing split FEC arrangement: core 0 does BB scrambling/BCH in bounded slices; core 1
builds TS/GCS BBFRAMEs, runs LDPC and maps PLFRAMEs. **Device frame-parallel FEC and four
pointer-fed writers are not implemented.** The separate even/odd ownership model is
native-tested and explicitly excluded from device builds. This is the highest-value
verified integration step; no new device performance result is claimed.

`make build` passed for iqbench and pvflight without warnings. `make check` passed all
70 command invocations, including the new tests. No Pico was flashed and no device serial
port was opened. Existing result evidence was preserved.

The shared worktree could not be staged: Git reported `Unable to create .../.git/worktrees/
codex-a1-qpsk-hwshape/index.lock: Operation not permitted`. The environment disallows
permission escalation. Commits were instead created in a temporary Git directory using
the integration objects as read-only alternates. **A1-commits.bundle contains the three
commits, including this report; the shared branch itself remains at its original HEAD.**
The first commits are `4c21f30` (live pointer integration/tests) and `0a7713c` (ownership
model/tests). The bundle requires base `8014ed33b93212cc37f9fd1ee1a23f75323b1bcd` and can
be imported by the coordinating engineer, with repository metadata write permission:

```sh
git bundle verify A1-commits.bundle
git fetch ./A1-commits.bundle \
  refs/heads/rd/a1-qpsk-hwshape:refs/heads/rd/a1-qpsk-hwshape-committed
```

The imported branch contains the requested committed artifacts. The existing worktree
files also contain the changes. Build identity in this restricted worktree is therefore
`8014ed33b9-dirty`; a clean checkout of the imported branch gets its own commit identity.

The implementation reuses [bench_dual.c](../../firmware/src/bench/bench_dual.c), including its actual
SPI prefill, BB queues, sliced frontend and ordered backend. The command fixes normal
5/6, pilots, L8/N4, packed mapper and streaming LDPC packer. It requires `bchkernel 2`,
`mapkernel 1`, `ldpcpack 1`; the runner additionally selects `bchbank 1`. Existing commands,
default selectors and autonomous startup behavior remain unchanged. Writer/row/schedule/
priority selectors are saved and restored. GPIO28 clock selection remains explicit.

The existing FIFO output backend accepts two writers only. Both axes consume eight-word
rows from the L8 table. `iqquadptr` produces the addresses in two pin-free PIO SMs; its
control DMA byte-swap is enabled for the L8 address representation. The 64 KiB table has
CRC `0xccad7f5a`. The existing two-writer cyclic engine sustained 16 Msym/s, so this is a
credible component to integrate, but adding live SPI/FEC traffic can change its ceiling.

Ownership in the implemented device path is precise:

1. Core 1 alone consumes the stateful PV TS/GCS adapter and writes the two BB queue slots.
   Core 0 acquires a completed BB slot, copies it into its FE slot, then releases that BB
   slot. Core 0 writes FE slots in sequence; core 1 acquires completed FE slots in sequence.
2. Core 1 alone writes `in_buf` through one persistent `symstream_t`. A normal pilot frame
   has 33,282 symbols, so each frame advances the partial-word phase by two symbols.
   There is no frame-boundary padding. Core 1 publishes `encoder.total` only after mapping
   all complete words, with a DMB before the publication. It releases FE storage afterward.
3. Before starting a backend frame, core 1 reserves a conservative whole-frame extent:
   `stream.n + frame_words + 1 - consumed <= 8192`. Core 0 publishes `consumed` from
   `iqquadptr.reusable_words`, the lower of the two source-read issue totals minus eight
   words. The encoder acquires that release before reuse. An issued high cursor never
   releases storage. The 32-bit encoder totals stay well below rollover during 60 s.
4. Core 0 acquires `encoder.total` and passes only that immutable completed prefix to
   `iqquadptr_publish`. Credits authorize complete 512-word batches. A zero sentinel is
   installed before its preceding valid grant is released. Both axes retain their own
   DMA cursors, while one shared grant ring follows the slow axis. The producer API rejects
   decreasing publication or publication more than 8192 words ahead of guarded reuse.
5. Faults stop output writers and the DDR clock before releasing pointer resources. The
   SPI consumer is joined before stopping its ingress service. If pointer abort times out,
   its claims remain held and buffer restoration is suppressed; `restored=false` fails
   host validation. No input storage is overwritten while that DMA might still own it.

Core 0 polls every 128 us and rejects gaps above 256 us. BCH slices here use 96 words,
leaving more margin than the existing 128-word descriptor-path slices. At SYS128, the L8
PIO program needs at least 49 cycles per packed 16-symbol source word. The conservative
maximum issue bound over a 257 us timer interval is `8 + ceil(128e6*257e-6/49) = 680`
words. Healthy publication requires at least 1360 granted words ahead of the high issue
cursor. Cursor wrap is independently bounded by `(8192-8)*49/128e6 = 3132.94 us`; the
API's integer deadline is 3132 us. These are bounds on source reads, not physical output.
Two prefilled frames provide 4160 complete words and 4096 granted words at startup.
Live refill must maintain the runway; the program stops if it cannot. Poll/credit guards
are checked during the run, not inferred from the final waveform snapshot.

The resource counts below come from the actual PIO arrays and allocation paths, including
capture and the on-chip SPI source. Each block has four SMs and 32 instruction words.

| Block/service | SMs | Instruction words | DMA channels |
|---|---:|---:|---:|
| PIO0: two paired I/Q writers | 2 | 4 | 4 (two control, two sample) |
| PIO1: SPI RX, CS/READY, emulator | 3 | 19 | 3 |
| PIO1: sparse capture | 1 | 3 | 1 |
| PIO2: two L8 pointer SMs | 2 | 13 shared | 4 (two input, two credit) |
| PIO2: GPIO28 DDR clock | 1 | 2 | 0 |
| Total | 9 / 12 | PIO0 4, PIO1 22, PIO2 15 | 12 / 16 |

PIO1 has no free SM. The pointer SMs start before output; the existing atomic cross-PIO
start enables the PIO0 writers and PIO2 DDR clock together. It leaves the pointer SMs
running. The capture SM remains PIO1's fourth SM. Four pointer-fed writers would require
8 output DMA channels, making 16/16 total, with writers in PIO2 and pointers/clock in
PIO0. That fits the raw budgets, but alternating row ownership is missing: two DMA readers
must not race on one RX FIFO/DREQ. Simply removing the backend's two-writer check is unsafe.

No permanent data arena was added. The inspected iqbench link map has:

| Storage | Address / extent |
|---|---|
| Aligned symbol source `in_buf` | `0x20000000..0x20008000` (32 KiB) |
| Existing sample arena `ring_buf` | `0x2001f8d8`, 128 KiB |
| Aligned L8 pointer LUT within it | `0x20020000..0x20030000` (64 KiB) |
| `bb_buf` | `0x20050000`, 8108 bytes |
| Existing `cap_buf` | `0x20051fac`, 56 KiB |
| Capture portion, 4096 words | `0x20059fac..0x2005dfac` |
| FE1 portion, 2027 words | `0x2005dfac..0x2005ff58` |
| Existing FE0 | `0x2006a090`, 8108 bytes |

The old cap-buffer input overlay is unused; `link_ring` is also unused by this experiment.
SPI slots still borrow `cfg.ti`; its emulator and BB0 borrow `cfg.tq`; BB1 uses `bb_buf`.
After the reserved 1 KiB heap, low striped SRAM has 680 bytes free in iqbench and 136 in
pvflight. High striped SRAM has 4464 bytes free; scratch X has 16 and scratch Y 304 bytes
before their reserved 2 KiB stacks. The new bench's own generated stack frame is 680 bytes
including saved registers; nested calls/interrupts need additional stack. Addresses are
build observations, not fixed ABI. Existing hot encoder code and BCH bank selection remain
in place. Pointer supervision and the new orchestration are XIP/cache code, as in the
existing pointer benchmark. No extra hot code was forced into the nearly full low SRAM.

The first-principles rates and estimates are as follows. At SYS128/divider 1, the bus emits
128 M 16-bit I/Q words/s, each axis gets 64 MS/s, GPIO28 is 64 MHz, and N4 gives 16 Msym/s.
At divider 2 all these rates halve. 64/65 = 98.46% of the AFE sample limit; ideal N4 maximum
is 16.25 Msym/s. AFE7071 SLOS789C p.27 was checked: its single differential DDR mode latches
I/Q on rising/falling edges and bypasses the FIFO. Pad timing and RF performance remain
outside this digital experiment.

At 16 Msym/s, sample DMA does 64 M 32-bit transfers/s: 256 MB/s of table reads plus
256 MB/s of FIFO writes. Address DMA adds 8 M transfers/s, packed input DMA 2 M 16-bit
transfers/s, and SPI RX/emulation approximately 1.64 M transfers/s. Credit traffic adds
about 3906 transfers/s. Total is approximately 75.64 M DMA transfers/s, 59.1% of an ideal
128 M single-read/single-write budget, before arbitration and control-register effects.
During pins32 capture, `128e6/17 = 7.53 M` additional transfers/s last approximately
544 us. That raises this simple utilization bound to about 65.0%. Average bandwidth does
not guarantee a FIFO handoff deadline. L8 pointer execution is 49/128 = 38.3% of each
pointer SM's available cycles at this symbol rate; its ideal unpaced ceiling is 41.8 Msym/s.

M33 cycle estimates use the instruction timings in [rp2350-notes.md](../../docs/rp2350-notes.md),
not native timing. Disassembly gives approximately 236 instructions for the healthy pointer
cursor/fault/publish and output-fault leaves, plus roughly 80–130 outer/copy instructions
and amortized grant work. Charging taken branches, result-use penalties and uncertain
multiregister operations gives **400–650 cycles/poll**, or **2.4–4.0% of core 0** at 128 us.
XIP misses, IRQs and SRAM/FASTPERI arbitration are excluded from that estimate. The separate
cyclic pointer benchmark measured approximately 554 cycles/poll / 3.38%, a useful prior
observation rather than a new live-pipeline measurement.

The existing optimized GCS pipeline measured 45.2% core-0 BCH work and 82.0% core-1 work.
Keeping that split suggests **roughly 48–50% core 0 and 82% core 1** after pointer integration;
TS may retain roughly 94% core-1 load. Memory placement, shorter slices and new DMA traffic
can change those numbers. Current `core0_work_busy` times supervision+BCH, excluding idle
spin and some loop overhead; the control's `core0_active_busy` has broader instrumentation.
Report both fields and stage costs, rather than treating their difference as a precise
measured saving. All waiting cores spin; spare time is not measured sleep or power savings.

For frame-parallel FEC, 16e6/33282 = 480.740 frames/s and 128e6/480.740 = 266256 cycles
of one-core wall budget per frame. Isolated optimized FEC is 295922 cycles/frame, hence
295922/(2*266256) = **55.57% per core before ingress and supervision**. The integrated
measured GCS BB+BCH/LDPC+map/ingress work sums to approximately 127% of one core. Balancing it,
then adding supervision, suggests approximately **62–66% per core before a staging copy**.
The 55–60% complete-pipeline target is therefore not established by the supplied stage sum.

Native verification is reproducible with `make check`. The new tests are:

- `host/test_dualptr_native.py`: compiles actual production prefill, BB queue and frontend
  leaves with real portable PV adapters and DVB-S2 FEC. A deterministic scheduler models
  the existing backend, skewed DMA readers, guarded source reuse and actual credit leaf.
  Each of TS and GCS matches 33,282 complete packed words through sixteen distinct frames
  against independent Python, crossing four ring lengths and all eight pilot-frame carry
  phases. Freeze controls consume exactly 14,336 authorized words and stop. This models
  event ordering, not instruction timing or simultaneous bus arbitration.
- `host/test_frame_order.py`: real C FEC/maps plus a two-slot atomic ownership model match
  independent Python for ten varied 5/6 frames, pilots off/on. It forces odd-before-even
  completion, backpressure, ring wrap, stale/wrong owners, exact guards, counter rollover
  and continuous tails. LDPC calls remain serialized in the native harness.
- `host/test_dualptr_trial.py`: nine tests cover both sources/rates, all 4096 independent
  waveform/clock observations, final-sample corruption with valid serial CRC, inverted
  clock, finite-credit starvation, unrelated faults, nonfinite CPU/rate fields, SPI/BB
  provenance and four-writer control validation. The runner uses today's
  `dual_trial.validate_capture` / `dual_spi.reference_window`, without discarding bad samples.
- Existing pointer tests exhaust L8 keys and histories, PIO semantics, credit/sentinel
  interleavings and stop lifecycle; existing DAC-clock/FIFO tests cover their shared backend.

The coordinating engineer can run the following from this worktree or a clean checkout of
the imported branch. The build directory is `firmware/build`; load its `iqbench.uf2`
(absolute path in this worktree ends
`.claude/worktrees/codex-a1-qpsk-hwshape/firmware/build/iqbench.uf2`). Hardware loading is
left to the coordinator. The runner neither builds nor flashes. Use a fresh output folder
for every invocation, and the exact git identity from the binary that was loaded:

```sh
make build
make check
A1_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build/git_rev.h)

# 3 s: GCS at 16 Msym/s, plus the optimized existing four-writer control.
python3 host/dualptr_trial.py --ms 3000 --clocks 128000 --cpws 1 --sources 6 \
  --faults 0 --clock-modes 1 --control --expected-git "$A1_FW_GIT" \
  --build firmware/build --out results/dualptr/a1-gcs-3s

# 3 s: both live inputs, 8/16 Msym/s, correct and inverted clocks, grant starvation.
python3 host/dualptr_trial.py --ms 3000 --clocks 128000 --cpws 2 1 --sources 5 6 \
  --faults 0 1 --clock-modes 1 2 --expected-git "$A1_FW_GIT" \
  --build firmware/build --out results/dualptr/a1-matrix-3s

# 60 s: complete GCS pipeline, same-rate control and hardware-pointer experiment.
python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 1 --sources 6 \
  --faults 0 --clock-modes 1 --control --expected-git "$A1_FW_GIT" \
  --build firmware/build --out results/dualptr/a1-gcs-60s

# 60 s: TS qualification after its short run passes; same-rate control included.
python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 1 --sources 5 \
  --faults 0 --clock-modes 1 --control --expected-git "$A1_FW_GIT" \
  --build firmware/build --out results/dualptr/a1-ts-60s
```

The runner issues `dualprio 0`, `dualwriters 4`, `quadprofile 1`, `bchkernel 2`,
`mapkernel 1`, `ldpcpack 1`, `bchbank 1`, `clock 128000`, `dualclock 1`. Its positive
commands are `dualptr 1 3000 6 0` / `dualptr 1 60000 6 0`; source 5 selects TS.
The control is `dualstream 1 <ms> 2 <source> 4096 0 0 0 4 8`. For a compact manual run,
send those selector commands via `python3 host/iqbench.py raw '<command>'`, then send the
benchmark; use the runner to retain and validate the actual captures. The runner restores
`dualclock 0` and SYS128 afterward. Its `manifest.json` hashes firmware, validation sources
and the built UF2/ELF; captures and run rows are retained even when qualification fails.

Pass criteria: positive rows classify `digital_live_clean`, errors/pointer/output faults
are zero, full requested duration elapses, runtime BB/FEC traffic advances, independent
BBFRAME CRCs and accepted-message CRC chain match, every one of 4096 snapshots matches
one fixed independent waveform offset, and GPIO28 equals IQ_FLAG in every snapshot with
both phases present. Poll gap is at most 256 us, minimum healthy credit runway at least
1360 words at SYS128, neither axis reads beyond granted words, selectors/buffers restore.
The control must classify `cpu_descriptor_control_clean`. Load reduction is an outcome
to record, not a condition that excuses waveform or ownership errors.

Negative controls: `dualclock 2` plus `dualptr ... fault0` must retain correct waveform
data while all 4096 clock snapshots fail parity, classifying `inverted_clock_detected`.
`dualclock 1` plus `dualptr ... fault1` freezes credits at one quarter of the requested
duration and must stop early on pointer `SOURCE_EMPTY` (bit 3), with a null-trigger IRQ
and no read beyond the frozen grant. Terminal owned TX stalls are permitted; unrelated
DMA/ownership/configuration/poll faults are rejected. Classification is
`credit_starvation_detected`. These controls run separately. The host validator also has
a data-corruption control: the final waveform sample is changed and serial CRC recomputed;
the independent waveform check still rejects it. Runner failure exits nonzero.

The precise remaining frame-parallel work is:

1. Contextize LDPC workspace and profiling without altering the legacy wrapper. `P` and
   fallback `lpar` are shared today, and device `dvbs2_prof` writes also race. For fixed
   normal 5/6/q30/pack1, one additional private workspace needs 32*12*4 + 32*4 = 1664 bytes
   (rounded live matrix plus transpose tile); pack1 avoids `lpar`. The 4464-byte high-bank
   slack or an idle borrowed arena can hold it, but code placement and stack must be
   checked again. Verify each context against the current serial/Python references.
2. Give core 0 even and core 1 odd frame generations, each with its own BB/FE/workspace.
   Stateful SPI consumption needs an input turn: the tested model releases that turn only
   after a complete BBFRAME is copied, allowing the other core's input/FEC to overlap.
   Slot states are FREE -> INPUT -> ENCODING -> READY -> COPYING -> FREE, sequence-tagged;
   reuse advances the sequence by two. All stage ownership uses acquire/release atomics.
3. The native model's safe baseline maps into two caller-owned staged PLFRAMEs. A single
   publisher acquires only exact `next_output`, reserves `(fill+symbols)/16` complete ring
   words against guarded reuse, appends exact symbols into one persistent stream, then
   releases the completed prefix and staging slot. Odd completion cannot release a gap.
   Two staged outputs cost 2*2081*4 = 16648 bytes. Either unused side of the aligned 64 KiB
   table within the 128 KiB arena has at least 32 KiB contiguous space, enough for both.
   Avoid double-counting the eight-word guard when passing pointer `reusable_words`.
4. That baseline copy is expensive: roughly 16 instructions/full word, estimated 18–24
   cycles/word, or 37–50k cycles/frame. It adds about 8 MB/s of staging write/read traffic
   and 14–19% of one core at 16 Msym/s. A single publisher could reach roughly 75–85% core 0
   even with otherwise balanced FEC. Test it as a correctness milestone, not as evidence
   for the 55–60% target. For lower load, use an ordered mapping token to append directly
   into the shared stream, or separately prove disjoint direct-map intervals with boundary
   carry repair. The latter is a new ownership proof and is not covered by this model.
5. Bound core-0 LDPC and mapping steps so pointer supervision remains under 256 us.
   Whole LDPC (~111k cycles / 0.87 ms) or whole FEC (~296k / 2.31 ms) is too long. Expose
   resumable LDPC group/accumulation/pack and PL-map chunks; prove checkpoints, tail state
   and publication under adversarial interleavings. Simply relaxing the poll budget to
   1 ms is insufficient with today's two-frame startup and conservative runway proof.
6. Add a separate frame-parallel command/flag, preserve this split path as the control,
   then repeat the exact 3 s/60 s capture, clock, starvation and ordering controls. Only
   claim balanced measured load after those pass. Extend to four writers separately with
   explicit alternating FIFO ownership; PIO/DMA counts alone do not prove that handoff.

Dead ends and open questions: unsynchronized dual LDPC corrupts shared scratch; independent
streams cannot both mutate one partial symbol word; flushing each frame inserts fourteen
extra symbols; four output readers cannot arbitrarily share two pointer FIFOs; whole-frame
core-0 FEC violates supervision timing; a staged publisher can consume much of the intended
saving. These are why the device path stops at verified split-FEC integration. Native tests
cannot establish live FIFO handoff margin, IRQ latency, stack high-water, SRAM contention,
thermal/long-run behavior or AFE/RF performance. The 96-word slices and two-writer live bus
need the coordinator's measurements. If the short run faults, preserve its evidence and
compare the same-rate four-writer control; do not disable guards to obtain a positive row.

Input evidence was read from [README](../../README.md), [DAC clock notes](../../docs/dac-clock.md),
[RP2350 notes](../../docs/rp2350-notes.md), [optimized encoder measurements](../../results/optimizations/2026-10-15/README.md),
[waveform campaign](../../results/waveforms/2026-10-15/README.md),
[DDR clock campaign](../../results/dac-clock/2026-10-15/README.md), pointer campaign summaries and
the memory-placement A/B/A record. Measured priors above retain their original scope;
all new cycle/load predictions and event models are estimates or simulations.
