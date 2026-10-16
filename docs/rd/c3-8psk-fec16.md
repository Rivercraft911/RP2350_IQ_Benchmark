# C3: frame-parallel 8PSK FEC into packed axis-code rings

C3 implements the CPU-side experiment: two independent, bounded GCS → BB scrambling → BCH → LDPC → fused 8PSK PL mappers, with ordered publication into the packed code rings requested by B6. **Native correctness and firmware builds pass; SYS128 CPU fit is unmeasured.** No Pico, serial port, flashing tool, clock/VREG/flash change, Git mutation or existing results evidence was used. This worktree starts at B5 `5d7ddab57b`, on `rd/c3-8psk-fec16`.

Eliminating the separate conversion does not eliminate packing instructions. The actual fused mapper estimates to **103–140k cycles/frame** with the benchmark's eight-word slices, versus the old measured approximately 48k signed-plane mapper. This leaves 2/3 as a device measurement question and gives substantially less margin than the earlier “conversion-free” estimate. Neither success nor impossibility of the complete SYS128 pipeline is established by host execution. The existing one-lane 32 MHz PV-SPI source independently falls short of all three requested rates; C3 explicitly uses an in-memory GCS source and charges its adaptation cost.

## Implementation and ownership

- [dvbs2_8psk.c](../../firmware/src/dvbs2_8psk.c) adds `dvbs2_8psk_codes_begin/step/frame/flush`. It writes sixteen chronological two-bit coordinate codes per uint32, separately for I and Q, low bits first. It includes the existing π/8 global rotation, π/2-BPSK PLHEADER, pilots and Gold sequence. Normal/short codes, reversed 3/5 columns and stream continuation remain supported. The original signed-plane mapper API is unchanged.
- [dvbs2.c](../../firmware/src/dvbs2.c) adds a caller-owned LDPC matrix and continuation state, sharing only initialized read-only code/BCH/address tables. Its new packer uses pack1's streaming bit order, including q60, with **q × 12 words** of matrix storage. No concurrent call writes the old global `P`, `fe`, `lpar` or `dvbs2_prof`. `dvbs2_frontend_begin_reentrant` suppresses shared profiling and supports degree192 as well as 128/160; the original frontend admission behavior stays unchanged. Both cores use frontend bank0; scratchY's core0-only leaf is not shared.
- [c3_fec.c](../../firmware/src/c3_fec.c) supplies portable jobs and a sliced in-memory GCS BBFRAME adapter. The continuous payload byte at absolute offset j is `(29*j+7)&255`, with a 256-byte period. Each frame starts at `frame_index*(Kbch-80)/8`, rather than resetting the payload cursor. MATYPE1 is `0x72`, UPL/SYNCD zero and SYNC `0xb9`; header CRC8 uses the existing initialized read-only table. The initial doubled 512-byte payload pattern is prepared outside timing. Every frame's header, memory reads, BB writes, byte order conversion, scrambling and FEC remain charged. This is **memory source adaptation**, not PV-SPI receive/protocol validation or payload generation at an external sender.
- [bench_c3_fec.c](../../firmware/src/bench_c3_fec.c) adds experiment-only `c3fec code ms [slow_us] [cpw]`, for normal 2/3 (5), 5/6 (8) and 3/4 (6), pilots on, Gold0. There is no output path. It borrows the unused OUT8 table tail and `in_buf`, dispatches even frames to core0 and odd frames to core1 through the existing idle worker, then restores BCH/bank selectors and the PRBS input. Other mapper/LDPC selectors are preserved.

Each frame has 22,194 symbols, which is 2 modulo16. Mapping separately padded frames would insert fourteen false symbols at every boundary. Instead each job starts at its absolute complete-group cursor and fill, with zero accumulators, and **defers its first complete word**. It writes later full words directly into disjoint intervals of the shared rings. Only core0's ordered publisher ORs the deferred first word with the preceding frame's saved partial word, writes both axes, and releases the complete-group counter after DMB. Final partial symbols remain pending; there is no per-frame flush, full-frame code staging copy or separate plane-to-code conversion pass.

At most two frames own ring capacity. Slots0/1 start together; after timer retirement of frame f, only slot f+2 can be released. A 65,536-symbol ring exceeds the two-frame ownership interval of 44,388 symbols by **21,148 symbols**. Reuse is based on this timer consumer only. This is an output-free CPU benchmark, **not a DMA credit API**: B6 must replace timer retirement with its actual issued-read/credit rules and retain boundary publication ordering. Absolute cursors stay below uint32 overflow for the admitted 60-second duration even at SYS256/cpw1.

## Cadence and accounting

Let d=`cpw` and SYS be the reported clock. Then `Rs=SYS/(8d)`, frame period `T=177552*d/SYS` seconds and each worker gets `2T` for its subsequent frame. At SYS128/d1 this is **16 Msym/s, T=1.387125 ms, 177552 cycles/core per on-air frame, 355104 cycles per assigned alternate frame**. SYS128/d2 is the 8-Msym/s control. SYS256/d2 is the same absolute 16-Msym/s cadence, with twice the CPU cycles; SYS256/d1 is a CPU-only 32-Msym/s stress cadence, not a DAC operating point. No clock is changed by C3.

Frame f's absolute timer deadline is `epoch + ceil((f+2)*177552*d*1e6/SYS)` microseconds. Two frame periods of startup are explicit. Rounding each absolute deadline once avoids accumulated fractional-us drift. Frames0/1 are initially released together; frame0's deadline is2T and frame1's is3T. Target frames are `floor(ms*SYS/(177552*d*1000))`. A successful run retires all targets and therefore takes the requested duration plus up to one frame period and the final service latency, including startup. A complete frame must be published before its timer retirement; this tests frame cadence, not individual DMA word deadlines or actual RF consumption.

Every core0 work unit returns to timer/service logic. Service is nominally every32us; gaps above128us fault the run. GCS and BCH slices are64 words, LDPC slices are one row/group/pack unit, and mapping slices are eight 32-symbol data words plus any header/pilot reached. The final GCS slice also performs the complete bounded BB copy/scramble/zero-fill pass. That pass's actual maximum service gap is deliberately measured rather than assumed safe.

DWT deltas accumulate in uint64 totals; each subtraction covers only one bounded operation, so a 32-bit DWT wrap during a60s run is harmless. Reported stages are `gcs` (**including BB copy/scramble/zero-fill**), `bch`, `ldpc`, `map`, and injected `delay`. `max_frame_cycles` sums the frame's work slices; `max_frame_us` includes yields/service interruptions from assignment to readiness. Core0 `busy_cycles/busy` includes measured supervision and loop overhead; core1 includes its work-loop overhead. Deliberate idle waits are excluded. Setup, final ring CRC and JSON output are outside the cadence interval. These fields are device measurements **only when the coordinator runs the command**.

A first deadline miss stops new releases, records `late_frames=1`, and drains already active jobs before publishing them in order. Thus the final physical-ring CRC never includes speculative stores from an unfinished frame. Publication lateness reports actual publication time relative to the absolute deadline, including drain. A service-gap fault also stops releases and drains. A gap-only fault may have `late_frames=0`; it still fails a positive run. The command does not retry a missed cadence or conceal it with an expanding queue.

## First-principles cycle budget

The checked local RP2350 text is datasheet build2025-07-29, §3.7.4.9, printed pp137–144, also summarized in [rp2350-notes.md](../rp2350-notes.md). Simple ALU instructions are nominally one cycle; taken branches cost at least two; loads and complex shifts can incur a one-cycle result-use penalty. Multiword transfers, XIP cache misses, two-core SRAM contention and interrupts prevent instruction counting from being a device guarantee.

The actual Release/GCC14.2 normal mapper loop contains **114 instructions per32 data symbols**, including15 LDR and7 STR (some stack spills). At eight-word slices there are675 data words,85 step calls,98 normal runs and14 pilot calls. The normal helper accounts for87,724 scalar instructions, plus1,752 multiword-transfer cycles; the wrapper accounts for5,023 scalar instructions and about3,740 multiword-transfer cycles. Header and pilots add approximately4–5k cycles. Including taken branches gives a zero-wait baseline near103k/frame. Charging every normal complex-shift and load result-use penalty without overlap, plus wrapper/pilot penalties, gives approximately140k. A nominal planning value is120–125k. This range excludes XIP misses, active DMA and SRAM arbitration, and is **an estimate, not a measured lower/upper bound**.

The algebra computes packed Q and obtains packed I using `I=Q XOR (3*spread16(a XOR r0)) XOR 0xaaaaaaaa`. This shares sign-plane work and avoids intermediate signed-plane stores/loads, but still needs a perfect shuffle and dilation. Treating the old48k mapper as the new fused mapper would materially underbudget this implementation.

The previous B4 isolated **measurements**, not measurements of C3, are:

| Rate | BB+BCH cycles/frame | pack1 LDPC | old signed-plane map | GCS data capacity at16 Msym/s |
|---|---:|---:|---:|---:|
| 2/3 | 91907 | 107834 | 47952.9 | 30.970533 Mb/s |
| 5/6 | 113948 | 111314 | 47951.8 | 38.756421 Mb/s |
| 3/4 | 139286 | 114667 | 47948.4 | 34.840407 Mb/s |

Replacing only the old mapper with103–140k consumes **85.25–95.67%**, **92.44–102.86%**, and **100.52–110.94%** of the joint355104-cycle budget, respectively, before memory GCS adaptation, scheduling, changed LDPC packing or new contention. These are sensitivity estimates based on inherited measurements; they are not predictions validated for the C3 image. In particular, 3/4's newly sliced degree192 C leaf runs from flash and cannot inherit the older BCH measurement unchanged. A slower result must be retained as a negative result, not “corrected” to these stage sums.

Both160-bit rates execute1345 (2/3) or1682 plus two tail bytes (5/6) BCH word recurrences. The existing160-bit assembly loop has54 scalar instructions/word, twenty table loads plus one input load, and branch/result-use costs; slice prologues and epilogues are additional. A59–70-cycle word model gives approximately80–95k/100–118k cycles for2/3 and5/6 with slicing. The192-bit rate executes1512 word recurrences plus three tail bytes; its compiled new C loop has75 instructions/word,28 LDR plus one LDRD, three STR and24 XOR. A90–110-cycle word model gives about136–166k before cache/contention. The actual `bch` totals and `max_slice_cycles` resolve this placement effect.

LDPC needs480/600/540 address edges and120/150/135 information groups for2/3,5/6,3/4. Clear, group, row accumulation, prefix and twelve transpose/pack operations total312/252/282 bounded units. Matrices use60/30/45 rows respectively. All row rotation/XOR and transpose operations are still CPU work; no DMA acceleration or free parity calculation is assumed.

The shifted-edge inner body has84 instructions (twelve source-load/shift/merge/row-load/XOR/store groups), plus one initial source load and approximately14 address/loop instructions. Charging row-load and complex-shift penalties gives120–145 cycles/shifted edge, versus65–90 unshifted. The shifted/unshifted edge counts are460/20,585/15,530/10. Extraction/duplication adds170–240 cycles/information group; clear/accumulate/prefix application about200q cycles; bounded dispatch about45–60 cycles/unit. There are24 transpose tiles forq60/q45 and12 forq30. A450–650-cycle transpose model plus tile loading and streaming append gives roughly28–35k cycles for the q>32 pack or14–19k forq30. Together these produce broad LDPC estimates of130–165k (2/3),128–165k (5/6),137–173k (3/4). The new LDPC leaves use O3 in flash; reduced loop code size was rejected when it prevented inlining the row update.

The compiled GCS interior loop is seven instructions/word, including one source load, REV and one BB store. Including the taken branch, possible unaligned-load cost, slice setup/header CRC and the13-instruction/two-word BB copy/scramble loop gives planning ranges23–32k/28–38k/26–36k for the complete `gcs` stage. Its unaligned source reads are deliberate and readable in the doubled pattern; their silicon cost remains a measurement variable. Combining the new stage models gives the following **contention-free planning envelopes**, with additional loop/supervision/IRQ/XIP costs still to measure:

| Rate | GCS + BB preparation | BCH | LDPC | Fused map | Total cycles/frame | Share of355104 cycles |
|---|---:|---:|---:|---:|---:|---:|
|2/3|23–32k|80–95k|130–165k|103–140k|336–432k|94.6–121.7%|
|5/6|28–38k|100–118k|128–165k|103–140k|359–461k|101.1–129.8%|
|3/4|26–36k|136–166k|137–173k|103–140k|402–515k|113.2–145.0%|

These ranges are instruction-model estimates, not rigorous throughput bounds. They suggest2/3 is marginal and the higher rates are unlikely to fit this implementation at SYS128. The benchmark is useful precisely because it can measure the actual fit or preserve a clear negative result. No “82% after fusion” claim is made.

## DMA, peripheral port and SRAM budgets

C3 itself claims **zero DMA channels, zero PIO SMs, zero PIO instruction words**. Its only output is CPU stores to SRAM. TIMER/DWT/SIO/USB service remains in measured supervision; C3 does not read PIO or DMA registers during cadence. The packed interface requires `2*4/16 = 0.5 byte/symbol`, or **8 MB/s** total code writes at16 Msym/s. B6 reading it with DMA adds8 MB/s SRAM reads,0.125 DMA transfers and0.125 FASTPERI accesses/symbol: **2 M transfers/accesses/s**,1.5625% of SYS128's ideal port capacity. This input-interface cost is separate from B6's pointer/sample graph.

The RP2350 text was checked at §2.1 p24 and §2.1.1 p25: PIO0/1/2, USB and DMA control registers share one fast-peripheral port; a zero-wait subordinate accepts one access/SYS cycle. An RX-FIFO-to-trigger-register pointer uses two accesses; an SRAM pointer uses one. B5's cyclic OUT8 SRAM-pointer baseline is4 transfers/accesses per symbol (50% at d1), excluding a live code-input graph. C3 does not claim B6's total port/resource budget. The earlier14-bit PIO-pointer graph's8.125 accesses in8 cycles remains impossible at d1; CPU fusion cannot fix that port limit.

BCH's minimum table-read traffic alone is80 bytes per160-bit word or96 bytes per192-bit word: approximately107.6/134.6/145.2 kB per frame for2/3,5/6,3/4, or77.6/97.0/104.6 MB/s at16 Msym/s. LDPC rotation updates read12 source words and read/write12 row words per edge: at least69.1/86.4/77.8 kB per frame (49.8/62.3/56.1 MB/s), before extraction, cumulative passes, prefix, transpose and stack spills. These are traffic counts, not per-bank bandwidth guarantees. Add GCS source reads/BB writes, BB/frame/scrambler reads, map FE/Gold reads, the8 MB/s code stores, instruction fetches and eventual output DMA. Both cores can contend even without B6.

## Linked storage and stack

C3 adds no large static allocation and does not use the high SRAM gap. The Release experiment map is the authority for placement. The128 KiB `ring_buf` remains reserved; C3 only writes its unused OUT8 half, so this is compatible with an owned64 KiB OUT8 table and not with a128 KiB OUT16 table.

| Region or object | Address / bytes |
|---|---|
| Reserved `ring_buf` | `0x20000000`,131072 |
| OUT8 table reserved head, untouched by C3 | `0x20000000..0x2000ffff`,65536 |
| C3 borrowed tail | `0x20010000..0x2001ffff`,65536 capacity |
| C3 context, including two jobs and shared Gold | `0x20010000..0x2001afc7`,45000;20536 bytes remain in the tail |
| Two I/Q code rings (`in_buf`) | `0x20020000`,32768 total;16384/axis |
| Existing `link_ring`, unused by C3 | `0x20028000`,32768 |
| Lower `.data` / `.bss` | `0x20030110` /40712; `0x2003a040` /23248 |
| Minimum heap / gap after it | `0x2003fb10` /1024;240 bytes remain |
| Existing `.sram_hi` | `0x20040000`,258016; ends`0x2007efe0` |
| Core0 stack | `0x2007f000..0x2007ffff`,4096;32-byte gap below |
| ScratchX code / core1 stack |2032 /2048 bytes;16-byte gap |
| ScratchY code |1952 bytes;2144 free |

Each job is19296 bytes in the ARM build: two2027-word BB/FE arrays (16216 bytes), a720-word worst-case matrix (2880), frontend/LDPC/map continuation state and its stream. ARM frontend, LDPC state, code stream, map state and shared map config sizes are40/80/40/24/80 bytes. Required active matrix sizes are2880/1440/2160 bytes for2/3,5/6,3/4; the benchmark reserves2880 for each to keep layout fixed across runs. Gold storage is one shared5536-byte arena, not two. The separate BB input makes source adaptation independently timed and avoids implicit shared mutable state.

New mapper, GCS, reentrant LDPC and degree192 code stays in flash to preserve the tight existing SRAM placement. Core1 retains its2 KiB stack; core0 has4 KiB. Compiler stack reports give336 bytes for LDPC step plus36 for its transpose leaf (372 total),32 for the portable job dispatcher,56 for the core1 worker and224 for the command: approximately460 bytes on the core1 encoding call chain or628 on core0, before interrupts/SDK effects. The mapper step/helper chain is smaller. These are planning evidence, not runtime high-water measurements. Run `stackcheck` before and after device trials; both scratch code comparisons must remain intact and neither stack paint may be exhausted. USB interrupts and printf paths must be included in that assessment.

## Native verification

All commands below are host-only. Host wall time and the virtual scheduler's synthetic slice costs are **not Cortex-M33 timings**.

- `host/test_dvbs2_8psk_codes.py`:22 normal/short configurations,180 mapped frames/4560 slice calls, all16 initial fills, tiny ring wraps, Gold extremes, and36 separately mapped frames completed out of order. Every word matches both the old C mapper plus `iq4hist_pack_group()` and the independent Python constellation/header/Gold reference. The unchanged mapper's459-frame regression also passes.
- `host/test_dvbs2_parallel.py`:48 independent Python FEC vectors across six supported normal rates, real concurrent pthread workers, three BCH selectors, multiple slice sizes, serial BCH/LDPC comparisons, input/workspace canaries, no-write admission and zero-budget behavior.
- `host/test_c3_pipeline.py`:the production GCS/FEC/mapper job code, three rates ×16 continuous frames. Odd frames complete first; every pair's physical4096-word I/Q rings, carry and counters match independent Python. Four ring wraps and zero-fill eighth/sixteenth boundaries are covered. The odd frame's boundary word must remain untouched before ordered publication. Zero/one-word source slices match the existing `pv_gcs_bbframe` adapter and retain guards. ASan/UBSan checks passed for both concurrent FEC and the complete pipeline.
- `host/test_c3_bench.py`:the byte-for-byte production command and real encoder with mocked virtual time/worker pumping. Covers all three rates, slower and SYS256 ratios, actual JSON varargs, negative delay, injected200us service gap, failure drain, final CRC, buffer guards, selector/PRBS restoration and worker join. Its1us synthetic slices test scheduling logic only.
- `host/test_c3_fec_trial.py` and runner `--self-test`:independent continuous GCS/PL ring reference, retained-window CRCs, metadata/accounting checks and deliberate corruption/deadline failures. Serial requires an explicit `--port`; no automatic discovery occurs.

For a three-frame offline check, `published_groups=4161`, `pending_fill=6`; physical4096-group ring CRC32 values (little-endian uint32) are:

| Code index / rate | I CRC32 | Q CRC32 |
|---|---:|---:|
|5 /2/3|1531943157|3986744385|
|8 /5/6|2356957565|1808985931|
|6 /3/4|2937561118|1824749921|

The device runner computes CRCs for the actual published frame count, using only the retained final stream window, so a60s trial does not require regenerating every frame on the host. It saves raw output, source/build hashes, copied ELF/UF2 and fresh run records. These generated records are new evidence, not changes to existing runs.

**Completed here:** default `make build`, the explicit experiment build of both `iqbench` and `pvflight`, all88 Python invocations in `make check`, the final production-command mock rerun after tightening release ordering, `git diff --check`, and relative Markdown link checks. No new compiler warnings were emitted. The inherited waveform self-test reports four unavailable gitignored historical firmware images; it does not claim their hashes were rechecked. All other checks pass. Current experiment identity is `5d7ddab57b-dirty`; final UF2 SHA256 is `1963fb0efab780f9a9378841d6d0043e24cbe289e5ec8b46949310911ff162dd`, ELF SHA256 is `d4c25e71e2e7cb130693a804b4601658a8867525e3140da4b1f7201b9e06445c`. The runner archives the actual build hashes because a dirty revision string alone is insufficient. **No C3 device cycles, utilization, lateness or60s qualification were measured here.**

## Coordinator commands and pass criteria

From this worktree, build without touching the device:

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-c3 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON \
  -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-c3
make check
python3 host/c3_fec_trial.py --reference --codes 5 8 6 --frames 3
C3_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-c3/git_rev.h)
```

The coordinator loads `firmware/build-c3/iqbench.uf2` through the normal procedure. Absolute UF2 path:
`/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.claude/worktrees/codex-c3-8psk-fec16/firmware/build-c3/iqbench.uf2`.
Use the device's actual CDC path for `C3_PORT` and a fresh output directory for every invocation. The following commands are instructions for the coordinator; none was run against hardware here.

```sh
# Slower SYS128 control:8 Msym/s, all three rates.
python3 host/c3_fec_trial.py --port "$C3_PORT" --codes 5 8 6 \
  --cpw 2 --ms 3000 --slow-us 0 --build firmware/build-c3 \
  --expected-git "$C3_FW_GIT" --out results/c3-8psk-fec16/control-8m

# Decisive SYS128 target:16 Msym/s, real two-core contention, no output DMA.
python3 host/c3_fec_trial.py --port "$C3_PORT" --codes 5 8 6 \
  --cpw 1 --ms 3000 --slow-us 0 --build firmware/build-c3 \
  --expected-git "$C3_FW_GIT" --out results/c3-8psk-fec16/target-16m

# Required negative:5ms busy work/frame, sliced to16us, must miss cadence.
python3 host/c3_fec_trial.py --port "$C3_PORT" --codes 5 \
  --cpw 1 --ms 3000 --slow-us 5000 --build firmware/build-c3 \
  --expected-git "$C3_FW_GIT" --out results/c3-8psk-fec16/slow-negative

# After short positives pass, repeat each surviving rate for60s.
python3 host/c3_fec_trial.py --port "$C3_PORT" --codes 5 8 6 \
  --cpw 1 --ms 60000 --slow-us 0 --build firmware/build-c3 \
  --expected-git "$C3_FW_GIT" --out results/c3-8psk-fec16/qual-60s
```

Manual equivalents are `c3fec 5 3000 0 2`, `c3fec 5 3000 0 1`, `c3fec 8 3000 0 1`, `c3fec 6 3000 0 1` and `c3fec 5 3000 5000 1`. Use `info` and `stackcheck` before and after. On the coordinator's independently enabled SYS256 image, use d2 for16 Msym/s; C3 neither enables nor configures that clock.

A positive must exit0 with no runner faults, `errors=late_frames=max_lateness_us=0`, `published_frames=consumed_frames=frames_target`, exact independent ring CRCs/group counts/partial fill, parity-owned per-core frame counts, finite utilization and a maximum core0 service gap≤128us. At SYS128/d1,3000ms gives2162 frames and approximately3.000351s total elapsed;60s gives43254 frames and approximately60.000092s. Device output carries each core's stage totals, maximum frame work/wall time and maximum slice. Divide stage totals by that core's completed frames to obtain measured cycles/frame; compare the sum plus supervision to355104 cycles per alternate frame. A short pass does not establish a60s pass, and a CPU-only pass is admission to B6 integration, not transmitter qualification.

The negative must return `late_frames=1`, deadline error and positive lateness, stop further job releases, drain its bounded active slots and retain correct final ring CRCs. The runner exits0 only when that expected failure is detected; a clean negative is a failed control. Preserve unsuccessful rates and their raw output instead of relaxing deadlines or dropping their workload.

## Ingress, risks and rejected routes

At16 Msym/s the required BB data rates are30.970533/38.756421/34.840407 Mb/s for2/3,5/6,3/4. The existing32 MHz PV-SPI emulator's30.8697 Mb/s cap is lower even than2/3 (by0.1008 Mb/s). Its current capacity is therefore a binding ingress gate for a real sustained link, regardless of a C3 memory-source pass. Moving to memory removes that rate limiter for a compute experiment and excludes PV-SPI message receive/CRC/ownership cost. A faster real source must budget that work separately; C3 implements no faster wire protocol.

Other risks are shared SRAM/table bandwidth, XIP contention between the new flash leaves, USB/interrupt tails, the newly sliced degree192 path and loss of compute margin when output DMA takes bus priority. The earlier31% BCH placement effect and roughly6-point DMA-priority penalty cannot be assumed absent here. C3 adds no such penalty to its measured counter: it has no output DMA. B6's SM/channel use, finite hardware credits, FIFO starvation protection, capture and real clock/data behavior remain separate integration gates.

Rejected: reusing global `P` from both cores (data races), serializing LDPC under a lock (hides available frame parallelism), separate signed-plane conversion (retains the measured3.45–3.59 cycles/symbol), independently padded frame outputs (wrong waveform), two full extra static encoder contexts (SRAM), and replaying immutable pointers as “live” output. No host-native runtime is reported as an M33 cycle measurement. No general impossibility proof is claimed from the old stage table: faster packing, placement or different encoder code could change CPU fit.

Intended small logical commits for the coordinator; none was created:

1. `firmware/src/dvbs2_8psk.{c,h}`, `host/test_dvbs2_8psk_codes.py`: **Fuse sliced 8PSK PL mapping into packed axis-code streams.**
2. `firmware/src/dvbs2.{c,h}`, `host/native/dvbs2_parallel_test.c`, `host/test_dvbs2_parallel.py`: **Add bounded concurrent FEC workspaces and degree192 frontend.**
3. `firmware/src/c3_fec.{c,h}`, `firmware/src/bench_c3_fec.c`, `firmware/src/main.c`, `firmware/CMakeLists.txt`, `host/native/c3_pipeline_test.c`, `host/test_c3_pipeline.py`, `host/c3_fec_trial.py`, `host/test_c3_fec_trial.py`, `host/test_c3_bench.py`, `Makefile`: **Benchmark frame-parallel GCS FEC at the packed-code frame cadence.**
4. `docs/rd/c3-8psk-fec16.md`: **Record C3 budgets, native verification and device measurement gates.**
