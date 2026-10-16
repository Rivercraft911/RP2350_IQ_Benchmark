# C7: private BCH/transpose copies and three-table BCH

C7 makes the placement problem and an instruction-count experiment measurable in one
image. **Native correctness and both required builds pass; no C7 device cycles have been
measured.** In particular, the ≤65k-cycle BCH target for normal 2/3 is an estimate to test,
not an achieved result. No Pico, flash, serial port or hardware transport was accessed.
Git metadata and existing result evidence were not changed.

Read this with [the bank measurements](../../results/ldpc-banks/2026-10-15/README.md),
[C4](c4-fec-speed.md), [R2](r2-hw-offload.md) and
[the timing notes](../rp2350-notes.md). Inherited measurements are BCH ≈84k cycles alone
and ≈152k with both workers fetching scratch Y for normal 2/3, and LDPC transpose/pack
≈26.8k. Those are earlier image measurements, not measurements of this change.

## What is selectable

The C4 bits 1/2/4/8/16 retain their meanings and command defaults. New bits are independent:

| Bit | Selection | Off | On |
|---|---|---|---|
| 32 | Private BCH instructions | Existing selection; bit 4 chooses scratch Y | Copy the whole existing t8/t10/t12 leaf into each worker's own striped SRAM group |
| 64 | BCH multiword loads | Existing scalar-load leaf | New byte-table leaf using paired LDM loads; same original table |
| 128 | Private transpose instructions | Existing scratch-X transpose | Each worker calls its own immutable striped-SRAM copy |
| 256 | Three-table BCH | Four byte-table lookups per input word | 11/11/10-bit tables and new leaf; `c3pair` layout 3 only, implies private BCH copies |

Bit 32 supersedes bit 4 for BCH execution. Bit 64 alone runs the new source leaf from XIP;
combine it with 32 for the SRAM experiment. Bit 256 and 64 together are rejected. Bit 128
works with either LDPC mode. `c3fec` accepts 0..255; `c3pair` accepts 0..511 subject to the
constraints above. `c3pair` now also accepts normal 8/9 and 9/10 (codes 9/10) for t8 checks.

Useful masks: 31 = C4 control; 63 = 31 + private BCH; 191 = 63 + private transpose;
255 = 191 + LDM BCH; 447 = 191 + three-table BCH. Mask 27 retains the prior striped t10 / flash-C
t12 control. Defaults produce the same bits, but extra dispatch and changed linked layout
can alter timing. Use same-image A/B measurements.

The frontend captures an optional leaf and table per worker. The LDPC state similarly
captures its transpose entry; no per-core global selector is changed during encoding.
New copy APIs return private entries without modifying the LDPC prepared default. Copies
validate alignment and capacity, copy only the complete self-contained function, and issue
DSB/ISB before worker release. They contain no literals or external branches. In particular,
we copy the complete scratch-Y t10 function, not the small striped continuation stub which
branches into another function. Configuration and arenas must stay fixed until both workers
finish. Native builds execute corresponding C recurrences, not machine code from writable data.

All new algorithms handle degree 128/160/192, arbitrary incoming remainders, zero-length
calls and uneven slices. Existing byte-tail handling appends the final 0/8/16/24 input bits.
Other BCH degrees, including short-frame degree 168, keep the old APIs; new preparation
rejects them. The checked EN302307-1 V1.4.1 §5.3 Table 5a, printed p22, gives t10 for 2/3 and 5/6,
t12 for 3/4 and t8 for 8/9 and 9/10. Tests cover all eleven normal rates, not just these five.

## Instruction work and estimates

The original table is four planes ×256 rows ×8 words  = 32,768 bytes, with 4/5/6 live words
per row. LDM reduces instruction fetches while retaining all 16/20/24 table-word reads.
It is not a claim that one LDM transfers several words in one M33 cycle.

The wider table stores three planes:2048 rows for x[31:21],2048 for x[20:10],1024 for
x[9:0], with 4/5/6 words per row. Setup constructs each entry by XORing contributions from
the unchanged byte tables. An independent bit-serial polynomial checks these entries and the
assembly results. It removes one table lookup and4/5/6 table-word loads per input word.

Counts below come from the assembled instructions. The transfer-expanded column charges
one cycle per LDM register instead of one per LDM instruction; it is an instruction model,
not a cycle measurement. Prologues, sliced calls, pipeline stalls and branch refill are extra.

| Degree / t | Original scalar loop instructions | LDM scalar / transfer-expanded | Three-table scalar / transfer-expanded | Table loads, old → three-table |
|---|---:|---:|---:|---:|
| 128 /8 |46|39 /47|27 /36|16 →12|
| 160 /10 |54|46 /54|33 /45|20 →15|
| 192 /12 |63|52 /64|40 /55|24 →18|

New loop heads are four-byte aligned. The t10 three-table schedule hides input-load and
last-LDM-register dependencies with independent instructions; address arithmetic uses
LSL#2 additions for the 20-byte row stride. Plane-offset additions are separated from their
consumers. At least one extra cycle for a taken loop branch gives a provisional46-cycle
interior per word:1345 whole words ×46 =61,870 cycles at 2/3. Entry/exit,22 slices, frontend
dispatch and the append bring an optimistic zero-wait planning estimate to **63–65k**.
The analogous estimates are about79–82k at 5/6 (1682 whole words plus16 tail bits), and
86–91k at 3/4 (1512 whole words plus24 tail bits). t8's table3 interior is roughly38 cycles
including its exposed complex-address dependency, about70–73k for 8/9 and 9/10.

These estimates exclude second-core bus contention, interrupts and XIP misses. Even a few
extra cycles per word can miss65k. The same-table LDM variant retains a roughly54-cycle
transfer-expanded t10 interior before branch/dependency costs; it may help concurrent
instruction fetch, but does not establish the 65k target. Compare63 vs255 only after
separately checking transpose63 vs191;191 vs255 isolates LDM.

The checked RP2350 datasheet build2025-07-29 §3.7.4.9, pp137–144, explicitly limits the
instruction timing tables to one core, no cache misses and no DMA. §§3.7.4.9.1–.2 pp138–139
identify complex shifts and load-result use penalties; §3.7.4.9.5 covers multi-register
loads; §3.7.4.9.7 pp140–142 covers branch and instruction alignment; §3.7.4.9.12 p144 covers
instruction/data SRAM contention. Host elapsed times are not used as M33 evidence.

For the complete normal 2/3 pipeline, replacing inherited84k BCH by estimated64k would
reduce its ≈348k stage sum to≈328k before supervision and concurrent slowdown. The strict
16-Msym/s budget is355,104 cycles per core per alternate assigned frame at SYS128. This
arithmetic does not establish a full transmitter fit. The wide table also has no allocation
in the current output path.

## Storage and exact placement

No static SRAM, DMA channel, PIO SM or PIO instruction allocation is added. Runtime storage
borrows the existing idle-only buffers. The normal C3 context grows to55,288 bytes, including
one 1024-byte code reservation for core0 and per-worker pointers; core1 uses cap_buf's first
1024 bytes. Each reservation contains 512 bytes for BCH and 512 for transpose. Job/frontend/
LDPC state sizes are19,336/48/92 bytes in the ARM Release build.

| Function | Bytes copied |
|---|---:|
| Existing t8 /t10 /t12 BCH |208 /240 /258|
| New LDM t8 /t10 /t12 |186 /210 /224|
| New table3 t8 /t10 /t12 |132 /156 /176|
| Existing transpose |308|

`c3pair` layouts 0/1/2 retain their meanings for jobs/LDPC. Regardless of those layouts,
private C7 instructions use one arena in each striped group. New layout 3 always reserves
ring_buf[0..122879] for the maximum table, including its off control. Both jobs and shared
Gold/LUT/LDPC resources occupy a compact 54,932-byte context in cap_buf. It fits the56-KiB
buffer with 2412 bytes remaining. Shared worker counters start at ring byte 122880; core0's
code arena starts at 124928 and ends 125952. Tables, counters and code do not overlap.
Hammers are rejected for layout 3. Layout1 already rejects hammers.

| Table degree | Extra table SRAM | Original table retained | Table-word read bytes/frame |
|---|---:|---:|---:|
|128|81,920|32,768|86,208 at 8/9;87,264 at9/10|
|160|102,400|32,768|80,700 at 2/3;100,920 at 5/6|
|192|122,880|32,768|108,864 at 3/4|

Read-byte counts include whole-word kernels only; final byte tails add small byte-table reads.
The wide table lives in SRAM0–3 and both jobs in SRAM4–7, so it also changes data placement.
Compare layout 3 off/on to keep jobs fixed; even then the table source changes bank group.
It consumes the waveform LUT reservation and cannot coexist with B6's present OUT8 shaping
table. It is a diagnostic candidate for a future memory redesign, not an integrated speedup.

The final experiment map (`firmware/build-exp/iqbench.elf.map`) and ARM DWARF give:

| Object or region | Address / extent |
|---|---|
| ring_buf |0x20000000..0x20020000|
| in_buf |0x20020000..0x20028000|
| cap_buf |0x20051fac..0x2005ffac|
| Existing BCH byte table |0x20071f50..0x20079f50|
| `c3fec` context |0x20010000..0x2001d7f8|
| `c3fec` core0 BCH /transpose copies |0x2001d194 /0x2001d394|
| `c3fec` core1 BCH /transpose copies |0x20051fac /0x200521ac|
| `c3pair` layouts 0–2 core0 BCH /transpose copies |0x2000850c /0x2000870c|
| `c3pair` all layouts core1 BCH /transpose copies |0x2005a4b8 /0x2005a6b8|
| `c3pair` layout 3 core0 BCH /transpose copies |0x2001e800 /0x2001ea00|
| Existing scratch-Y t10 /t8 /t12 sources |0x20081530 /0x20081620 /0x200816f0|
| Existing scratch-X transpose |0x20080498..0x200805cc|
| Core1 stack |0x20080800..0x20081000|
| Core0 stack |0x2007f000..0x20080000|
| Lower .data |0x20030110..0x2003a0d0|
| Lower .bss, after alignment |0x2003a100..0x2003fbf8|
| Minimum heap |0x2003fbf8..0x2003fff8;8 bytes remain|
| SRAM4–7 allocations |0x20040000..0x2007efe8;24 bytes below stack|
| Scratch-X /Y linked code |2032 /3688 bytes;16 /408 bytes remain|

The experiment pvflight image links with **zero** bytes after its 1-KiB lower heap; its
high allocations end 0x2007eea8,344 bytes below its stack. Any additional hot code/static
allocation requires another map check. Nothing was moved out of existing static sections;
the end symbols only expose copy extents. The new default indirect transpose call can change
baseline cycles. All addresses above exclude the Thumb entry bit; JSON leaf pointers include it.
RP2350 §4.2 pp337–338 confirms the two striped256-KiB groups and single-bank scratch addresses.

A relevant existing diagnostic bug was repaired: the who 7 transpose hammer used cap_buf+8192,
inside the high LDPC executable arena. With layout 2 it could overwrite executing instructions.
Its tile now uses cap_buf+12288, outside both code arenas and both active jobs for supported
hammer layouts 0/2. This is a correctness fix to a diagnostic, not a measured performance result.

## Native verification and build

Both `make build` and the prescribed Release experiment configure/build complete for iqbench
and pvflight without introduced warnings. The full `make check` regression passes, including
all existing references and the Markdown link check.
Reproduce the offline checks:

```sh
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
make build
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
make check
```

The new tests run within that target:

- `host/test_c7_bch.py`: all eleven normal rates, all 15,360 wide-table row indices across the
  three degrees, arbitrary incoming remainders and uneven continuation, 264 frontend runs,
  five mixed-leaf frontend pairs, 1152 portable transpose matrices, exact serial/Python BCH,
  input/output/table guards, failed-admission no-write checks and a corrupted-table control.
- `host/test_bch_fast_asm.py`: assembled M33 disassembly is interpreted, rather than a rewritten
  Python version of the algorithm. Checks real loop/branch/load semantics, all table indices,
  arbitrary remainders, zero/odd lengths, slice boundaries, exact copy extents, AAPCS registers,
  stack guards, word-aligned new loop heads and no external relocations or branch/literal targets.
  Passed 31,848 cases / 3,083,769 interpreted instructions, including 1058 actual assembled
  transpose cases against an independent bit-matrix reference (all 1024 basis bits).
  It is an instruction-semantics test, not Arm execution or silicon timing.
- `host/test_c7_pair.py`: 180 production-command runs, real pthread worker for who 2, all five
  selected rates, layouts 0–3, masks 31/63/191/255 plus same-layout 191/447 controls. Every physical
  output ring word and CRC matches independent Python. Seven invalid requests verify rejection;
  guards, worker join, selector and PRBS restoration are checked. Synthetic cycles only test
  command accounting.
- The production `c3fec` virtual-time test includes 63/127/159/191/255 for all three pipeline
  rates. Existing serial/native/independent references and default modes remain in `make check`.

The experiment image reports `02962753ad-dirty`. Exact local SHA-256 values:

- iqbench.uf2: `77dd7e3278f491b8312bf0c8ae7414d4a07a2869b644bdb3f2180df5e1d50d0d`
- iqbench.elf: `c27e2116ac0529ff3018a48eea494b2fc04daa163e0332b67baf39b272d306b3`

The dirty revision alone does not uniquely identify the changes; archive the hashes and map.

## Coordinator commands and pass criteria

Do not compare firmware images to isolate a bit. Build once, archive its map/ELF/UF2/hash,
use a fresh result directory, and run these on the coordinator's hardware session at SYS128.
The lines below are device console commands, not commands executed during this work.
Repeat each A/B in alternating order at least three times; use pairs 100 to amortize startup.
Repeat for code 8 and 6 after code 5, and code 9/10 for t8. `who 0` runs both jobs sequentially on
core0, `who 2` releases A on core0 and B on core1 together; `who 1` additionally checks sequential
execution on core1.

```text
# Placement only: scratch-Y control versus private striped copies.
c3pair 5 100 0 0 31
c3pair 5 100 0 0 63
c3pair 5 100 0 2 31
c3pair 5 100 0 2 63
# Existing striped t10 /flash-C t12 control.
c3pair 5 100 0 0 27
c3pair 6 100 0 0 27
c3pair 6 100 0 0 63
# Isolate transpose, then LDM with the same two arenas.
c3pair 5 100 0 0 191
c3pair 5 100 0 2 191
c3pair 5 100 0 0 255
c3pair 5 100 0 2 255
# Scratch-X fetch adverse control; compare LDPC substep5 (transpose/pack).
c3pair 5 100 0 7 63
c3pair 5 100 0 7 191
# Wider table: same layout 3 jobs/rings/code destinations on both sides.
c3pair 5 100 3 0 191
c3pair 5 100 3 0 447
c3pair 5 100 3 2 191
c3pair 5 100 3 2 447
# Invalid wide allocation: must return an error before writing buffers/starting workers.
c3pair 5 1 0 2 447
```

Every successful pair must retain these **independent** final-ring CRCs, irrespective of
pair count, layout, who and option selection. The command intentionally leaves first boundary
words deferred and does not publish/join frames; these differ from `c3fec` CRCs.

| Code | crc_a | crc_b |
|---|---:|---:|
|5 (2/3)|1273684834|2540800392|
|6 (3/4)|4157714373|1725042067|
|8 (5/6)|1863126860|2777209518|
|9 (8/9)|1538593740|3547307867|
|10 (9/10)|2021468233|2222914226|

Require SYS128, correct requested options/layout/who, 100 runs per job (200 combined for who 1),
and the expected CRC pair. With 32/128, printed `bch_leaf`/`transpose_leaf` must lie in different
striped groups, match the addresses above plus Thumb bit, and avoid scratch. Table3 must report
its exact address 0x20000000 and degree-specific byte count. Preserve both jobs' stage and
LDPC-substep results; evaluate the slower core, not only the average.

Placement acceptance: private copies should remove the large scratch-Y penalty, with concurrent
BCH no more than 15% above its solo value, and solo performance within 5% of the fastest old
same-image choice. Require a repeatable reduction in the who 7 transpose/pack penalty when 128
is enabled. These are proposed acceptance gates, not observed outcomes. Failure is useful:
record residual table/stack contention rather than claiming the code copy solves it.

Instruction target: `c3pair 5 ... 447` should give **≤65,000 BCH cycles on each concurrent
worker**, not only solo, while keeping the correct CRCs. For proportional targets use ≤81,300
at 5/6 (Kbch ratio 53840/43040, rounded) and ≤87,800 at 3/4 (also charging six rather than five live words).
The latter is a planning target, not an exact scaling law. Table placement may prevent all
three targets despite the reduced instruction work.

The adverse controls are 31 vs 63 under who 2, and who 7 with transpose off/on. They should retain
correct CRCs while revealing the shared scratch fetch cost. The invalid layout command above
must fail; the native used-table corruption control must also detect a parity mismatch.

After choosing the best **non-wide** mask, check the full cadence without an output engine:

```text
c3fec 5 3000 0 1 128 31
c3fec 5 3000 0 1 128 191
c3fec 5 3000 0 1 128 255
c3fec 5 60000 0 1 128 191
c3fec 5 60000 5000 1 128 191
```

Repeat the best mask for 8 and 6, and test cpw2 as a lower-rate control. The 5000 us case is the
explicit deadline-failure control: it must fail the deadline/cadence checks while draining
and restoring selectors. The existing runner now accepts the new masks:

```sh
python3 host/c3_fec_trial.py --ms 3000 --codes 5 8 6 --cpw 1 --slow-us 0 \
  --options 31 191 255 --gap-us 128 --build firmware/build-exp \
  --expected-git 02962753ad-dirty --out results/c7-bch-place/2026-10-16/qualify-1
```

For a cadence pass require independent code CRCs for the actual published frame count,
zero errors/late frames, target = published = consumed, both cores' slices ≤128 us, service gap ≤128 us,
and full utilization/deadline checks from the runner. Do not relax gap 128 for qualification.
Neither a successful `c3pair` nor `c3fec` proves output-DMA service or integration with B6's
core1 grant worker. Bit 256 is deliberately unavailable in `c3fec`.

## Alternatives evaluated and limitations

- **Interpolator-built addresses (R2):** approximately 11 address instructions become 8 SIO
  instructions before setup/restoration and extra register pressure. The20 t10 loads remain.
  A few thousand cycles saved is plausible; this does not independently close84k→65k. No
  interpolator state is claimed or mutated by C7. RP2350 §3.1.10 pp44–45 describes one-cycle
  SIO accesses and next-cycle results, PEEK versus POP mutation; it is not a GF/XOR engine.
- **Two input words per loop:** ordinary unrolling only amortizes SUBS/BNE, approximately
  1–1.5 cycles per word, and the remainder recurrence still serializes consecutive words.
  Slicing-by-eight retains 40 t10 word loads per 64 input bits and adds planes/register pressure.
  It has no demonstrated advantage over the three-table design; no unrolling speedup is claimed.
- **Other splits:** two 16-bit planes need 2,621,440 packed bytes at t10, impossible on chip.
  Four wider planes retain 20 loads and cost more SRAM. Eight nibble planes need only 2560
  packed bytes at t10 but double reads to 40 words/input word. The balanced11/11/10 split
  minimizes row count among three-plane integer splits of 32 bits.
- **BFI row-address construction:** direct insertion of byte indices requires aligned table
  plane bases. The current byte table is0x20071f50, not8-KiB aligned; changing alignment would
  overflow near-full high SRAM. An unaligned-base BFI scheme is not silently assumed valid.
- Code copies remove shared instruction-bank pressure; each core still reads the shared BCH
  tables, and core1's stack remains in scratch X. LDM burst traffic can compete with instruction
  fetch or other SRAM users. The same-image measurements decide whether its fetch reduction helps.
- Static headroom is extremely small. Runtime copied code is valid only while the corresponding
  idle buffers are borrowed. Table3 cannot be promoted into the live shaping engine without a
  separate SRAM allocation and scheduling design.

## Intended small commits

No commits were made. Suggested logical commits, without co-author trailers:

1. `firmware/src/dvbs2/dvbs2.c`, `dvbs2.h`, `dvbs2_asm.S`, `dvbs2_bch_fast.S`,
   `firmware/CMakeLists.txt`: **Add reentrant BCH and transpose copies plus three-plane BCH leaves**.
2. `firmware/src/c3_fec.{c,h}`, `bench_c3_fec.c`, `bench_c3_pair.c`, `main.c`,
   `host/c3_fec_trial.py`: **Expose C7 placement and BCH A/B options with bounded idle arenas**.
3. `host/native/c7_bch_test.c`, `host/test_c7_bch.py`, `test_c7_pair.py`,
   `test_bch_fast_asm.py`, `test_c3_bench.py`, `Makefile`:
   **Verify C7 recurrences, executable copy bounds and concurrent command output**.
4. `docs/rd/c7-bch-place.md`, `docs/rd/README.md`:
   **Document C7 budgets, allocation limits and coordinator qualification commands**.
