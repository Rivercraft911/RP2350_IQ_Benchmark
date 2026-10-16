# C6: packed 8PSK mapper with fixed shifts

**Built and verified natively; device cycles are unmeasured.** New option bit **32** selects
a 73-instruction loop per 32 data symbols, versus C4's 94, using the same 512-byte dilation
table. It also batches 32 input words per C3 mapper step instead of eight. The instruction
model gives about **53.3k cycles for data alone**, and a provisional **60–75k cycles for a
complete normal PLFRAME on one uncontended core**. The requested **≤60k measured mapping
stage is not established**. Concurrent instruction/data fetch can put it above that range.
No host timing is used as M33 timing evidence.

Worktree `rd/c6-map8`, 2026-10-16. No device or serial port was accessed, no flashing command
was run, and Git metadata and existing result records are unchanged. `make check`, ordinary
build and experiment build pass. Defaults and old mapper selections retain their output bits.
Compare options within this image: larger borrowed contexts change addresses from older images.

## Implementation and selection

[The leaf](../../firmware/src/dvbs2_map8_asm.S) retains the existing chronological bit-plane
algebra. For three deinterleaver columns `a,b,c` and Gold planes `r0,r1`:

```text
w  = (a & (b ^ c ^ r0)) ^ r1
qa = b ^ w; qb = c ^ w ^ r0; d = a ^ r0
Q  = spread(qa) | (spread(qb) << 1)
I  = Q ^ (3 * spread(d)) ^ 0xaaaaaaaa
```

Each sixteen-bit plane half becomes sixteen chronological two-bit axis codes. FE remains
canonical and read-only: normal columns are three aligned 675-word intervals; reversed 3/5
swaps pointers. Three RBITs per input word preserve the existing Gold representation. Gold
already restarts from a prepared sequence every frame; there is no new per-frame LFSR saving.

The C4 leaf needs 11 stack loads per 32 symbols, dynamic shifts and inner-loop ring indexing.
C6 retains four input pointers, LUT and two advancing output pointers in registers. One I
carry lives on the stack; Q carry stays in a register. Eight loop copies use immediate shifts
for fills 0,2,…14. C3's frame adds two symbols modulo sixteen, its header adds ten, and each
pilot adds four, so these fills cover its entire schedule. The upper-half lookup reuses a
consumed plane register to preload I carry and hide two dependencies.

[The wrapper](../../firmware/src/dvbs2_8psk.c) clamps assembly calls to contiguous pairs of
output words. One pair crossing a ring edge uses C4 for that input word. Odd fills also use
C4; short columns retain the general extractor, including their 24-bit tail. PLHEADER and
pilots remain identically prepacked. Deferred first words, partial carries, ring masks and
ordered publication are preserved. No materialized interleaver or extra frame buffer is made.

`dvbs2_8psk_codes_c6_init()` prepares both leaves in one caller-owned arena;
`dvbs2_8psk_codes_begin_c6()` selects C6. Its first 484 words retain C4's layout. Metadata
words226–227 hold the second function pointer; old initialization clears it. Requesting C6
without preparation fails without changing mapper state. The native C6 leaf uses the portable
algebra; the separate instruction-semantics test checks the actual M33 source.

In `c3fec`/`c3pair`, **bit32 overrides bit2**. `C4_ALL` stays31: default `c3pair` remains31,
default `c3fec` remains0; accepted masks extend to63. Compare **27 versus59** (all except
scratch-Y BCH, toggle bit32), **31 versus63**, or **2 versus34** (mapping alone). Bit32 includes
the new leaf and larger slices: device A/B must not attribute all savings to assembly alone.
All masks reserve the same context, and both leaves are prepared for either mapper selection.

## Instruction and cycle budgets

Counts come from GCC Arm14.2.1/objdump and the source instruction model. They are not device
timing. Checked local RP2350 datasheet build2025-07-29: §3.7.4.9 pp137–144, especially
result delays, shifts, register loads/stores and instruction fetch (.1/.2/.5/.6/.12), consistent
with [project notes](../rp2350-notes.md). Standalone LSR has no result-use penalty. A complex
ORR followed by STR of its result does not incur one either: stored data is not an address
source operand. Charging either case as a dependency overestimates this leaf.

| Per 32 data symbols | C4 | C6 |
|---|---:|---:|
| Scalar loop instructions |94|73|
| Input / LUT loads |5 /12|5 /12|
| Stack loads / stores |11 /4|3 /2|
| Axis output stores |4|4|
| Copied instructions |440 B|2,516 B|
| Leaf stack including saved registers |compiler dependent|64 B|

C6 executes16 input/Gray/Gold instructions,38 dilation/axis instructions,16 carry/store
instructions and three loop instructions. Approximately six extra zero-wait cycles remain:
three dilation load/join dependencies, one low-half I-carry load use, one end-pointer load use
and one additional taken-branch cycle. Thus **79 ×675 =53,325 cycles** is a data-loop planning
value, not a measured result or rigorous bound. Scalar data instructions total49,275.
Entry/exit adds31–44 scalar instructions per run depending on fill, plus multi-register
transfer cycles. Fetch stalls and inter-core contention are additional.

With pilots and no ring edge, C4's eight-word slices require85 step calls and98 leaf runs;
C6's32-word slices require22 calls and36 runs. Header/pilot insertion, stream copies, caller
branches and C3 dispatch remain charged. Charging those gives the provisional60–75k complete
stage estimate. A32-word data slice is about2,528 cycles,19.75us at SYS128, before constants
and dispatch. Even several thousand extra cycles leave a plausible margin to the existing
128-us service gate; there is no worst-case interrupt/contention proof. Record measured
`stage_max_cycles`, `max_slice_cycles` and `max_service_gap_us`.

At16Msym/s,22,194symbols ×8SYS cycles/symbol ×2 frame owners gives **355,104 cycles per assigned
frame per core**. Replacing historical99.6k mapping by60–75k changes the supplied2/3 stage sum
348k to about308–323k. 5/6's26k+106k+135k+99.5k becomes327–342k. Those combine historical
measurements with an estimate. Add supervision, startup and the measured concurrent-stage
penalty before assessing fit. They do not establish a complete16-Msym/s transmitter.

## Table alternatives and dead ends

Unbuilt rows below count explicit scalar extraction/merge constructions per32symbols. They
are neither compiler results nor optimality bounds. Sizes exclude unchanged constants/Gold.

| Approach | Table bytes | Work and decision |
|---|---:|---|
| C3 SWAR shuffle/dilation |0|114 compiled loop instructions. Four shuffle stages and repeated masks dominate packing. Retained control.|
| Byte dilation, selected |512|12 extracts +12 halfword reads +6 joins +2 Q joins +6 I operations =38 packing/map operations. Optimize the surrounding loop.|
| Nibble dilation |16|24 extracts +24 reads +18 joins +8 axis operations =74, adding36 operations to save496bytes.|
| Two-plane nibble zip plus byte d dilation |768|About54 packing/map operations; two-nibble index construction loses to byte spreading.|
| Two-plane byte zip plus byte d dilation |131,584|About34 packing/map operations, only four saved. Does not fit beside contexts or the live OUT8 table.|
| Four-symbol direct Gray/axis map, no Gold |8,192|Three extracts +two joins +one read per four symbols =48 index/read operations, then about28 to split/join dense axis bytes, before Gold.|
| Same, sparse32-bit entries |16,384|About eight output-assembly operations/32 instead of28, but exceeds remaining live context space; Gold still costs work.|
| Four-symbol direct map including Gold |2,097,152|12 FE bits +8 Gold bits index16-bit entries. About64 index/read operations with precombined Gold bytes, before axis assembly. Too large.|
| Two-symbol direct map including Gold |1,024|Six FE bits +four Gold bits index one byte.16 lookups cost about128 index/read operations with precombined Gold nibbles, before axis assembly.|
| Eight-symbol direct byte-column map |67,108,864|24 FE bits index32-bit entries, before Gold. Not an on-chip table.|

For the unrotated direct table, prepacked Gold can apply rotation with two mask loads and
roughly seven ALU operations per16symbols (18 operations/32). Keeping both Gold representations
doubles data Gold storage from8 to16bytes/32symbols. Combining Gold into four-symbol tables
instead needs up to256 rotation variants, giving the2-MiB table above. A fixed configuration
does not make these variants free.

Keeping FE words MSB-first, preparing a second reversed Gold copy and using a reversed-byte
dilation table would save three RBITs/word,2,025 instructions/frame. Roughly5.4KiB extra Gold
plus a second table is a poor trade here; mutating shared Gold also spoils clean A/B.

Interpolators were considered, not allocated. Checked source: §3.1.10 pp44–46 and CTRL SHIFT
descriptions pp69–70. One dilation scheme feeds rotated plane bits into an accumulator,
uses lanes for masked indices/rotating feedback, then reads four POP addresses per plane.
Across qa/qb/d: three RORs +three accumulator writes +12 SIO address reads +12 SRAM LUT reads
=**30 instructions and15 SIO accesses**, versus24 CPU extract/read instructions. Setup,
preservation and address dependencies add cost. Register relief does not justify it after
the assembly rewrite.

## SRAM and placement

Only existing borrowed buffers grow their contents; no static table, DMA channel, PIO SM,
PIO instruction or interpolator ownership is added. Gold/constants are shared read-only;
job states and stacks are private. The512-byte LUT and384-byte normal constants are unchanged.
The arena grows1,936 → **5,008 B**, retaining C4's1,024-byte instruction reservation and
appending **3,072 B** for C6 (2,516used,556spare). Both copies remain available for A/B.
The copied section includes a 2,514-byte function and two bytes of alignment padding.
Code stays in striped SRAM, away from the single banks contested by BCH, transpose and core1's
stack.

Release experiment ELF/map/DWARF addresses, same reservations for all masks:

| Object / region | Address and bytes |
|---|---|
| `ring_buf` |`0x20000000`,131072|
| C3 context, unused OUT8 tail |`0x20010000`,57272;8264bytes left in64KiB|
| C3 jobs |`0x20010000`, `0x20014bf0`;19312bytes/job|
| C3 Gold / mapper arena / LDPC arena |`0x20019834` / `0x2001add4` / `0x2001c164`|
| C3 C4 / C6 entries, Thumb bit omitted |`0x2001b164` / `0x2001b564`|
| Pair low A Gold / LUT / C6 / LDPC |`0x20004bc4` / `0x20006164` / `0x200068f4` / `0x200074f4`|
| `cap_buf`, high lane B base |`0x20051fac`,57344|
| Pair high B Gold / LUT / C6 / LDPC |`0x20056b70` / `0x20058110` / `0x200588a0` / `0x200594a0`|
| High rings / high-layout total |`0x2005b0a0`,16384 /53492, leaving3852bytes|
| Lower `.data` / `.bss` |`0x20030110`,40880 / `0x2003a0c0`,23280|
| Minimum heap / lower remainder |`0x2003fbb0`,1024 / **80 B**|
| High static allocation / core0 stack |`0x20040000`,258024 / `0x2007f000`,4096;24B gap|
| SCRATCH_X code / core1 stack |2032 /2048;16B remain|
| SCRATCH_Y code |3688;408B remain|

Both `pvflight` builds link too: lower heap ends`0x2003fff8` (8B remain), high static allocation
ends`0x2007eea8`, actual core0 stack remains`0x2007f000..0x20080000`. Further static allocations
need both images rechecked. Pair low storage is74728B, below its hammer at98304B. High-copy
and transpose hammers do not overwrite layout3's candidate code.

Existing pair layout1 moves lane B data, Gold, LUT and code together. New opt-in **layout3**
retains layout0 data, tables, rings and LDPC code and changes only the shared C6 function
pointer to the high copy; it requires bit32. JSON includes `map_lut_a/b` and `map_leaf_a/b`.
Existing layout2 still moves only LDPC code. Retain actual addresses with device measurements.

C6 data traffic/32symbols:20B FE/Gold +24B LUT +16B output +20B stack =**80B**, versus120B for
C4 including spills. At16Msym/s that is about40MB/s, versus60MB/s, before setup and instruction
fetch. SRAM fetch still contends. DMA's10–18-cycle row loop is unchanged, and C3's two FEC
workers still conflict with B6's dedicated core1 grant worker during live integration.

## Verification

Executed without hardware:

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
make check
```

Both targets in both builds pass without introduced warnings. `make check` passes, including
the new assembly check and all existing native/model/reference checks. Targeted results:

- **540 mapper frames +108 deferred frames**: baseline/C4/C6 modes versus the old signed-plane
  C mapper/converter and independent Python PL/Gold reference. Normal2/3,3/4,5/6, reversed3/5,
  short frames, pilots on/off, Gold extremes, all16fills, tiny rings, deferred/out-of-order
  completion, zero budgets and no-write failure admission pass.
- **Actual M33 source semantics:240cases,692leaf calls,383930instructions**. Scalar Gray phase
  and quarter-turn tables form the oracle, independently of mapper equations. All even fills,
  exhaustive label × Gold rotations, split calls, reversed columns, full675-word patterns,
  carry/n wrap, bounds, guards,64-byte stack and AAPCS preservation pass. The ring model also
  checks1040 crossing-pair fallbacks; native tests check production C fallback. Compiled entry
  is at offset0, all branches local, section2516B and no relocations. This is not Arm execution.
- **64option masks ×three rates ×16frames** through the real C3 graph: every pair's physical
  rings/carry versus Python, odd-first completion and wraps. The entire mask sweep also passes
  ASan/UBSan. Production command mocks include32/59/63, cadence/draining, PRBS/selector
  restoration and negative controls. Host elapsed times have no M33 interpretation.

## Coordinator commands and qualification

The following device commands were **not run here**. Use one experiment image for every A/B;
record ELF/UF2 hashes, source diff and map. The Git revision alone does not identify an
uncommitted image. Keep SYS128.

First isolate mapping, then concurrent FEC. For code5 (2/3):

```text
c3pair 5 40 0 0 27
c3pair 5 40 0 0 59
c3pair 5 40 0 1 27
c3pair 5 40 0 1 59
c3pair 5 40 0 2 27
c3pair 5 40 0 2 59
c3pair 5 40 1 2 59
c3pair 5 40 3 2 59
```

Repeat these lines with first argument6 (3/4) and8 (5/6). Layout0/1/3 separates shared lower,
split lanes and high mapper-code-only placement. Optionally repeat31/63 for scratch-BCH
interaction; keep that bit equal in A/B. Run off/on/off again to expose drift. Pass: equal A/B
`crc_a/b`, all requested jobs complete, expected layout addresses and reduced `avg_stage.map`
on both cores. The performance target additionally requires **≤60000 map cycles/frame**;
record a miss honestly.

Exercise every fill and independently check ring CRCs using the updated existing runner:

```sh
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 5 6 8 --cpw 2 --ms 3000 --options 27 59 \
  --gap-us 128 --out results/c6-map8/coordinator-cpw2-ab
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 5 6 8 --cpw 1 --ms 3000 --options 27 59 \
  --gap-us 128 --out results/c6-map8/coordinator-cpw1-ab
```

Raw equivalents: `c3fec 5 3000 0 2 128 27`, `c3fec 5 3000 0 2 128 59`, then cpw1 and codes6/8.
Qualification needs zero errors/lateness/dropped frames, independent ring CRC equality and
`max_service_gap_us <=128`. Retain stage maxima and divide map stage cycles by that core's
completed frames. Follow positives with60000ms at27/59. At cpw1 the inherited startup deadline
may fail before steady state: that is failed full cadence, not a measurement of isolated map
cost. Do not relax the service gate and call it qualification.
Use fresh output paths if those names already exist, and run `stackcheck` after the sweep
to retain device stack-watermark and code-integrity evidence.

Negative control in a separate fresh directory:

```text
c3fec 5 3000 5000 1 128 59
```

Expect deadline error bit1, positive lateness, stopped new releases and drained active jobs;
their CRCs still match Python for the published frame count. The runner accepts this expected
negative with `--slow-us 5000 --options 59`. An error-free negative means cadence validation
is broken. All these are CPU-only tests; FIFO/DMA live-output qualification remains separate.

## QPSK and remaining risks

For the same globally pi/8-rotated four-level output, QPSK is simpler. After the existing
interleaved-domain Gold swap/flip, let `x` hold chronological I signs in even bits and Q signs
in odd bits. Then `I_codes = x ^ 0xaaaaaaaa` and `Q_codes = adjacent_bit_swap(x)`. No mapper
LUT is required. An unbuilt construction is about23scalar instructions/16data symbols with
immediate packing, or27–31cycles with ordinary hazards. For32400data /33282total symbols
with pilots this suggests **60–75k cycles/complete QPSK frame**. Its SYS128/16Msym/s alternate
budget is532512cycles. This is design arithmetic, not a tested QPSK change; the current binary
QPSK shaper wants a different representation, so this is not its drop-in replacement.

Risks: two-core instruction/data contention, eightfold code duplication, tiny static headroom
and longer slices. Toolchain or ABI changes require the copied-section checks again. Odd
fills and short columns intentionally gain no new data-loop speedup. Larger direct tables,
extra Gold and interpolators were rejected for the costs above. Sub-60k device mapping,
complete16-Msym/s cadence and live integration all remain unproven.

## Intended small commits (not created)

1. `firmware/src/dvbs2_8psk.{c,h}`, `firmware/src/dvbs2_map8_asm.S`,
   `firmware/CMakeLists.txt`, `host/test_dvbs2_8psk_codes.py`, `host/test_c6_map8_asm.py`,
   `Makefile` — **Add opt-in fixed-shift packed 8PSK mapper and independent native checks.**
2. `firmware/src/c3_fec.{c,h}`, `firmware/src/bench_c3_{fec,pair}.c`, `firmware/src/main.c`,
   `host/c3_fec_trial.py`, `host/native/c3_pipeline_test.c`, `host/test_c3_{pipeline,bench}.py`
   — **Expose C6 bit32, larger mapper slices and isolated mapper-code placement diagnostics.**
3. `docs/rd/c6-map8.md`, `docs/rd/README.md` — **Document C6 budgets, rejected designs and
   coordinator A/B qualification.**
