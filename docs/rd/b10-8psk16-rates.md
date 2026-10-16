# B10: B9 rate admission, SRAM hot paths and service margin

Implemented and verified offline on `rd/b10-8psk16-rates`, based on `fe3da359cc`.
**No new device throughput or service-margin result is claimed.** The Pico was
not accessed. The coordinator must establish recovery of mask 251 and qualify
5/6 and 3/4 with the same-image matrix below. Existing result evidence and Git
metadata were not modified; no commits were made.

The command is now:

```text
b9 cpw ms invert prio slow_us options [code] [sram_hot]
```

The original six arguments keep their defaults `1 3000 0 13 0 251`; omitted code
is still normal 2/3 (5). Code 8 admits normal 5/6 and code 6 normal 3/4.
`sram_hot` defaults to 1; 0 selects the flash implementations in the **same image**
with identical reservations. Both selections use the improved B9 slice bounds
and supervisor. `best` remains 251. Ordinary startup, `c3fec`, `c3pair` and their
option defaults are retained. All B9 work remains experiment-only.

## Regression: what the comparison establishes

Pristine `e6d8776b0f` and `fe3da359cc` were extracted with read-only `git archive`
to `/private/tmp/b10-regression-main` and `/private/tmp/b10-regression-current`.
Both experiment images were built with SDK 2.2.0 / GCC 14.2.1 and the same Release,
experiment, high-stack and `PICO_NO_PICOTOOL=ON` settings. The maps and decoded
instructions were compared, rather than comparing current dirty code with an
old result image.

The historical measurements are in
[B9 results](../../results/b9-8psk16/2026-10-16/README.md) and
[C9 results](../../results/c9-fec-fit/2026-10-16/README.md). Mask 251 gained about
6.8k GCS cycles and 5–6k LDPC cycles per frame, despite the C9 bits being off.
The static comparison rules out a moved SRAM table, changed B9 job layout, or a
large new per-word computation:

| Object | Main and pristine C9-merged placement |
|---|---:|
| `ring_buf`, B9 context | `0x20000000`, `0x20010000` |
| `in_buf`, link ring | `0x20020000`, `0x20028000` |
| `cap_buf` | `0x20051fac` |
| LDPC offset / row metadata | `0x2006df8c`, `0x2006ef8c` |
| BB scrambler | `0x2006ff8c` |
| SRAM frontend entry | `0x20036ed0` |

The complete GCS body is 928 bytes in both images. Its start moves from
`0x1002df74` to `0x1002e8cc`, a **0x958-byte flash displacement**. Its decoded
body, including the ten-instruction payload loop, is unchanged apart from the
external CRC call target. This displacement changes its cache-bank parity
(address bit 3) and cache sets. C9 adds mode-3 LDPC selection, but its twelve
packing dispatches add fewer than 100 instructions/frame; this cannot count for
the observed multi-thousand-cycle losses. The retained C5 assembly pack body is
unchanged.

| Flash function | Main start / bytes | Pristine C9-merged start / bytes |
|---|---|---|
| GCS helper | `0x1002df74 / 928` | `0x1002e8cc / 928` |
| `c3_job_step` | `0x1002e384 / 520` | `0x1002ecdc / 554` |
| C5 scheduler | `0x10012c7c / 532` | `0x10012c7c / 572` |
| LDPC prefix | `0x10012a84 / 504` | unchanged |
| B9 core-0 work | `0x100314bc / 412` | `0x10032710 / 412` |
| B9 core-1 loop | `0x100316f0 / 376` | `0x10032944 / 376` |

GCS has 313 decoded instructions in either image; C3 dispatch grows 175 to 189
and the C5 scheduler 191 to 203 static instructions. The copied C5 group, rows
and original pack sections remain 6,462 / 126 / 1,574 bytes with unchanged
destinations. In the completed priority-13 records, main's per-frame means were
GCS **23,063 / 22,909** and LDPC **88,510 / 98,442** (cores 0 / 1). The C9-merged
3-s run gives **29,890 / 29,920** and **93,574 / 104,045**. BCH changes by only
0.9–1.4k and mapping by less than 0.7k. The 0.94-s source-empty record shows the
same stage pattern.

The remaining mapped explanation is the relocated flash working set and its
interaction between cores, rather than additional GCS payload arithmetic or
SRAM placement. RP2350's checked datasheet text, sections 4.4 / 4.4.1, describes
two 8-KiB cache banks interleaved by 8-byte line, a 16-KiB two-way cache, and
one-cycle hits. **Maps do not measure cache misses or bank stalls.** They do not
prove which cache sets or relative execution phases account for every lost
cycle. A claim that the exact 6.8k/5–6k has already been recovered would be
unsupported without the requested hardware A/B.

There are concrete new aliases in the pristine merged map: GCS loop line
`0x1002e948`, B9 core-1 line `0x10032948`, alarm IRQ line `0x10034948` and USB
task line `0x10038948` select cache set **0x129**, with only two ways. Main's GCS
loop starts at `0x1002dff0` (set 0x3fe), outside main's B9 core-1 set range.
The merged LDPC caller at `0x1002ed88`, scheduler at `0x10012d88` and USB code
at `0x10038d88` share set **0x1b1**. Set selection uses address bits 12:3,
checked against section 4.4.1.1; this is a mapped conflict opportunity, not a
record of their simultaneous execution. New code changes flash layout again,
so the same-image placement test isolates SRAM execution without promising to
reproduce every old-image eviction.

The implemented fix removes XIP from the repeated GCS payload body and the C5
LDPC scheduler/prefix body, instead of relying on flash padding:

- `b10_hot.S` is a 32-byte relocatable GCS leaf, with seven instructions/payload
  word. It copies from a 512-byte reservation containing two copies of the
  256-byte source period, reverses the word and XORs the scrambler. A slice of at most 64 words
  starting at offset 0..255 stays inside that reservation. Header/partial-word
  generation still uses the existing independently checked code.
- The existing LDPC prefix and C5 scheduler are collected into a 1,076-byte
  flash section and copied together, including their literal pools. The only
  direct BL stays within that section; leaf calls use BLX registers. The entry
  offset is retained. DSB/ISB follows both copies.
- B9 stores both copies in its existing borrowed SRAM tail. Function pointers
  are passed to the B9 stepping API; other schedulers keep their original API.
  `sram_hot=0` passes NULL callbacks, allowing controlled placement comparison.

The new GCS loop removes three decoded instructions per complete payload word:
**4,026 / 5,037 / 4,527 instructions/frame** at 2/3, 5/6, 3/4 respectively
(1,342 / 1,679 / 1,509 complete words after the first three header words).
The seven-instruction loop has an optimistic eight-cycle floor including a
taken branch; load dependencies, unaligned accesses and SRAM arbitration add
cost. The three removed instructions/word suggest roughly **3–6k cycles/frame**
of GCS saving after callback/header overhead, excluding any removed XIP stalls
or new SRAM contention; this is not a bound. This is an M33 instruction estimate using
[rp2350-notes.md](../rp2350-notes.md), not a timing measurement. The scheduler
copy preserves its arithmetic; its benefit is the changed instruction source,
not an invented instruction saving.
The loop target is explicitly word-aligned. Checked datasheet section
3.7.4.9.7 otherwise charges another cycle when the first instruction at an
unaligned branch target is 32 bits. The decoded-instruction test enforces this
alignment; the entry-only padding does not consume per-word instructions.

## Service gate: mechanism and changes

The previous loop did work before checking service time. A slice could begin
just before the 32-us service interval expired. Publication, release, capture
setup and interrupt time could then precede another slice before the next due
check. The gate is the time between service **starts**, so none of those costs
can be omitted.

The failed merged runs' core-0 slice maxima were 8,245/8,989 cycles (64.4/70.2 us),
yet gaps were 132/129 us. Main's 108–113-us gaps accompanied roughly 9k-cycle
slice maxima. These totals cannot identify the precise stage or IRQ latency;
in particular, it would be wrong to attribute every gap to MAP alone. Mask 251
has fused BB construction, so the baseline's final whole-frame BB pass is not
the explanation for its regression.

B9 now checks whether service is due **before** starting work. A service-only
iteration returns to the due check before admitting another slice. The unchanged
fault condition remains `gap > 128 us`. B9-specific slices are:

| Stage | Before | B10 B9 |
|---|---|---|
| GCS with bit 8 | at most 64 words | at most 64 words |
| GCS without bit 8 | final slice includes whole BB scramble/zero pass | second pass at most 64 words |
| BCH without 4096 | 64 words | 64 words |
| BCH with 4096 | 128 words | **96 words** |
| LDPC with 4096 | two units | two units |
| C6 MAP | 32 words | **16 words** |
| Other MAP | eight words | **four words** |

C6 mapping now takes 43 calls/frame; the ordinary mapper takes 169. BCH96 adds
exactly four calls/frame relative to BCH128 at all three admitted rates.
Charging 80–150 cycles/wrapper estimates 0.32–0.60k added cycles/frame, before
stalls. This retains most C9 scheduling savings. Existing C9 BCH64 maxima near
5.2k cycles suggest a provisional 7.8k-cycle BCH96 slice (61 us), giving about
93 us with the 32-us interval before remaining overhead. LDPC2 can process up to
104/104/96 edges at these rates: roughly 7.5k/7.5k/6.9k instruction slots for
the large group body, before metadata, dependencies and arbitration. These are
planning estimates, not worst-case latency proofs.

New diagnostics include per-stage maximum slice cycles, maximum service-body
cycles, count of gaps over 96 us, and the elapsed time of the maximum gap. IRQ
body maxima remain available; they still do not measure completion-to-entry
latency. Stage maxima include prefill/drain, while gap/service counters describe
live output. The coordinator can now distinguish a long encoder slice from
service setup or a startup-only gap.

## Rates, cycle budget and SRAM

The supplied EN 302 307-1 V1.4.1 text, section 5.3 Table 5a, was checked directly:
Kbch is 43,040 / 53,840 / 48,408. Subtract the 80-bit BBHEADER. Each normal
8PSK pilot frame still contains 22,194 symbols; at SYS128/cpw1 it takes 177,552
cycles. Each alternate-frame worker has **355,104 cycles = 2,774.25 us**.

| Code | Rate | Payload bits/frame | Calculated payload at 16 Msym/s |
|---:|---:|---:|---:|
| 5 | 2/3 | 42,960 | 30.97053 Mb/s |
| 8 | 5/6 | 53,760 | 38.75642 Mb/s |
| 6 | 3/4 | 48,328 | 34.84041 Mb/s |

Measured C9 CPU-only pair work was about 304k at 5/6/6395 and 312–313k at
3/4/12795. Adding B9's historical roughly 9% supervision is another 32k cycles
per assigned frame: provisional totals **336k / 344–345k**, with just
19k / 10–11k cycles remaining. Those are mixed historical arithmetic budgets,
not predictions of B10 maxima. Shorter slices and service rechecking add work;
the SRAM GCS path removes counted instructions. Arbitration can change both.
Two complete frames are still published before output, and frames 2/3 start
during prefill; no `c3fec` first-frame deadline is transplanted into B9.

The mode-3 LDPC reservation grows from 8,192 to **9,728 bytes** (9,448 used).
Both BCH and transpose reserve separate 512-byte slots per core. The compact
table follows the capture, keeping the old capture base unchanged. The final
experiment ELF/map and ARM DWARF give these half-open addresses:

| Reservation | Address / bytes |
|---|---|
| OUT8 table | `0x20000000..0x20010000`, 65,536 |
| B9 context | `0x20010000..0x2001f5b8`, **62,904** |
| Jobs 0 / 1 | `0x20010000` / `0x20014be8`, 19,336 each |
| Mapper configuration / Gold arena | `0x200197d0` / `0x20019824..0x2001adc4` |
| C6 mapper table/code arena | `0x2001adc4..0x2001c154`, 5,008 |
| Shared C5/C9 LDPC arena | `0x2001c154..0x2001e754`, 9,728 |
| Core-0 BCH / transpose | `0x2001e754..0x2001e954` / `0x2001e954..0x2001eb54` |
| Duplicated GCS pattern | `0x2001eb64..0x2001ed64`, 512 |
| Hot-code reservation | `0x2001edb0..0x2001f5b0`, 2,048 |
| GCS copy / scheduler copy | `0x2001edb0..0x2001edd0` / `0x2001edd0..0x2001f204` |
| Copied scheduler entry | `0x2001efc8` (Thumb bit omitted) |
| Packed I / Q rings | `0x20020000..0x20024000` / `0x20024000..0x20028000` |
| Preserved link ring | `0x20028000..0x20030000` |
| Core-1 BCH / transpose | `0x20051fac..0x200521ac` / `0x200521ac..0x200523ac` |
| Pins32 capture | `0x200523ac..0x200563ac`, 16,384 |
| Compact BCH table maximum | `0x200563ac..0x2005c3ac`, 24,576 |

The borrowed tail has **2,632 bytes** remaining. Capture and compact table do
not overlap; both remain inside the existing 57,344-byte `cap_buf`. Compact
table usage is 20/20/24 KiB for these rates. No new static SRAM, DMA channel,
PIO SM, or PIO instruction is allocated. All masks and both placement selections
reserve identical context space. Global experiment free space stays **4 bytes**
below the lower-SRAM heap limit, **24 bytes** below the 4-KiB core-0 stack,
**16 bytes** before the core-1 stack, and **8 bytes** at scratchY's end.
The command's ARM prologue reserves **984 bytes** (36 saved-register bytes plus
948 local bytes), up from B9's 912. This excludes callees, dispatch and IRQ
frames; the coordinator's final stack paint remains required.

The four-chain waveform graph remains 13 DMA channels and 10 SMs including
capture. Its measured row-loop estimate remains about 14 cycles against a
16-cycle deadline at cpw1: the two-cycle DMA margin is not improved by CPU
instruction counts. Additional SRAM instruction fetches can affect that margin.

Admission accepts valid C9 masks 0..16383, excludes wide table bit 1024, and
rejects compact-table bit 2048 together with LDM bit 256. It rejects unsupported
codes and invalid placement flags before borrowing storage or launching work.
New result fields record the selected code, Kbch/payload, arenas and placement.

## Offline verification and reproduction

Both required builds and the full `make check` pass; no new compiler warnings.
`PICO_NO_PICOTOOL=ON` honors the explicit tool prohibition: SDK output is
ELF/BIN/HEX/map, with no UF2 packaging invocation. The coordinator must package
the actual matching image rather than reuse an old UF2.

```sh
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DPICO_NO_PICOTOOL=ON
make build
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON -DPICO_NO_PICOTOOL=ON
make build BUILD=firmware/build-exp
make check
```

- The production B9 command runs under native hardware shims: eight tests,
  49 command executions. Twenty-one rate/placement positives include all three
  rates, 251/4603/6395/12795, baseline at cpw2 and transpose combinations.
  Guards, ring seams, MAP credit, independent final ring CRCs, restoration and
  deterministic drain pass. Synthetic 80-us capture setup passes after a time
  recheck; 160-us setup faults the unchanged 128-us gate and drains exactly.
- The native C3 production graph retains 95 masks x three rates x 16 frames and
  adds five B9 masks x three rates x 16 frames. Both NULL and hot callbacks are
  checked. Bounds on GCS words, BCH words/call counts and MAP calls are asserted;
  output is compared to the existing independent BCH/LDPC/PL reference.
- `test_b10_hot.py` checks the compiled ARM scheduler's relocation closure and
  executes the actual decoded GCS instructions for **16,640** cases: every
  starting offset 0..255 and count 0..64, random scrambler, guards and AAPCS.
  It rejects external relative calls/literals and verifies the copy fits 2 KiB.
  `python3 host/test_b10_hot.py --elf firmware/build-exp/iqbench.elf` also checks
  the linked section boundaries, entry, direct branches and literal pools;
  the object check verifies all four compiled jump-table targets.
- B9 host verification has 13 tests plus the runner self-test. Capture references
  build the selected code independently through GCS/BCH/LDPC/PL and direct FIR;
  all sampled bus bits, frame seams, clock phase, large cursors, payload metadata
  and corrupt records are checked. Legacy 2/3 records remain readable.
- The original `c3_job_step` machine bytes remain identical under the native
  ARM compilation check: SHA-256
  `c7e361140d0b85f839ee3970bc861fd2e08449493b23e9dafaeeba73ad89f7e5`.
  Full regression checks include assembly, existing transmitters and links.

Native elapsed time and virtual scheduler delays are not M33 measurements.

Final local artifact SHA-256 (dirty Git identity alone is insufficient):

```text
build/iqbench.elf      808e4ce62b580e2d0948ba103bf8a41194c0eba8470cfb23e21e279b7c5c51ac
build-exp/iqbench.elf  4af316996d2eeb421869f8ea3e655c430c10133aaa91747179274f1df92e97f6
build-exp/iqbench.bin  be56cbe84dd54ae01a0043a3536518f886ca311f0c2eeafee2b964930894d0a7
build-exp/pvflight.elf 584e35c49b09c67fd82f6072b6dc914f151c281ef3d617ebc374969fd1a6b526
```

## Coordinator commands and acceptance

**Not executed here.** Use one experiment image at SYS128, explicit coordinator
port, fresh result directories, matching ELF/map/source diff and pinned image
identity. Run `info` and `stackcheck` before and after. The trailing arguments
below are code and placement, not another rate or clock divider.

First isolate the placement fix with unchanged mask 251 and the improved B9
supervisor. Repeat off/on/off at least three times; compare each worker's stage
cycles divided by its frames, not aggregate prefill-inclusive totals alone:

```text
info
stackcheck
b9 1 3000 0 13 0 251 5 0
b9 1 3000 0 13 0 251 5 1
b9 1 3000 0 13 0 251 5 0
c3pair 5 100 0 2 251
c3pair 8 100 0 2 251
c3pair 8 100 0 2 4603
c3pair 8 100 0 2 6395
c3pair 6 100 0 2 251
c3pair 6 100 0 2 4603
c3pair 6 100 0 2 12795
c3fec 5 3000 0 1 128 251
c3fec 8 3000 0 1 128 6395
c3fec 6 3000 0 1 128 12795
```

`c3fec` still has no prefill: failure of its initial code8/code6 deadline is a
retained negative/control, not a B9 failure or permission to move its epoch.
Pair independent CRCs are code5 `1273684834/2540800392`, code8
`1863126860/2777209518`, code6 `4157714373/1725042067`.

Then same-image rate A/B at priority 13, 3 s first:

```text
b9 1 3000 0 13 0 251 5
b9 1 3000 0 13 0 6395 5
b9 1 3000 0 13 0 251 8
b9 1 3000 0 13 0 4603 8
b9 1 3000 0 13 0 6395 8
b9 1 3000 0 13 0 251 6
b9 1 3000 0 13 0 4603 6
b9 1 3000 0 13 0 12795 6
```

Repeat the passing short cases for 60 s, keeping failures as failures. The
baseline/candidate long lines are:

```text
b9 1 60000 0 13 0 251 5
b9 1 60000 0 13 0 6395 5
b9 1 60000 0 13 0 251 8
b9 1 60000 0 13 0 4603 8
b9 1 60000 0 13 0 6395 8
b9 1 60000 0 13 0 251 6
b9 1 60000 0 13 0 4603 6
b9 1 60000 0 13 0 12795 6
```

Priority controls, source starvation, phase negatives and slow baseline control:

```text
b9 1 3000 0 0 0 251 5
b9 1 3000 0 0 0 6395 8
b9 1 3000 0 0 0 12795 6
b9 1 60000 0 0 0 251 5
b9 1 60000 0 0 0 6395 8
b9 1 60000 0 0 0 12795 6
b9 1 3000 0 13 5000 251 5
b9 1 3000 0 13 5000 6395 8
b9 1 3000 0 13 5000 12795 6
b9 1 3000 1 13 0 251 5
b9 1 3000 1 13 0 6395 8
b9 1 3000 1 13 0 12795 6
b9 1 3000 0 13 0 0 5
b9 2 3000 0 13 0 0 5
stackcheck
```

Use `host/b9_trial.py` to check every capture and ring; raw command output alone
is insufficient. Examples (the runner never builds or flashes):

```sh
B10_PORT='<coordinator serial port>'
B10_REV=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-exp/git_rev.h)
python3 host/b9_trial.py --port "$B10_PORT" --expected-git "$B10_REV" \
  --build firmware/build-exp --ms 3000 --cpws 1 --codes 5 --options 251 --sram-hot 0 \
  --priorities 13 --out results/b10/coordinator-placement-off-1
python3 host/b9_trial.py --port "$B10_PORT" --expected-git "$B10_REV" \
  --build firmware/build-exp --ms 3000 --cpws 1 --codes 5 --options 251 --sram-hot 1 \
  --priorities 13 --out results/b10/coordinator-placement-on-1
python3 host/b9_trial.py --port "$B10_PORT" --expected-git "$B10_REV" \
  --build firmware/build-exp --ms 3000 --cpws 1 --codes 5 8 6 --options 251 4603 6395 12795 \
  --priorities 13 --out results/b10/coordinator-rates-ab-3s
python3 host/b9_trial.py --port "$B10_PORT" --expected-git "$B10_REV" \
  --build firmware/build-exp --ms 60000 --cpws 1 --codes 8 --options 6395 \
  --priorities 13 --out results/b10/coordinator-code8-60s-1
```

Pass requires full duration, errors/pointer/output faults zero, exact independent
ring CRC and all 4,096 captured pins32 words, normal DDR phase, restored resources and
intact stack/code. The unchanged hard service gate is 128 us; for the B10 claim
**with margin**, additionally require `max_service_gap_us <= 96`, no gaps over
96, and core-0 maximum slices preferably <=8,192 cycles in repeated 60-s runs.
Record maximum service cycles, stage maxima, maximum-gap timestamp, IRQ counts,
worst frames and busy fractions. Do not call a 127-us pass robust.

Regression acceptance additionally requires code5/mask251 GCS mean at or below
the old main's roughly 23.1k cycles/frame and LDPC back near its historical main
cost, together with clean repeated 60-s priority-13 output. Compare the exact
main records and both workers, allowing stated run-to-run spread rather than
claiming a host-derived speedup. If SRAM improves GCS but not LDPC, retain that
partial outcome and investigate the remaining flash/memory interactions.

Priority 0 may stall a writer as it did historically; retain the timing and
fault rather than weaken acceptance. The 5,000-us source negative must report
source underrun and an exact deterministic drained ring. Inverted clock must
match data with consistently inverted GPIO28/IQ_FLAG phase. Mask0/cpw1 is an
expected slow-FEC failure; cpw2 is the slower positive control. Stack paint must
not be exhausted even when the code comparisons remain intact.

## Risks, dead ends and intended commits

Flash padding or moving only the BCH leaf would not remove dependence on XIP
layout; neither establishes a reusable fix. The pristine maps disprove the
initial moved-SRAM-table hypothesis. An unsliced final BB pass exists only in
the baseline path and cannot explain the measured fused mask251 regression.
MAP32 alone cannot explain the recorded gaps. The exact cache-stall attribution
and recovered silicon cycles remain open measurement gates.

Sharing the copied LDPC scheduler means both cores fetch it from SRAM0–3 while
DMA reads rows there. This can exchange XIP conflicts for SRAM contention; the
off/on control is essential. Smaller MAP slices and extra service iterations
cost CPU time. BCH96 and LDPC2 estimates exclude IRQs, USB and worst arbitration.
The copied C bundle's relocation safety depends on compiler output; the new
machine-code closure test and copy-size admission must remain required after
compiler changes. No larger gate, faster SYS clock or output-rate reduction was
used to claim cpw1 success. This remains memory GCS, not demonstrated external
ingress or RF performance.

Intended small logical commits (none made):

1. `firmware/{CMakeLists.txt,src/b10_hot.S,src/dvbs2.c,src/dvbs2.h}`,
   `host/test_b10_hot.py`, `Makefile`: **Add verified borrowed-SRAM GCS and LDPC hot paths.**
2. `firmware/src/{c3_fec.c,c3_fec.h,bench_b9.c,bench_b9.h,main.c}`:
   **Extend B9 rates and C9 arenas with bounded service and same-image placement control.**
3. `host/{b9_trial.py,test_b9_trial.py,test_b9_bench.py,test_c3_fec_trial.py,test_c3_pipeline.py}`,
   `host/native/c3_pipeline_test.c`: **Verify B9 rates, hot paths, slice bounds and service negatives.**
4. `docs/rd/{b10-8psk16-rates.md,README.md}`:
   **Record B10 regression evidence, memory budget and coordinator qualification matrix.**
