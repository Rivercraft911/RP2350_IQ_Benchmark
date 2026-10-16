# C9: smaller FEC tables and bounded scheduling work

**Device result (coordinator, 2026-10-16):** pair work at 5/6 318k -> 304k (6395) and 3/4
328k -> 312k (12795), bit-exact. The 16 Msym/s cadence still misses the first frame, by 27 µs
(5/6) and 109 µs (3/4). See [results](../../results/c9-fec-fit/2026-10-16/README.md).

C9 supplies a same-image candidate for 5/6 and 3/4 at the 16 Msym/s FEC cadence.
**Cadence fit is not established: no device was accessed.** The preferred starting
masks are **4603 for 5/6** and **12795 for 3/4**. They use the existing LDM BCH
leaf, cheaper supervision and larger BCH/LDPC slices; 12795 adds a new q=45 packer.
The compact BCH table fits the real memory reservation, but instruction counts
favor LDM, so it remains an A/B diagnostic.

The inherited [C8 measurements](../../results/c8-fec-combined/2026-10-16/README.md)
are the comparison: mask 251 misses the first 5/6 frame by 225 us (28,800 SYS
cycles), and 3/4 by 294 us (37,632 cycles). Code 5 (2/3) already passes 60 s.
Every claim below is a storage calculation, native correctness result or instruction
estimate unless explicitly attributed to those measurements. No serial port,
flashing tool, Pico or immutable result record was used or changed. No commits
were made.

## Selectors and implementation

Defaults and the existing `best` aliases remain unchanged: `c3fec=0`,
`c3pair=31`, named `best=251` / `1275` respectively. New bits are independent:

| Bit | Name | Behavior |
|---:|---|---|
| 2048 | `C9_BCH_TABLE4` | Four packed 8-bit BCH planes; implies private per-worker BCH copies, variant 3 |
| 4096 | `C9_SCHEDULE` | Cheaper cadence supervision, exact fixed-point deadlines, prelaunch waiting worker, BCH128/LDPC2 slices |
| 8192 | `C9_LDPC_PACK` | LDPC mode 3; implies C5, adds fused q=45 gather/fixed pack; overrides bits 1/32 |

`C9_ALL=16383` is the parser boundary, not a legal all-features request.
Bit 2048 excludes 256 (LDM) and 1024 (wide table); 1024 still requires pair
layout 4 and excludes 256. Pair bit 2048 rejects who 4: that SRAM hammer occupies
its table reservation. Admission precedes buffer writes and worker launch.

Useful masks: 251 is the C8 control; 507 adds LDM; 4603 adds bit 4096;
12539 is 251+4096+8192; **12795** adds LDM to that; 2299 is 251+2048;
6395 is 2299+4096; 14587 adds q45 packing to 6395. All work in both commands,
except the scheduler's launch/timer changes apply only to `c3fec`.
`c3pair` with 4096 still exercises the larger BCH/LDPC slices.

The compact table has four planes in high-byte-first order, each 256 rows of
4/5/6 live remainder words. Initialization uses the existing byte table before
any timed work. The original 32-KiB padded table remains intact for final byte
tails, defaults and restoration. Both workers capture their own copied leaf and
one read-only table pointer. No global selector changes during jobs.

The q45 packer fuses prefix application, the first two transpose stages and fixed
45-bit output packing. It shares C5's private transpose and keeps its 324-byte
maximum stack footprint. Other q values take C5's existing path. The original
C5 pack machine bytes and end symbol stay unchanged; mode 3 appends 1280 bytes
plus two alignment bytes. The mode-3 bundle is 9448 bytes in a 9728-byte arena;
mode 2 remains 8166 bytes. No group arithmetic was changed.

Bit 4096 launches core 1 while all jobs are IDLE, before the epoch. The epoch
still precedes both frame releases; no GCS, BCH, LDPC or mapping occurs outside
the frame budget. The normal loop uses wrap-safe CYCCNT subtraction to decide
when to read the microsecond timer. Publication and the 128-us service-gap fault
still use actual timer time; all absolute deadlines retain their old rounding.
At admitted SYS128/256, one frame is exactly 22194/11097 sixteenths of a
microsecond times cpw. An absolute product plus `+15 >> 4` replaces 64-bit division
without cumulative rounding drift. Publication is skipped when its next worker
is not READY. Per-slice work returns its cost, avoiding a second 64-bit busy read.

BCH takes 128 rather than 64 words per slice; LDPC takes two rather than one
units (at most eight groups). GCS stays at 64 words and C6 mapping at 32 words:
C8 cold slices reached about 8k/12k cycles there, so doubling those stages would
risk the gate. Existing code-8/code-6 core-0 BCH/LDPC maxima were about 5.2k cycles;
doubling is a provisional <10.4k-cycle slice estimate. With a 32-us service
interval (4096 cycles), this leaves roughly 1.9k cycles before the 16384-cycle
service-gap ceiling. Interrupts/contention can invalidate that estimate; the
unchanged fault gate and coordinator qualification decide.

## Candidate counts and cycle budget

At SYS128, 16 Msym/s and 4 SPS, there are 8 SYS cycles/symbol. Each assigned
alternate frame has `2 * 22194 * 8 = 355104` cycles = 2774.25 us, including
core-0 supervision. Host elapsed time is not an M33 measurement.

| BCH scheme | t10 bytes | t12 bytes | Whole-step work / reason |
|---|---:|---:|---|
| Existing 4x8 padded | 32768 | 32768 | Existing private scalar loops: 54/63 instructions; live loads 20/24 |
| Existing 4x8 LDM | 0 extra | 0 extra | 54/64 transfer-expanded instructions per 32 input bits |
| New packed 4x8 | 20480 | 24576 | 59/72 transfer-expanded instructions per 32 input bits |
| 3x8, 24-bit step | 15360 | 18432 | `7W-1..8W-1` load/XOR/shift operations for only 24 bits, before source repacking |
| 3x10, 30-bit step | 61440 | 73728 | Extra remainder shifts/source repacking; t12 exceeds 64 KiB |
| 2x11, 22-bit step | 81920 | 98304 | Exceeds 64 KiB; two 11-bit tables do not cover a 32-bit word |
| C7 11/11/10 | 102400 | 122880 | Measured faster, cannot coexist with the current OUT8/C3 reservations |

`W=degree/32`. A straightforward non-word-aligned remainder shift needs
`2W-1` shift/combine operations in addition to three table contributions.
That gives `3W loads + 3W XORs + (2W-1) = 8W-1` operations/step. Folding
shifts into EOR operands can lower the count to `7W-1`, before complex-shift
penalties. At t10 the favorable 24-bit-step count is still `34 * 32/24 = 45.3`
load/XOR operations per 32 input bits, versus 39 for four byte planes, before
addressing/source repacking. These are operation models, not assembled leaves.
The 30-bit version could reduce that arithmetic, but its 61440-B t10 table
exceeds the 55-KiB contiguous capture interval. Capture plus the 4592-byte
context spare is only 60912 B, still 528 B short before fragmentation. Its t12
table is larger still. These smaller-step designs were counted and rejected
rather than implemented. Packed 4x8 is implemented to test whether placement or
burst behavior can overcome its worse actual instruction count.

The actual assembled packed loops have 35/43/52 scalar instructions and
47/59/72 after charging every LDM register transfer. Copies are 164/196/222 B
(t8/t10/t12), well below 512 B. Its optimistic transfer-expanded plus branch
interiors cost 100920 cycles at 5/6 and 110376 at 3/4, before calls/tails/stalls.
This is not a supported saving over LDM.

For existing LDM, the same optimistic model gives `1682 * 55 = 92510` cycles
at 5/6 and `1512 * 65 = 98280` at 3/4, plus entry, tails, slices and exposed
dependencies. Compare C8 concurrent byte BCH of 117271/116587 cycles in layout 0.
This suggests roughly 15–25k/10–18k savings respectively; these ranges are
planning estimates, not bounds or new measurements. See the
[M33 instruction notes](../rp2350-notes.md): transfer-expanded instructions do
not establish SRAM contention or load-result penalties.

The q45 packer changes 21457 to 13882 decoded instructions/frame (7575 fewer).
The multiple-load/store expansion adds the same 1344 transfers on each side;
memory opcodes fall 4515 to 2944. Expected saving is **8–11k cycles**, excluding
new contention. q30 already uses C5's specialized pack and has no corresponding
algorithmic saving. Remaining groups cost about 55936/50481 decoded instructions
at q30/q45 (56296/50721 word-expanded): 600/540 edges, with 590/530 shifted.
Shifted bursts have a >=75-transfer-expanded floor versus the existing 72
instructions; prepacked edge metadata costs about 2.4 KiB for only 1–2k theoretical
cycles/frame. Neither was adopted.

C8's steady 6.6% supervision is about 23.4k cycles per assigned frame; its first
failed frames used 37–40k. C9 reduces slices per assigned frame from 136/141/138 to 96/99/97
(codes 5/8/6), and removes timer/accounting/division work. The compiled wrapper
contains roughly 80–100 decoded instructions per slice outside the FEC body.
Coarsening removes about 3.2–4.2k instructions; avoiding empty publication calls
and exact division adds further savings. The provisional steady saving is
**6–10k cycles/frame**, excluding bus/XIP changes. The observed 58–64-us startup interval
is an upper limit on the launch time recovered, not all a claimed speedup.
A conditional mean-frame budget uses the C8 layout-0 pair means plus 23.4k
supervision (the measured 2/3 steady load), subtracting the estimates above:

| Code / candidate | Conditional work + supervision | Slack to 355104 |
|---|---:|---:|
| 8 / 4603 | `319.1k - (15..25)k + 23.4k - (6..10)k = 307.5..321.5k` | 33.6..47.6k |
| 6 / 12795 | `326.1k - (10..18)k - (8..11)k + 23.4k - (6..10)k = 310.5..325.5k` | 29.6..44.6k |

These are arithmetic scenarios mixing inherited means with instruction estimates,
not predictions of maxima. C8 pair maxima exceeded means by about 28–33k cycles,
and the failing cadence startup used more supervision than steady 2/3. There is
little assured worst-frame margin. Adding the BCH, packing and supervision
estimates makes closing the observed 30–40k deficit plausible, with uncertain margin. **A 60-s device pass and measured
minimum publication slack are required before calling either code fitted.**

## SRAM and linker layout

No static arena, DMA channel, PIO state machine or PIO instruction is added.
All masks reserve the enlarged 9728-byte LDPC arenas, so same-image A/B has fixed
job/code placement. The experiment Release ELF/map plus ARM DWARF give these
half-open addresses (copied entries omit the Thumb bit):

| Object | Address / size |
|---|---|
| `ring_buf`, `in_buf`, `cap_buf` | `0x20000000..0x20020000`, `0x20020000..0x20028000`, `0x20051fac..0x2005ffac` |
| OUT8 shaping reservation | `0x20000000..0x20010000`, untouched by `c3fec` |
| C3 context / job 1 | `0x20010000..0x2001ee10`, 60944 B (4592 B spare) / `0x20014c08` |
| C3 Gold / mapper | `0x20019864` / `0x2001ae04` |
| C3 C5 group / rows / old pack / q45 entry | `0x2001c194` / `0x2001dad4` / `0x2001db54` / `0x2001e17c` |
| C3 core-0 BCH / transpose | `0x2001e794` / `0x2001e994` |
| C3 core-1 BCH / transpose | `0x20051fac` / `0x200521ac` |
| C3 compact table max | `0x200523ac..0x200583ac`, after the 1024-byte core-1 code reservation |
| Pair layout-0 lane / low context | 40716 / 81944 B; low ends `0x20014018` |
| Pair layout-0 A LDPC / BCH / B job | `0x2000750c` / `0x20009b0c` / `0x20009f0c` |
| Pair layouts 0–3 core-1 BCH / transpose | `0x2005bab8` / `0x2005bcb8` |
| Pair layout-1 rings / high end | `0x2005beb8` / `0x2005feb8`; 244 B spare |
| Pair layout-4 context / B job / end | 55028 B / `0x2005a918` / `0x2005f6a0`; 2316 B spare |
| Pair layout-4 LDPC / core-1 BCH | `0x20057f18` / `0x2005a518` |
| Pair compact table max, all layouts | `0x20018000..0x2001e000`, below external Gold/shared state |
| Pair layout-4 Gold / shared / core-0 BCH | `0x2001e000` / `0x2001f5a0` / `0x2001f620` |
| Original padded BCH table | `0x20071f50..0x20079f50`, 32768 B |
| Lower BSS / minimum heap end | `0x2003fbfc` / `0x2003fffc`, 4 B spare |
| High allocations / core-0 stack bottom | `0x2007efe8` / `0x2007f000`, 24 B spare |
| Scratch X end / core-1 stack bottom | `0x200807f0` / `0x20080800`, 16 B spare |
| Scratch Y end / bank end | `0x20081e68` / `0x20082000`, 408 B spare |

`c3_job_t` remains 19336 B; mode 3 consumes 9448 of 9728 copied bytes. Compact
table bytes are 16/20/24 KiB. In cadence the 24 KiB maximum leaves 31744 B of
capture storage unused after its private code. In pair layouts the compact table
is in SRAM0–3, whereas cadence places it in SRAM4–7: pair timing cannot alone
predict its cadence timing. Compile-time assertions and native guards check
these reservations. Static lower/high headroom is unchanged. Default iqbench
has 544/344 B free after heap/below stack; pvflight has 0/344 B in both builds.

The two C3 frame jobs, code rings and shaping table now coexist in address space;
this does not integrate ingress or B6's separate core-1 grant worker.

## Offline verification

Both required builds passed without warnings; full `make check` and
`git diff --check` pass. The build
caches use `PICO_NO_PICOTOOL=ON` to honor the tool prohibition: the SDK produces
ELF/BIN/HEX/map, with no UF2 conversion or device access. The coordinator must
package the matching source; no old ignored UF2 is this image.

```sh
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DPICO_NO_PICOTOOL=ON
make build
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON -DPICO_NO_PICOTOOL=ON
make build BUILD=firmware/build-exp
make check
```

Native checks execute the production commands/job graph and independent existing
Python BCH/LDPC/PL references. Physical rings, CRCs, boundary carries, guards,
selectors and PRBS restoration are checked, including odd-first completion and
ring wraps. Compact BCH tests check all 3072 row indices, arbitrary remainders,
uneven slices, all eleven normal rates and final byte tails. Flipping a used table
bit must change parity: a healthy-looking corruption negative fails the test.
Assembly-semantics checks execute the actual decoded instructions, including
AAPCS, stack/code extents and unsupported rates; BCH passes 37003 cases
(4034109 interpreted instructions). C9 q45 pack semantics cover 1920
cases; portable FEC covers 128 independent vectors across modes 0–3. The C3 graph covers 95 masks x three
rates x 16 continuous frames (4560 frames); the production pair test covers
585 valid requests and 13 admission negatives. An independent review additionally
checked 60 compact-table requests in layouts 2/3 and four CLI rejection masks.
Scheduler checks cover the counter wrap, slow worker, a 200-us service fault,
exact deadline arithmetic at both clocks/all cpw, and positive/negative slack.
They are
not execution of relocated Thumb on silicon.

Final local experiment artifact SHA-256 (the dirty Git identity alone is insufficient):

```text
iqbench.elf e791c5dda3ef8a132c444b71727384f01898bacd3f2ea317ef41e14397263839
iqbench.bin 7fa4cdcd57e5713a266f8041b60358bc6274a3f82d006284d48e2846344d9d25
pvflight.elf 4eea61aba6c32b9eef157220fc0cffc61ab48b8c3cd732285c5317aebef7c47e
```

## Coordinator commands and acceptance

These lines have **not been run here**. Use one experiment image, SYS128, fresh
result destinations, archived source diff, ELF/map and hashes. Repeat A/B/A at
least three times. Start with codes 5/8/6, keeping layout and who fixed:

```text
info
stackcheck
c3pair 5 100 0 2 251
c3pair 5 100 0 2 507
c3pair 5 100 0 2 4603
c3pair 5 100 0 2 12795
c3pair 5 100 0 2 2299
c3pair 5 100 0 2 6395
c3pair 5 100 0 2 251
c3pair 8 100 0 2 251
c3pair 8 100 0 2 507
c3pair 8 100 0 2 4603
c3pair 8 100 0 2 12795
c3pair 8 100 0 2 6395
c3pair 8 100 0 2 251
c3pair 6 100 0 2 251
c3pair 6 100 0 2 507
c3pair 6 100 0 2 4603
c3pair 6 100 0 2 12539
c3pair 6 100 0 2 12795
c3pair 6 100 0 2 14587
c3pair 6 100 0 2 251
```

Repeat 507/4603/12795 with who 0 and 1 to distinguish contention. Require all
100 runs/job and independent pair CRCs: code 5 `1273684834/2540800392`, code 8
`1863126860/2777209518`, code 6 `4157714373/1725042067`. Record both workers'
stages, LDPC substeps and maxima; mode 3 must chiefly reduce q45 substep 5.
BCH/LDPC substep attribution with two-unit slices can span a transition; total
LDPC stage and frame work remain the comparable measurements.

Then test cadence in the same image, option off/on/off, gap 128 unchanged:

```text
c3fec 8 3000 0 1 128 251
c3fec 8 3000 0 1 128 507
c3fec 8 3000 0 1 128 4603
c3fec 8 3000 0 1 128 6395
c3fec 8 3000 0 1 128 251
c3fec 6 3000 0 1 128 251
c3fec 6 3000 0 1 128 4603
c3fec 6 3000 0 1 128 12539
c3fec 6 3000 0 1 128 12795
c3fec 6 3000 0 1 128 14587
c3fec 6 3000 0 1 128 251
```

For each passing mask, use its exact numeric value in a 60-s run; the preferred
starting lines and their slow-worker negatives are:

```text
c3fec 8 60000 0 1 128 4603
c3fec 6 60000 0 1 128 12795
c3fec 8 3000 5000 1 128 4603
c3fec 6 3000 5000 1 128 12795
stackcheck
```

Run the 60-s lines only for masks that passed the short run. The host runner
checks actual published output independently; for example:

```sh
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 8 --options 251 507 4603 6395 \
  --cpw 1 --gap-us 128 --ms 3000 --out results/c9-fec-fit/coordinator-code8-ab
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 6 --options 251 4603 12539 12795 14587 \
  --cpw 1 --gap-us 128 --ms 3000 --out results/c9-fec-fit/coordinator-code6-ab
```

Pass requires `errors=0`, no late frames/lateness, target=published=consumed,
independent rings/carry, SYS128/cpw1, every slice <=16384 cycles, service gap
<=128 us and intact stack/code. For the claim **with margin**, additionally
require minimum publication slack >=32 us (one service interval) during the
60-s run; record the actual minimum and busy fractions. Zero-late with less slack
is a cadence pass without that margin. First-frame failure remains failure.
The slow negative must report deadline error/lateness, stop releases and drain
exact published output. Native used-table corruption must remain detected.
Invalid examples `c3fec 8 100 0 1 128 2304`, `c3pair 6 1 0 2 3072` and
`c3pair 5 1 0 4 2299` must reject before mutation or launch.

## Risks, rejected schedules and intended commits

The compact-table candidate is an honest instruction-count dead end unless device
placement changes the result. Relaxing the service gate, moving the epoch past
frame preparation or promoting a host timing to an M33 measurement was not used.
Larger slices reduce service runway; new contention or interrupts can still make
4096 fail. Same-image A/B is necessary because enlarged borrowed code arenas move
private BCH addresses even with all C9 bits off. Static SRAM remains near full.

Moving frame-0 GCS/BB to core 1 adds a handoff on its BCH critical path, while
charging the other already-busy worker roughly 26–31k more cycles. Uneven 2:1
whole-frame ownership gives the busier core only about 266k cycles/frame on
average, below its existing 319–327k work. A fine-grained cross-frame split needs
a different pipeline; neither schedule was claimed as a free capacity gain.
B6 grant service, live ingress, DMA/FIFO runway and RF remain separate integration
work. A CPU-only pass is not a complete 16-Msym/s transmitter result.

Intended small logical commits (none made):

1. `firmware/src/{dvbs2.c,dvbs2.h,dvbs2_bch_fast.S,dvbs2_ldpc_c5.S}`:
   **Add compact BCH diagnostic and opt-in q45 fused packing.**
2. `firmware/src/{c3_fec.c,c3_fec.h,bench_c3_fec.c,bench_c3_pair.c,main.c}`,
   `host/c3_fec_trial.py`: **Expose C9 candidates with bounded scheduling and fixed SRAM reservations.**
3. `Makefile`, `host/native/{c3_pipeline_test.c,dvbs2_parallel_test.c}`,
   `host/test_{c9_bch,c7_bch,bch_fast_asm,c5_ldpc_pack,c3_pipeline,c3_bench,c3_fec_trial,c7_pair,dvbs2_parallel}.py`:
   **Verify C9 command graphs, table corruption, assembly semantics and references.**
4. `docs/rd/{c9-fec-fit.md,README.md}`:
   **Record C9 memory/count budgets and same-image coordinator qualification.**
