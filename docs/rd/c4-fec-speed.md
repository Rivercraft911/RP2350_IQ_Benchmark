# C4: cheaper concurrent FEC and packed 8PSK mapping

C4 adds independently selectable LDPC, packed mapping, BCH and fused GCS/BB and cached-deadline options to C3. **Native correctness is verified; C4 device cycles and the SYS128 16-Msym/s fit are unmeasured.** The objective remains at most about 320k cycles per assigned alternate frame, including supervision, and slices below 128 us. Instruction reductions are evidence for trying the device experiment, not proof that this objective is met.

This report replaces `REPORT.md`. The worktree is `rd/c4-fec-speed`, based on C3 `994f6aa458`. No Pico or serial port was accessed, no image was flashed, and no clock, VREG, flash configuration, `bench4fec.c`, Git metadata or existing result evidence was changed. The normal build and old APIs retain their selections; the new command argument defaults to zero. Build layout changes can still change timing, so compare options within the **same C4 image** before comparing with historical C3 measurements.

## Selection and implementation

The command is `c3fec code ms [slow_us] [cpw] [gap_us] [options]`. `options` is a bit mask:

| Bit | Option | Off | On |
|---|---|---|---|
| 1 | LDPC | C3 reentrant C group and generic packer | Shared immutable SRAM instruction arena, immediate rotation kernels, specialized q60/q30 packing |
| 2 | Mapper | C3 SWAR shuffle/dilation | Byte-dilation table, prepacked PLHEADER/pilots and SRAM data leaf |
| 4 | BCH | C3 bank0 streamed t10 / flash C t12 | Read-only scratch-Y streamed t8/t10/t12 leaves |
| 8 | GCS/BB | Generate BB, then copy/XOR/clear the whole frame | Generate and XOR directly into FE words; clear remaining words in 64-word slices |
| 16 | Deadlines | Recompute the same 64-bit quotient each service | Cache each publication/retirement deadline until its frame advances |

Use 0 for C3, 1/2/4/8/16 to isolate each change, 15 for all compute changes, 31 for all changes, and 30/29/27/23/15 to remove one option from the combined path. The benchmark preserves packed I/Q ring format, even/odd frame ownership, deferred boundary words, ordered publication, absolute timer deadlines, selector restoration and PRBS restoration. Setup and table/code copies occur before the timed interval. Both jobs and every arena keep the same placement for every mask; disabling an option does not shrink or move the contexts.

[LDPC](../../firmware/src/dvbs2.c) now has `dvbs2_ldpc_begin_reentrant_mode()`. Mode0 retains C3. Mode1 captures a prepared function pointer, owns its matrix and stack scratch, and shares only initialized read-only tables and instructions. [The PIC Thumb leaf](../../firmware/src/dvbs2_ldpc_asm.S) extracts a 360-bit information group directly into a duplicated bit window, avoiding C3's extra temporary copy. A TBH dispatch selects one of 32 immediate rotations. Each shifted row word uses two loads, two shifted XORs and one store, instead of two separate shifts plus merge/XOR. The zero rotation has its own path. The 5,580-byte aligned section (5,578 copied bytes) contains its dispatch table and has no external code/data relocation. `dvbs2_ldpc_prepare()` copies it into caller-owned SRAM and issues DSB/ISB before either worker begins. Prepare and reconfiguration are idle-only operations.

Normal2/3's q60 packer writes eight columns as fifteen words, following the old fast pack60 order. Normal5/6's q30/16-bit-offset packer uses a fixed sixteen-column pattern; its eight-column tail ends exactly on a word boundary. Other supported q values keep the general streaming packer. Neither core writes the legacy global `P`, `fe`, `lpar` or profiling checkpoints. The public concurrent path also supports QPSK FEC; QPSK PL mapping and ordinary transmit scheduling are unchanged.

C3's slowdown relative to old pack1 was not a different LDPC code: its group arithmetic and transpose were moved into flash, with a large inlined step dispatcher, dynamic append branches and independent matrices. Old pack1's matrix work ran from striped SRAM. Slicing adds hundreds of calls but cannot alone explain the measured 40–60k difference. Mode1 removes instruction work and changes fetch placement together; its A/B result cannot assign an exact fraction to each cause. SRAM instruction fetches can contend with the matrices and future output DMA.

[The mapper](../../firmware/src/dvbs2_8psk.c) retains the existing bit-parallel Gray/Gold algebra. With chronological input planes a,b,c, `w=(a & (b ^ c ^ r0)) ^ r1`, `qa=b^w`, `qb=c^w^r0`, and `d=a^r0`; packed `I=Q ^ 3*spread(d) ^ 0xaaaaaaaa`. A 256-entry uint16 byte-dilation table replaces repeated perfect-shuffle stages. The PLHEADER and every pilot's packed codes are prepared once for the fixed MODCOD/pilot/Gold configuration. Gold already restarted from a precomputed sequence in C3; C4 does not charge a fictitious per-frame LFSR saving. Columns remain reads from the canonical FE bit vector, rather than a materialized interleaver. Short frames and reversed3/5 columns keep bit-exact fallback handling.

[BCH](../../firmware/src/dvbs2_asm.S) adds the missing degree192 continuation leaf. It uses the existing four 256-row, eight-word-stride tables and six register-held remainder words. Option4 selects scratch-Y code per frontend state; it does not change the global bank selector during concurrent work. Degree160 uses the existing byte-for-byte scratch copy, so its benefit is solely a placement question. Degree128's existing specialized leaf is reusable by QPSK. Both cores may execute immutable scratch code concurrently with the core0 stack in high SRAM. Cold 24-byte `bch_g` moves from lower SRAM to high SRAM to retain linker headroom; it is used by initialization and the serial reference, outside the timed streamed kernels.

[The GCS/BB path](../../firmware/src/c3_fec.c) still reads the continuous memory payload, constructs the GCS header and CRC8, converts byte order, and emits all BB bits. Option8 XORs the existing BB PRBS while storing each generated word directly to FE. It removes the intermediate BB write/read and separate frame-copy pass; it does not replay a pre-encoded frame. Tail masking and zeroing remain charged. `dvbs2_frontend_begin_prepared()` admits this already prepared frame without touching it. The old BB array stays reserved for a placement-controlled A/B comparison. Header CRC8 is already table driven and only nine bytes/frame, so another CRC table is not worthwhile. The existing BB PRBS is already precomputed: allocating a second sequence would save nothing.

## Cadence, instructions and cycle estimates

At `d=cpw`, `Rs=SYS/(8d)`, a 22194-symbol frame takes `177552d` SYS cycles, and alternating frame owners receive **355104d cycles per assigned frame**. At SYS128/d1 this is16Msym/s and a1.387125ms frame period; d2 is8Msym/s and710208 cycles per assigned frame. SYS256/d2 is16Msym/s with710208 cycles per assigned frame. These changes use no absolute-clock assumptions or clock changes.

The checked local RP2350 datasheet build2025-07-29, §3.7.4.9 pp137–144, describes single-core/no-cache-miss/no-DMA instruction timing. Simple ALU operations nominally take one cycle; load results and complex shifts may incur result-use penalties, taken branches cost extra, and multiword transfers depend on transfer length. [The project timing notes](../rp2350-notes.md) record the same limits. All C4 cycle numbers below are **instruction-model estimates, not native timings or M33 measurements**. Two-core SRAM contention, XIP misses, USB and output arbitration are excluded.

LDPC's copied group leaf executes200 scalar instructions/group,72 per shifted edge and60 per unshifted edge, counting dispatch/loop bookkeeping. The shifted twelve-word interior is60 instructions versus C3's84. Counts are:

| Code / rate | Groups | Shifted / unshifted edges | Scalar group-leaf instructions | Total LDPC planning cycles/frame |
|---|---:|---:|---:|---:|
|5 /2/3|120|460 /20|58320|105–135k|
|8 /5/6|150|585 /15|73020|110–140k|
|6 /3/4|135|530 /10|65760|115–150k|

Totals additionally charge extraction/duplication, load/shift dependencies, row clear/accumulate/prefix, transpose, parity packing and sliced dispatch. There are312/252/282 LDPC units and24/12/24 transpose tiles respectively. This is not a claim that placement recovers old measured107.8/111.3/114.7k cycles on two contending cores.

The fused GCS payload loop disassembles to10 instructions/word including two loads, REV, XOR and store, versus the same-image off-path8-instruction source loop **plus** the approximately13-instruction/two-word BB copy/scramble loop. At1345/1683/1513 BB words this removes approximately6–8k scalar instructions/frame and8 bytes/BB word of intermediate SRAM traffic (10.76/13.464/12.104kB per frame). Header/tail setup, bounded zeroing and more service calls remain. A planning range is20–28k/24–33k/22–31k cycles for the full `gcs` stage.

The streamed t10 BCH interior has54 scalar instructions/word and20 table loads; t12 has63 and24 loads, against the old C t12 loop's74 instructions and stack spills. With64-word slices, provisional ranges are80–95k (2/3),100–118k (5/6), and105–130k (3/4). Scratch placement can either help instruction fetch or hurt shared-bank access. Wider slicing tables would need additional kilobytes and do not remove the twenty/twenty-four remainder-word table reads per four input bytes.

The copied mapper leaf is440bytes,94 instructions/32 data symbols (28 loads, including12 LUT and11 stack loads, and8 stores). C3's loop was114 instructions/32,15 loads and7 stores. There are675 data words,85 step calls and98 leaf runs at the unchanged eight-word budget. The new data loop executes63450 scalar instructions plus4018 entry/exit instructions and multiword/branch penalties. Prepacked constants reduce header/pilot work. A full mapper estimate is85–115k/frame. **The sub-80k mapper target is not established.** Its extra32data bytes/32symbols add about16MB/s at16Msym/s; SRAM fetch avoids XIP misses but increases low-bank contention.

There is also real supervision work. The supplied C3 cpw2 records charge core0 an additional87.8/83.5/82.0k cycles per assigned frame beyond stage totals (codes5/8/6), and core1 about17–19k. These are inherited device measurements. Option16 removes redundant deadline quotients: every release caches its absolute publication deadline; every retirement computes the next retirement deadline once. C3 recalculates that same64-bit division at each32us service. At SYS128/d1 that is roughly86.7 retirement calculations per assigned alternate frame, d2 roughly173.4, reduced to approximately four calculations/frame including both release and retirement. Each quotient still uses the original absolute-round-up formula; no cumulative rounding or expanding queue is introduced. Remaining timer reads, publication, polling, worker bookkeeping and interrupts are charged. The final libgcc path uses43 instructions in `__udivmoddi4`,9 in `__aeabi_uldivmod` and one veneer, including two hardware UDIVs; the inline numerator/call/epoch arithmetic brings a full deadline to about73 scalar instructions versus three cached-load instructions. A110–140-cycle model per full calculation gives approximately9.3–11.9k cycles saved per core0-assigned frame at SYS128/d1, or about17–24k at d2. These are estimates; much of the inherited82–88k supervision remains unexplained by division and must still be measured. No whole-pipeline fit follows from this saving.

| Rate | Fused GCS/BB | BCH | LDPC | Mapper | Stage sum estimate |
|---|---:|---:|---:|---:|---:|
|2/3|20–28k|80–95k|105–135k|85–115k|290–373k|
|5/6|24–33k|100–118k|110–140k|85–115k|319–406k|
|3/4|22–31k|105–130k|115–150k|85–115k|327–426k|

These are planning envelopes, not rigorous bounds. Add measured residual supervision/loop work before assessing320k, the desired total with margin, or355104, the absolute SYS128/d1 alternate-frame budget. The estimates do **not** establish that2/3 or5/6 fits. The faster kernels and reduced division work make a meaningful device test possible; preserve a negative result if they remain over budget. A positive timer run alone does not prove DMA word service.

Option8 bounds GCS generation and tail clear to64 words/call. BCH consumes64 words; LDPC one group/row/pack unit; mapper at most eight32-symbol chunks plus reached constants. Typical zero-wait instruction estimates are below6k cycles for those slices. This is not a16k worst-case proof under contention. Record both `core[].max_slice_cycles` and new `core[].stage_max_cycles`, together with the measured core0 service gap. The qualification gate remains128us; `gap_us=4096` is a separate diagnostic only.

## DMA, port and storage budgets

C4 itself allocates **zero DMA channels, zero PIO SMs and zero PIO instruction words**. Its output remains0.5byte/symbol of paired code stores,8MB/s at16Msym/s. It does not poll DMA/PIO during the cadence loop or offload any FEC work to the sniffer/interpolator. Supervision still includes timer/SIO/DWT and USB effects.

For integration, B6's four duplicated input streams read0.25words/symbol, costing0.25 DMA transfers and0.25 FASTPERI accesses/symbol,16MB/s of SRAM reads at16Msym/s. This is twice the two-stream C3 design estimate; do not count only two readers when using B6's four producers. B6's complete OUT8 graph is4.25 DMA transfers and6.25 port accesses/symbol:53.125% DMA /78.125% ideal port capacity at d1, before CPU grant polling. Stride17 capture adds8/17 of each per symbol, raising these to59.007% /84.007%. Its existing core1 worker's requested poll/rearm envelope raises the live port total to about90.361% with capture. These are read-only B6 design counts, not a C4 integration measurement.

The checked datasheet §2.1 p24 and §2.1.1 p25 puts PIO0/1/2, DMA control and USB behind one shared fast port with at most one zero-wait access/SYS cycle. A producer RX→trigger pointer consumes two accesses, an SRAM→TX sample one. The old one-symbol14-bit graph remains over capacity at d1 regardless of FEC speed. B6 OUT8 reserves10 SMs and13 DMA channels with capture (PIO0/1/2 use13/14/8 instruction words); C4 adds none, but **C3's core1 FEC and B6's dedicated core1 grant worker cannot run together unchanged**. A different grant schedule remains necessary.

Every new lookup/instruction allocation is shared read-only by both jobs. The 128KiB `ring_buf` allocation stays fixed: its first64KiB is reserved for OUT8 shaping, the following64KiB owns C4 contexts. The32KiB `in_buf` remains two16KiB code rings (65536 symbols each); `link_ring` remains unused. This cannot coexist with an OUT16 table occupying all128KiB.

| Table/arena | SRAM bytes | Ownership |
|---|---:|---|
| Existing BCH slicing table |32768|High SRAM; unchanged |
| Existing BB PRBS |8108|High SRAM; unchanged |
| Existing LDPC row/offset tables |8192|High SRAM; unchanged |
| Existing shared 8PSK Gold reservation |5536|C4 context; unchanged |
| New byte dilation |512|Mapper arena |
| New packed header |48|Mapper arena |
| New packed pilots, normal frame |336|Mapper arena |
| New LDPC executable section |5,578 copied /7168 reserved|C4 context |
| New mapper metadata |16|Mapper arena |
| New mapper executable code |440 used /1024 reserved|Mapper arena |
| New degree192 BCH code |260|ScratchY (258-byte function plus alignment) |
| Cold BCH generator coefficients |24|Moved lower→high SRAM; no net allocation |

The final Release experiment map and DWARF give54,200 bytes for the complete context at`0x20010000..0x2001d3b7`, leaving11,336 bytes in its64KiB tail. Each job is19,308 bytes, frontend40, LDPC88, mapper state24 and mapper config84. The7,168-byte LDPC arena holds5,578 copied bytes (5,580-byte aligned object section, including a64-byte TBH dispatch table); mapper arena is1,936 bytes total, with 440 actual code bytes in its1024-byte code reservation. One4-byte prepared LDPC pointer is static; each LDPC state adds8bytes. No extra frame or matrix allocation was introduced.

| Linked iqbench region | Start / bytes / end-exclusive |
|---|---|
| Lower `.data` |`0x20030110` /40736 /`0x2003a030`|
| Lower `.bss` |`0x2003a040` /23236 /`0x2003fb04`|
| Minimum heap |`0x2003fb04` /1024 /`0x2003ff04` (252bytes free after it)|
| High SRAM |`0x20040000` /258024 /`0x2007efe8` (24bytes below stack)|
| Core0 stack |`0x2007f000` /4096 /`0x20080000`|
| ScratchX code / core1 stack |2032 /2048bytes;16bytes remain|
| ScratchY code |2208bytes;1888bytes remain|

`pvflight` also links, with just8bytes remaining after its lower heap and344bytes below its high stack. Use iqbench for this experiment. Any further static allocation requires rechecking both images. SRAM instruction arenas remain in the same striped half as the matrices and output rings. B6's scratch worker/state is not in this branch: merge placement must be rechecked rather than assuming its1888-byte headroom suffices. Tables do not imply free SRAM bandwidth: BCH alone reads107.6/134.6/145.2kB/frame of remainder data; LDPC edges still read/write roughly69.1/86.4/77.8kB/frame before extraction, passes and transpose. The mapper trades scalar instructions for additional LUT loads. Global traffic is not a per-bank throughput guarantee.

## Native verification and build result

Both `make build` and the experiment `make build BUILD=firmware/build-c4` complete for iqbench/pvflight without introduced warnings. `make check` includes90 Python invocations. No host wall time is used as M33 timing evidence.

- Packed mapper:360 frames and72 deferred frames, normal/short codes, every initial fill, short tails, reversed columns, Gold extremes, tiny rings and out-of-order boundaries match both the old C signed-plane mapper/converter and independent Python. The old mapper's459-frame regression also passes.
- Concurrent FEC:48 independent Python vectors over six normal rates, real pthread workers with mixed LDPC modes and frontend flags, three BCH selectors, varied slice budgets, serial BCH/LDPC comparisons, input/workspace guards and zero-budget/no-write admission. ASan/UBSan of the threaded harness passes.
- Actual assembly source semantics:80 LDPC cases /449,080 interpreted instructions check all361rotation offsets, four information alignments, dirty matrices, bounds and AAPCS preservation;5,150 BCH cases /1,171,913 instructions check t8/t12, arbitrary incoming remainders, every table index, slice boundaries and stack/register preservation. These are instruction-semantics models, not execution on an Arm core.
- The production C3 job graph:all 32 masks ×three rates ×16 continuous frames; odd jobs finish first; every physical I/Q ring word, carry and complete-group count matches independent Python through wraps and all 16 boundary phases. ASan/UBSan passes that same32-mask sweep. The portable pipeline ignores bit16 by design; the actual benchmark mock verifies its scheduling separately.
- The production benchmark with virtual time and real FEC checks every option, cached/uncached deadlines at SYS128/256 ratios, slow failure drain, injected service gap, JSON varargs, CRCs, resource/selector/PRBS restoration and worker join. Its synthetic1us slices validate scheduling only.
- The mapper PIC test compiles with the actual GCC14.2/M33/softfp/cmse Release flags and verifies440bytes, no relocations, calls, external branch targets or PC-relative data. The built LDPC executable section likewise has no relocations. The runner rejects option/gap mismatches, CRC corruption, incomplete cadence, invalid utilization and over128us slices on either core; relaxed diagnostics remain explicitly tagged.

The built image identifies as`994f6aa458-dirty`. UF2 SHA256 is`d71984151f850d91e4f89fac36bd86d912ce792f5a2e19ea0d7b43b6bac4d549`, ELF SHA256`e9adbbe744972c03841373f635aa3bb38da0b204c890d06c9099496e02317025`. Rebuilds can change these values. **Final verification: `make check` passed all 90 Python invocations, both firmware configurations built, ASan/UBSan passed, and `git diff --check` plus Markdown link checks passed.** The inherited waveform self-test reports four absent gitignored historical firmware binaries; their hashes were not revalidated. No device qualification was performed.

## Coordinator build and trials

Host-only reproduction (completed validation is stated below):

```sh
make build
PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0" \
PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1" \
cmake -S firmware -B firmware/build-c4 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON \
  -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-c4
make check
python3 host/c3_fec_trial.py --reference --codes 5 8 6 --frames 3
C4_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-c4/git_rev.h)
```

The coordinator loads `firmware/build-c4/iqbench.uf2` through their normal procedure. Absolute UF2 path:
`/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.claude/worktrees/codex-c4-fec-speed/firmware/build-c4/iqbench.uf2`.
The runner records actual source/build SHA256s; a `-dirty` revision alone is insufficient. Use the actual CDC path in `C4_PORT` and fresh output directories. The following are device instructions, **not commands executed here**:

```sh
python3 host/iqbench.py --port "$C4_PORT" raw info
python3 host/iqbench.py --port "$C4_PORT" raw stackcheck

# All stages separately off/on, slower8Msym/s and target16Msym/s.
# Every mask is checked against the same independent ring reference.
for C4_CPW in 2 1; do
  python3 host/c3_fec_trial.py --port "$C4_PORT" --codes 5 8 6 \
    --cpw "$C4_CPW" --ms 3000 --options 0 1 2 4 8 16 15 31 --gap-us 128 \
    --build firmware/build-c4 --expected-git "$C4_FW_GIT" \
    --out "results/c4-fec-speed/isolated-cpw${C4_CPW}"
  python3 host/iqbench.py --port "$C4_PORT" raw stackcheck
done

# Remove each compute/deadline option from the combined option, at both rates.
for C4_CPW in 2 1; do
  python3 host/c3_fec_trial.py --port "$C4_PORT" --codes 5 8 6 \
    --cpw "$C4_CPW" --ms 3000 --options 31 30 29 27 23 15 --gap-us 128 \
    --build firmware/build-c4 --expected-git "$C4_FW_GIT" \
    --out "results/c4-fec-speed/remove-one-cpw${C4_CPW}"
done

# Diagnostic only, if128us controls stop too early for steady-state stage averages.
python3 host/c3_fec_trial.py --port "$C4_PORT" --codes 5 8 6 \
  --cpw 2 --ms 3000 --options 0 1 2 4 8 16 15 31 --gap-us 4096 \
  --build firmware/build-c4 --expected-git "$C4_FW_GIT" \
  --out results/c4-fec-speed/diagnostic-gap4096

# Required negative:5ms extra work/frame must miss the deadline with all options.
python3 host/c3_fec_trial.py --port "$C4_PORT" --codes 5 \
  --cpw 1 --ms 3000 --slow-us 5000 --options 31 --gap-us 128 \
  --build firmware/build-c4 --expected-git "$C4_FW_GIT" \
  --out results/c4-fec-speed/slow-negative

# Only for rates/options that pass short qualification; retain every failure.
python3 host/c3_fec_trial.py --port "$C4_PORT" --codes 5 8 6 \
  --cpw 1 --ms 60000 --options 31 --gap-us 128 \
  --build firmware/build-c4 --expected-git "$C4_FW_GIT" \
  --out results/c4-fec-speed/combined-60s
python3 host/iqbench.py --port "$C4_PORT" raw stackcheck
```

The runner completes and retains a sweep even if individual masks fail, then exits nonzero for unexpected failures. C3 controls without bit8 may reproduce the known service-gap failure; this is useful A/B evidence, not a reason to relax qualification. Manual equivalents are `c3fec 5 3000 0 2 128 0`, `c3fec 5 3000 0 1 128 31`, `c3fec 8 3000 0 1 128 31`, `c3fec 6 3000 0 1 128 31`; change the last argument to0/1/2/4/8/16 or31/30/29/27/23/15 and cpw to2 for controls. The negative is `c3fec 5 3000 5000 1 128 31`.

Positive output must identify the requested `options`, `cpw`, `service_gap_limit_us`, code and image; have `errors=late_frames=max_lateness_us=0`; retire/publish every target frame; match independent ring CRCs, group count and partial fill; keep valid even/odd per-core accounting and service gap≤128us. Also require every measured slice≤16384cycles at SYS128 (about16k), preferably≤16000 for margin, and no exhausted stack paint or scratch-code mismatch. For the320k goal, divide each core's `busy_cycles` by its completed frames; core0 already includes supervision. This aggregate includes startup/last partial service and is conservative for short trials. Use stage totals/frame for diagnosis and preserve cold maxima separately. The absolute deadline budget is355104cycles per assigned frame; a fit near this edge does not meet320k margin.

At SYS128/d1,3000ms targets2162frames and60s targets43254; cpw2 gives1081/21627. Three-frame offline CRCs remain I/Q `[1531943157,3986744385]` for5, `[2356957565,1808985931]` for8, and `[2937561118,1824749921]` for6, with4161 complete groups and fill6. The negative must show one deadline fault and positive lateness, stop releases, drain the bounded jobs, and retain exact CRCs. A healthy-looking negative fails its control. A4096us diagnostic is tagged `qualification=false` in runner evidence even when its requested relaxed checks pass.

On the coordinator's separately configured SYS256 image, use d2 for16Msym/s and d4 for8Msym/s. This branch configures neither clock. Do not transfer SYS128 utilization percentages without accounting for the doubled assigned-frame cycle budget.

## Risks, dead ends and handoff

The mapper's table-load cost and both cores fetching the same SRAM code are open arbitration questions. ScratchY BCH may improve CPU-only runs and worsen an integrated image, as the earlier C1 placement experiment did. The low SRAM headroom and4KiB core0/2KiB core1 stacks require `stackcheck` before and after runs, including after JSON printing. Core1's LDPC step plus copied leaf has an estimated496-byte static stack chain before dispatcher/worker/interrupt frames; compiler stack reports are not stack high-water measurements.

Rejected: sharing the old global LDPC matrix; locking the whole stage; a second BB PRBS; moving the complete copy pass into another stage without eliminating it; pre-encoded frame replay; per-frame output padding; wide mapper tables that spend their savings assembling indices; and claiming host execution time as M33 evidence. Interpolator addressing needs SIO loads/stores for each lookup and does not itself reduce the BCH remainder loads. Hazard3 lacks carryless multiply, so mixed-architecture BCH is not an implemented shortcut. The existing32MHz single-lane PV-SPI source still cannot sustain the2/3 memory-source target, and USB Full Speed is not the required ingress. C4 implements neither faster ingress nor B6 grant-service integration, AFE timing, RF/EVM or receiver qualification.

Intended small logical commits for the coordinating engineer; none was created:

1. `firmware/src/dvbs2.{c,h}`, `firmware/src/dvbs2_ldpc_asm.S`, `firmware/CMakeLists.txt`, `host/native/dvbs2_parallel_test.c`, `host/test_dvbs2_parallel.py`, `host/test_ldpc_asm.py`, `Makefile`: **Add concurrent SRAM LDPC rotation kernels and fixed-rate streaming packs.**
2. `firmware/src/dvbs2_8psk.{c,h}`, `host/test_dvbs2_8psk_codes.py`, `host/test_c4_mapper_pic.py`: **Add opt-in table packing and invariant PL codes for 8PSK.**
3. BCH/frontend hunks of `firmware/src/dvbs2.{c,h}`, `firmware/src/dvbs2_asm.S`, `host/test_bch128_asm.py`, frontend test hunks: **Stream degree192 BCH and admit directly scrambled FE input.**
4. `firmware/src/c3_fec.{c,h}`, `firmware/src/bench_c3_fec.c`, `firmware/src/main.c`, `host/c3_fec_trial.py`, `host/native/c3_pipeline_test.c`, `host/test_c3_pipeline.py`, `host/test_c3_bench.py`, `host/test_c3_fec_trial.py`: **Expose C4 A/B options, cache deadlines and fuse bounded GCS scrambling.**
5. `docs/rd/c4-fec-speed.md`: **Record C4 instruction budgets, native evidence and device qualification commands.**
