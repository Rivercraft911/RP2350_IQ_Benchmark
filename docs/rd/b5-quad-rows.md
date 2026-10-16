# B5: four writers with cyclic SRAM pointers at SYS128

**Stage 1 is implemented and passes native verification. Whether it sustains
16 Msym/s on the Pico is unmeasured.** `bench4quad8` supplies four independent
control/sample chains with immutable SRAM pointers, two writers per axis,
one-symbol OUT8 rows and the atomic GPIO28 DDR clock. The optional `bench4quad`
uses the same graph with two-word 14-bit rows. Neither command overclocks:
both require SYS128 and accept only integer cpw1/2/4.

This is the B5 delivery report, replacing `REPORT.md`. Changes remain uncommitted
on `rd/b5-quad-rows`, based on B4 (`1a81a8cdc4`). No attached Pico, flash operation
or serial port was accessed; no clock, VREG, flash configuration, `bench4fec.c`,
Git metadata or existing measurement evidence was changed. Stage 2 remains a
design and instruction-count estimate because its device gate has not been run.

## Experiment and implementation

The supplied B4 measurements establish the motivation: `bench4live8` at cpw2
was clean for 3 s, while cpw1 stalled both writers after roughly 20 us with healthy
pointer SMs. B5 removes those SMs and gives each output chain twice the row
interval. The supplied measurements are prior evidence, not B5 results.

- [iq4quad.c](../../firmware/src/iq4quad.c) builds four deinterleaved pointer
  rings in **I-even, Q-even, I-odd, Q-odd** order. Each contains 2048 uint32
  addresses and has a natural 8192-byte alignment. A 4096-symbol period uses
  exactly the existing 32 KiB `in_buf` arena. Control DMA increments by four
  bytes and wraps independently; there is no router, pointer FIFO or duplicated
  peripheral DREQ.
- [bench4quad.c](../../firmware/src/bench4quad.c) generates the first 4096
  symbols of B4's geometric 8PSK PRBS using its existing code-stream functions.
  Setup temporarily stores 256 packed words per axis in `cap_buf`. Six preceding
  symbols come from the **tail of that same period**, so history is continuous
  through symbol zero and every wrap. It then replaces the temporary codes with
  the capture buffer. Tables and pointers are immutable during output.
- [dual_dma.c](../../firmware/src/dual_dma.c) adds the experiment-only
  `dual_dma_start_quad_rows()` entry point. Each control channel reads one SRAM
  pointer into its own sample channel's `AL3_READ_ADDR_TRIG`; the sample DMA
  transfers one word (8-bit) or two (14-bit) to its writer FIFO and chains back
  to its control. Each sample channel owns a distinct writer TX DREQ. All four
  control channels start before writer execution, filling the FIFOs.
- The command polls output faults every 128 us, starts a 4096-word stride-17
  pins32 capture halfway through the run, and stops all writers and the clock
  together. Cleanup breaks chains before aborting, retains partial capture
  counts/errors, restores GPIO/priority and releases claims. It checks the
  immutable pointer CRC before restoring the usual PRBS input. Selectors and
  exact pre/post DMA/SM claim masks must restore. A failed resource/selector
  restoration prevents reuse of the borrowed input arena.

The table and scaling are unchanged from [B4](b4-8bit-rows.md). OUT8 drives
GPIO6..13, with GPIO0..5 and GPIO15 held zero, so signed AFE code = 64 times
the signed byte. The 14-bit variant drives GPIO0..13; both generate IQ_FLAG
on GPIO14. The AFE must use two's complement. B4's ideal held-DAC model has
only +0.025890 dB upper-mask margin for the 8-bit table; B5 adds no spectrum,
electrical or receiver measurement.

Existing commands, selectors, row-size rejection and ordinary startup defaults
are preserved. The new sources and commands compile only into experiment-enabled
`iqbench`, not `pvflight`. The immutable API deliberately supplies no live
read/reuse cursor: calling `dual_dma_progress()` in this mode faults and halts;
use `dual_dma_faults()`. Cyclic rings may wrap arbitrarily often between polls.

## Writer schedule and clock

Each axis uses its own four-instruction program, shared by its even/odd SMs:

```text
0: OUT PINS, 8/16  side I=1/Q=0
1: JMP X--, 0
2: SET X, 3 [3]
3: JMP 0 [3]
```

The optional side-set enable is absent on JMP/SET, which leave both the data
pins and IQ_FLAG untouched. Each OUT consumes 8 or 16 OSR bits but maps only
8 or 14 data pins. Initial PCs are `[0,1,2,1]` and X values `[3,4,0,0]` in
stream order. The schedule, in PIO ticks from atomic enable, is:

| Writer | First OUT ticks | Next active symbol |
|---|---|---|
| I-even | 0, 2, 4, 6 | 16, 18, 20, 22 |
| Q-even | 1, 3, 5, 7 | 17, 19, 21, 23 |
| I-odd | 8, 10, 12, 14 | 24, 26, 28, 30 |
| Q-odd | 9, 11, 13, 15 | 25, 27, 29, 31 |

Exactly one writer writes in every bus slot, including the startup handover.
The interposed JMP permits autopull between OUTs. Four joined TX FIFOs provide
eight words each; startup fills them, then explicitly PULLs an OSR before enable.
The deterministic prefill floor is **eight words total per writer** (OSR plus
seven FIFO words); DMA can refill the freed ninth slot asynchronously. Without
any further refill, first starvation from a writer's first active OUT is at
`128*cpw` SYS cycles for OUT8, or `64*cpw` for OUT16. At full nine-word capacity,
those bounds become `144*cpw` and `68*cpw`; the 14-bit active burst explains
why that last number is less than the average-rate estimate of 72.

Four writers occupy PIO2. The two-instruction DDR clock occupies PIO0, capture
PIO1. One PIO1 CTRL set-alias write enables/restarts both neighbouring blocks
on the same cycle. GPIO28 is high on every I slot and low on every Q slot;
`invert=1` reverses only that clock phase. Stop/fault uses one corresponding
cross-PIO disable write. This establishes digital slot parity, subject to the
actual FIFO never stalling; it does not establish pad/AFE setup or hold time.

## First-principles budgets

The read-only main-checkout RP2350 datasheet text (build 2025-07-29) was checked
locally: §2.1 p24 identifies the shared FASTPERI port, §2.1.1 p25 states one
zero-wait access per SYS cycle, §12.6 p1095 gives one DMA read and one write
per cycle across sixteen channels. A DMA transfer here means one read/write
pair. SRAM-to-trigger and SRAM-to-TX each cost **one** FASTPERI access;
PIO-RX-to-trigger would cost **two**. Priority does not increase port capacity.
PIO resource/FIFO limits follow the checked source descriptions recorded in
[B4](b4-8bit-rows.md) and [RP2350 notes](../rp2350-notes.md).

Let `d=cpw`. A symbol lasts `8d` SYS cycles, `Rs=128 MHz/(8d)`, and each
alternate-symbol chain gets `16d` cycles between rows. No timing equation
depends on an absolute overclock; these commands intentionally gate SYS128.

| At SYS128 | cpw2 control | cpw1 target |
|---|---:|---:|
| Symbol rate | 8 Msym/s | 16 Msym/s |
| Physical bus slots/s | 64 M | 128 M |
| GPIO28 / per-axis DAC samples/s | 32 MHz / 32 M | 64 MHz / 64 M |
| Cycles between rows of each chain | 32 | 16 |
| Full 4096-symbol period | 512 us | 256 us |

N4 at 16 Msym/s is 64 MSPS/axis, below the specified 65 MSPS ceiling; the
nominal α=0.20 bandwidth is 19.2 MHz. These are arithmetic, not AFE/RF tests.

| SRAM source, both axes per symbol | DMA transfers | FASTPERI accesses | Ideal DMA/port load cpw1, capture off/on | cpw2, capture off/on |
|---|---:|---:|---:|---:|
| OUT8: two pointers + two sample words | 4 | 4 | 50.000% / 55.882% | 25.000% / 27.941% |
| OUT16: two pointers + four sample words | 6 | 6 | 75.000% / 80.882% | 37.500% / 40.441% |

Capture adds `8/17 = 0.470588` transfers and accesses per symbol, or 5.882353
percentage points at cpw1. Its 4096 observations take about 544 us (1088 us at
cpw2); it adds 7.529412 M FIFO reads/s and 30.117647 MB/s SRAM writes at cpw1.
There is no runtime code-source DMA or pointer-generation work in Stage 1.
SRAM reads at cpw1 are 128 MB/s pointers plus 128 MB/s OUT8 table words
(**256 MB/s total**), or plus 256 MB/s OUT16 table words (**384 MB/s**).
Capture SRAM writes, CPU polls, USB and arbitration are additional traffic.
The read manager remains shared even when SRAM banks differ.

Per chain, OUT8 needs two DMA transfers/two port accesses in 16d cycles;
OUT16 needs three/three. The dependency remains SRAM pointer read → trigger
write → sample read/write → completion chain. The datasheet does not guarantee
its round-trip latency. Doubling the row interval addresses the measured
handoff failure; average utilization and FIFO capacity cannot prove it succeeds.

Resources with the implemented reserved capture service are **six SMs and nine
DMA channels**: PIO2 four writer SMs/eight instruction words/eight DMA channels;
PIO0 one clock SM/two words/no DMA; PIO1 one capture SM/three words/one DMA.
Without capture reservation the graph would need five SMs/eight channels.
No SPI source, credit DMA, pointer SM or router is present. The implementation
reserves capture resources before start even while capture is inactive.

Stage 1 performs no sample arithmetic or pointer publication during output.
Supervision is instrumented with DWT cycle deltas around service work, excluding
the spin wait. As a conservative instruction-based planning envelope, the prior
400–650 cycles/poll corresponds to 2.44–3.97% of core 0 at 128-us polls/SYS128;
this shorter fault-only service may cost less. This is **not a new M33
measurement**. Core 1 is idle. Normal polling adds roughly two FASTPERI reads
before capture, three after completion; even three/128 us is 0.0183% of port
capacity, separate from USB and capture setup.

## SRAM and build layout

No additional large target allocation is introduced. Setup codes and capture
share time, and the pointer rings replace input PRBS storage until cleanup.
The Release experiment ELF map gives:

| Allocation | Address / bytes |
|---|---|
| Reserved output arena / table base | `0x20000000`, 131072 |
| OUT8 table / unused reserved tail | first 65536 / next 65536 |
| OUT16 table | full 131072 |
| Four pointer rings | `0x20020000`, `0x20022000`, `0x20024000`, `0x20026000`; 8192 each |
| Existing link ring | `0x20028000`, 32768; unused by B5 |
| Setup code source | `cap_buf=0x20051fac`, first 2048; temporary |
| Pins32 capture | same base, first 16384; after setup |
| Lower data / BSS | `0x20030110` / 40632, `0x2003a000` / 23248 |
| Minimum heap / additional lower gap | `0x2003fad0` / 1024; 304 bytes remaining |
| Existing high-SRAM allocations | `0x20040000` / 258016; ends `0x2007efe0` |
| Core-0 stack | `0x2007f000..0x2007ffff`, 4096; 32-byte gap below |
| SCRATCH_X code / core-1 stack | 2032 / 2048; 16-byte gap |
| SCRATCH_Y code | 1952; 2144 bytes remaining |

The logical OUT8 table saving stays reserved in `ring_buf`. Tables and pointers
both reside in striped SRAM0–3, whereas capture is in SRAM4–7. Placement and
code/channel numbering can change contention; the prior 31% BCH placement
effect and B2's rare merged-image stalls remain relevant. No new static array
was put in the almost-full high-SRAM arena.

## Native verification

All checks below use host compilation/models and never open USB. Native wall
time is not an M33 cycle measurement.

- [test_iq4quad.py](../../host/test_iq4quad.py) compiles the real portable
  PRBS, packing, table and pointer C with `-Wall -Wextra -Werror`. Sixty-four
  cases cover four seeds, source periods 16..4096 symbols, two table bases,
  both widths and three laps. Every address and decoded sample matches direct
  scalar cyclic FIR. Each case includes 22 invalid no-write calls.
- [test_quad_rows.py](../../host/test_quad_rows.py) executes the actual writer
  opcodes/start arrays across 32 synthetic ring wraps, both widths, cpw1/2/4
  and both clock phases. It checks sole bus ownership, even/odd row order,
  sample/slot order, IQ_FLAG, GPIO28 and OUT8 low zeros. Native production fault
  leaves cover all 65536 raw IRQ masks, 256 output flag combinations, capture
  faults and forbidden progress calls. FIFO service is assumed, not simulated
  as an RP2350 arbiter.
- [test_dual_fifo.py](../../host/test_dual_fifo.py) compiles actual DMA setup
  and stop leaves in ordinary and experiment configurations. It checks contiguous
  independent rings, exact transfer counts and DREQs, alignment/ownership,
  partial captures, AHB errors, claims/clock release and old-mode regressions.
  The B4 fixture now reuses these shared experiment fields without redeclaring
  them; its original setup checks still pass.
- [test_bench4quad.py](../../host/test_bench4quad.py) executes the complete
  production command with real C builders and mocked hardware/time: 44 cases
  cover both widths, cpw1/2, inversion, priority 0/13, JSON varargs, capture,
  admission, setup/runtime/stop faults, immutable-ring corruption and cleanup.
- [test_iq4quad_trial.py](../../host/test_iq4quad_trial.py) has nine tests for
  the independently written scalar FIR/geometry, deinterleaved pointer CRCs,
  all 4096 capture observations, source wrap, low/spare/flag/clock corruption
  with recomputed transport CRC, strict metadata/cleanup and mocked CLI sweeps.
  Cross-checking against the native helper caught and corrected an initial
  host-only axis/parity concatenation error; frozen first/second descriptors
  and CRCs now guard that order independently.

Reproduction is `make build`, the experiment build below, and `make check`.
The full suite includes 82 Python invocations. Both firmware configurations
build without warnings. **`make check` passed all 82 invocations**, including
the new tests and existing encoder, protocol, PIO/DMA and result checks;
`git diff --check` and relative Markdown links also pass. The historical
`plot_waveforms` self-test reports four absent gitignored firmware images:
their previously recorded hashes could not be rechecked here. No new device
evidence is substituted for them.

## Stage 2: coded bits in, live pointers out (design only)

Implement this only after Stage 1 passes device capture and a 60-s run at cpw1.
The proposed upstream sends complete normal **coded** frames: 64800 bits per
22194-symbol pilot-enabled 8PSK PLFRAME. At 16 Msym/s this is **46.715328 Mb/s**,
5.839416 MB/s, one frame per 1.387125 ms. Header/pilot/Gold processing and symbol
mapping still have to run somewhere. Supplying all on-air 3-bit symbol labels
instead would cost 48 Mb/s. Neither number is BB/application throughput.

With PV-SPI's 1316/1332 payload/wire ratio and 10-us interpacket gap, carrying
46.715328 Mb/s requires about **49.479 Mb/s aggregate wire bit rate**, before
extra frame metadata. The existing 32-MHz one-lane source is insufficient.
Equivalent ideal lane rates are at least 24.74 MHz on two lanes or 12.37 MHz
on four; receiver timing, protocol framing and external sender ability need
their own implementation/measurement. B5 does not silently move local FEC or
this ingress work into the cyclic result.

Two explicit pointer-production estimates, following RP2350 §3.7.4.9
pp137–140 (checked locally), are useful. Simple shifts, UBFX/BFI, ORR with
LSL#2 and STR take one nominal cycle; an immediately consumed LDR can add one
result-use cycle. The datasheet's measurements exclude active DMA/cache misses.

1. Straight history update: LSL history by 2; BFI next code into low 2;
   UBFX low 14; ORR aligned table base with history shifted 2 (or 3 for OUT16);
   STR to alternating even/odd pointer destinations; LSR source by 2.
   Six instructions/pointer plus source loads and unrolled-loop overhead:
   **about 6.2 cycles/pointer**, 12.4 cycles per two-axis symbol. Assigning I
   and Q to separate cores costs about 77.5% per core before ingress/mapping
   or supervision. This is a tight planning case.
2. A window-extraction kernel can reduce that estimate. Reverse the order of
   two-bit codes in each 16-symbol packed source word: `RBIT` then swap bits
   within each pair (five simple instructions total). Let `P` be the previous
   reversed word and `V` the current one. For symbols `k=0..5`, form the key
   with `LSR key,V,#(30-2k)` then `BFI key,P,#(2+2k),#(12-2k)`; for `k=6..15`,
   use `UBFX key,V,#(30-2k),#14`. Append ORR-base and STR for each key. This is
   `6*4 + 10*3 = 54` instructions/16 pointers, plus five reversal instructions,
   one load, one previous-word move, one counter decrement and a two-cycle
   taken loop branch. Allowing a load-use cycle gives **65 cycles/16 pointers
   ≈4.1 cycles/pointer**, or about 51% per axis core. Even/odd stores are
   selected by unrolling, with separate postincrement destinations. The window identity was checked for
   16000 random windows, but this kernel is **not implemented or timed**.

Both estimates start with packed axis codes. They exclude coded-bit unpacking,
PL mapping, Gold scrambling, pilots, ingress, cache misses and SRAM contention.
The faster estimate leaves about 7.9 joint cycles/symbol of the sixteen
available across two cores. Prior GCS ingress cost, if it transferred unchanged
to coded frames, would consume another 22.1% of one core; that is a sensitivity
estimate, not a validated ingress implementation. No local-FEC fit is claimed.
Interpolators offer another address-construction option but consume SIO loads/
stores and still need history/packing; no lower timing bound is asserted.

Live SRAM traffic adds **128 MB/s pointer writes** to Stage 1's table/pointer
reads, plus about 8 MB/s reads for packed axis codes, before ingress/mapping.
For OUT8 this is roughly 392 MB/s total SRAM traffic across managers, plus
capture; the global sum is not a per-bank bandwidth guarantee. With a proposed
three-SM/three-DMA ingress source, output+capture+ingress would use nine SMs and
twelve channels, leaving three SMs/four channels before any credit service.
That count assumes an ingress implementation with those resources, not a
working multi-lane receiver.

For an OUT8 implementation, the currently reserved unused 64 KiB table tail
could hold two 8100-byte coded frames, two 11104-byte four-bit axis-code frames
(padded to 16-symbol groups) and small ownership metadata (38408 bytes before
metadata). This is a logical
fit inside existing storage, not a new linker allocation or implemented queue.
OUT16 has no such table tail; a live version must select a different owned
arena and recheck its capture/input coexistence. Keep only bounded chunks of
work on either core so supervision can interrupt publication.

Live ownership must be added explicitly; the present immutable API cannot
publish safely. Give each core its own axis's two rings and retain history
across frame boundaries. Publish whole 256-symbol batches (128 entries per
parity), then a DMB/release ready counter. Sample **four** monotonic control-read
cursors in bounded service, allowing at least eight entries per ring beyond
issued reads before recycling memory. Never infer physical output from them.
The per-ring wrap period is only 256 us at cpw1: service nominally every 64 us,
with a proved maximum gap below 128 us, rather than relying on this cyclic
command's no-cursor polling behavior.

An example software credit policy keeps at least 2176 chronological symbols
ready per axis (1088 per parity) at each successful service: this exceeds
2048 symbols consumed in 128 us plus FIFO/pending-read guards. Stop before the
next interval if the projected ready margin falls below that threshold;
upstream frame/slot credits admit only memory actually owned by the producer.
Readers must never overtake publication, and writers must never overwrite an
issued-but-in-flight read. Initial prefill covers all four rings. This design
needs a real cursor API, measured stop/service bounds and starvation controls.
It provides **software deadline protection**, not finite hardware grants: the
current FORCE-paced chains would otherwise replay stale pointers indefinitely.
A hard finite-grant design needs a separate DMA/PIO gating mechanism and a new
resource/port budget. Do not describe this Stage 1 engine as live or credit-safe.

## Exact coordinator trials

Builds and tests here never contact hardware. From this worktree:

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-b5 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON \
  -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-b5
make check
B5_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-b5/git_rev.h)
```

The explicit stack option on first configure avoids the existing CMake option
ordering issue described in B4. The runner rejects a build without the 4-KiB
stack definition. Load `firmware/build-b5/iqbench.uf2` using the coordinator's
normal procedure; its absolute path is
`/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.claude/worktrees/codex-b5-quad-rows/firmware/build-b5/iqbench.uf2`.
The runner neither builds, flashes nor changes clocks: it issues `info` first
and requires SYS128. Use a fresh output directory for every invocation.

```sh
# cpw2 control + cpw1 decisive target, both priority masks; 3 s each.
python3 host/iq4quad_trial.py --bits 8 --ms 3000 --cpws 2 1 \
  --priorities 0 13 --inverts 0 --build firmware/build-b5 \
  --expected-git "$B5_FW_GIT" --out results/b5-quad-rows/short

# Required negative control: correct data, inverted GPIO28 at both rates/masks.
python3 host/iq4quad_trial.py --bits 8 --ms 3000 --cpws 2 1 \
  --priorities 0 13 --inverts 1 --build firmware/build-b5 \
  --expected-git "$B5_FW_GIT" --out results/b5-quad-rows/inverted

# After short positives pass, repeat with capture halfway through every 60 s run.
python3 host/iq4quad_trial.py --bits 8 --ms 60000 --cpws 2 1 \
  --priorities 0 13 --inverts 0 --reps 3 --build firmware/build-b5 \
  --expected-git "$B5_FW_GIT" --out results/b5-quad-rows/qual-60s

# Cheap optional 14-bit comparison, same clocks/graph and its own FIR reference.
python3 host/iq4quad_trial.py --bits 14 --ms 3000 --cpws 2 1 \
  --priorities 0 13 --inverts 0 1 --build firmware/build-b5 \
  --expected-git "$B5_FW_GIT" --out results/b5-quad-rows/rows14
```

Manual device lines are `bench4quad8 2 3000 0 0`, `bench4quad8 2 3000 0 13`,
`bench4quad8 1 3000 0 0`, `bench4quad8 1 3000 0 13`; replace the third
argument with 1 for inversion, the duration with 60000 for qualification, or the
command with `bench4quad` for 14-bit. Prefer the runner: it preserves source and
binary hashes, raw capture text, per-run records and failed results. It opens
serial only when explicitly invoked by the coordinator.

Positive output must classify **`digital_quad_clean`**, `faults=[]`, exit 0,
and retain the entire requested duration. Expect `writers=4`,
`chains_per_axis=2`, `pointer_source="sram"`, `row_symbols=1`, `period_symbols=4096`,
`pointer_ring_words=2048`, and `sym_rate=8000000.0/16000000.0` at cpw2/1.
Source CRC is 3034056686 and previous words are `[1163384050,335446372]`.
OUT8 table CRC is 1578312796 and pointer CRC 4057852319; OUT16 table CRC is 431963165
and pointer CRC 2212114013. `pointer_after_crc` must equal its initial CRC.
`dac_clock_pio=0`; errors, pointer/output errors and writer TX stalls must be 0.

Every one of 4096 snapshots must match the independent scalar FIR at one fixed
stride 17 cyclic offset, including low data bits, IQ_FLAG and spare 15. GPIO28
must equal IQ_FLAG in every positive snapshot, with both phases observed.
Transport CRC, capture count 4096, selector/PRBS/resource restoration and bounded
128..256-us supervision gaps are mandatory. Inversion must retain **zero data
mismatches** while failing clock parity in **all 4096** snapshots, classifying
`clock_inversion_detected` with no other faults. A stalled run is a failure even
if some captured words happen to match; preserve elapsed time and raw output.
Capture begins halfway through the run, so its start can expose a distinct fault.

Current build identity is `1a81a8cdc4-dirty`; this alone cannot identify dirty
source edits. UF2 SHA256 is
`d1ee7190a0b2b1cbe30a47a00006b01e015931dd0b66e050cb7642eaec3fdad5`, ELF SHA256
`50b31c0ac4125a4c3b7c5a5bba2905d3e713d86dd58bf1feaa078bd3e64a280e`.
Rebuilds after changes may differ; use the runner's retained hashes.

## Risks, rejected routes and delivery

The decisive risk remains DMA latency under simultaneous chains, capture and
SRAM contention. An 8-Msym/s prior pass with one chain is motivation, not proof
that four contending chains meet a 16-cycle deadline. Priority 0 and 13 may behave
similarly with core 1 idle; that does not overturn the measured live-FEC priority
requirement. Rare B2 faults after 40–51 s mean a short pass is insufficient.
The inherited DMA abort loop is unbounded, so hardware stop hangs remain an
existing robustness limitation. GPIO28 stopping at its last level and the lack
of a real clock-driver permission output remain as documented in
[DAC clock notes](../dac-clock.md). AFE capture, RF compliance, temperature and
receiver acquisition are open gates.

Rejected: A2's sole-RX router adds two transfers/accesses per symbol and makes
even OUT8 exceed 100% FASTPERI at cpw1; 14-bit PIO pointers require 8.125 accesses
in 8 cycles (101.563%) before capture. Two chains cannot fix those port bounds.
Duplicating an RX DREQ violates the documented credit rule. Preserving B4's
65536-symbol period as SRAM pointers would need 512 KiB for pointers alone.
Reusing the descriptor four-writer row4/8 programs for row1/2 would give the
wrong bus handover; B5 uses dedicated programs and an explicit entry point.
Live writes into these immutable rings would replay stale/unpublished entries
without the Stage 2 ownership machinery. No clock increase or table retuning
is part of this experiment.

Intended small logical commits for the coordinating engineer (none created):

1. `firmware/src/iq4quad.{c,h}`, `host/native/iq4quad_test.c`,
   `host/test_iq4quad.py`: **Build cyclic even/odd four-level row pointer rings.**
2. `firmware/src/dual_dma.{c,h}`, `host/test_dual_fifo.py`,
   `host/test_iq4writer8.py`, `host/test_quad_rows.py`, `host/test_dacclk.py`:
   **Add independent one-symbol SRAM DMA chains and four alternating writers.**
3. `firmware/src/bench4quad.{c,h}`, `firmware/src/main.c`,
   `firmware/CMakeLists.txt`, `host/iq4quad_trial.py`,
   `host/test_bench4quad.py`, `host/test_iq4quad_trial.py`, `Makefile`:
   **Expose opt-in SYS128 quad-row trials with independent capture validation.**
4. `docs/rd/b5-quad-rows.md`: **Record B5 budgets, native evidence and device gates.**
