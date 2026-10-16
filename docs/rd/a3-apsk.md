# A3: table-driven APSK PL mapping and a cheaper 32APSK key conversion

**Device result (coordinator, 2026-10-16):** 60 s clean at 16APSK cpw 7 option 1 (4.571 Msym/s,
14.73 Mb/s) and 32APSK cpw 9 option 3 (3.556 Msym/s, 14.33 Mb/s). Kernels measured inside the
estimates (5.71, 8.77, 7.16 cycles/symbol); the predicted cpw 6 / cpw 8 underran because the
worst frame, not the mean, sets the limit. See [results](../../results/a3-apsk/2026-10-16/README.md).

2026-10-16, branch `worktree-agent-ab62c7d523917fc33`, based on `main` at `0296275`.
Implemented and natively verified. **No device measurement was made**: nothing here opened
the Pico, a serial port or picotool. Every cycle figure for the new code is an estimate
from compiled instruction counts. The coordinator's device runs decide the result.

**What it should buy, if the estimates hold** (SYS128, normal 5/6 with pilots, 2 SPS):

| Mode | Measured best today | Predicted with A3 | Steps | BB data capacity |
|---|---|---|---:|---:|
| 16APSK | CPW 8, 4.000 Msym/s (split) | **CPW 6, 5.333 Msym/s** (CPW 7, 4.571, with margin) | 2 | 12.89 → 17.18 Mb/s |
| 32APSK | CPW 12, 2.667 Msym/s (split) | **CPW 8, 4.000 Msym/s** (CPW 9, 3.556, with margin) | 4 | 10.75 → 16.12 Mb/s |

Both new points are predicted at 84–90% work per core. Neither leaves 10% margin, so the
fallback in brackets (one step slower) is also listed in the device plan. Capacity is
`Rs × 53760 / frame_symbols`, as in [apsk-fec](../../results/apsk-fec/2026-10-15/README.md).

## The baseline is already one step past the README

The front-page table and the task brief quote 3.556 / 2.286 Msym/s at SYS128 (CPW 9 / 14,
`results/apsk-fec/2026-10-15`, core 1 at 96 / 94%). The opt-in split encoder
(`benchapskfec ... split 1`, BB scrambling and BCH on core 0) already passed **60 s clean
at CPW 8 (16APSK, 4.000 Msym/s) and CPW 12 (32APSK, 2.667 Msym/s)** at both clocks
([optimizations/2026-10-15](../../results/optimizations/2026-10-15/README.md),
`apsk16-split-soak/`, `apsk32-split-soak/`, firmware `5e38990269`). Those are the
baselines here, and the README table is stale for APSK.

Recorded cost per transmitted symbol in those soaks (SYS128, `ldpcpack 1`, `bchbank 1`):

| Cycles/symbol | 16APSK split, CPW 8 | 32APSK split, CPW 12 |
|---|---:|---:|
| C1: BB transport + header | 1.53 | 1.90 |
| C1: LDPC | 6.84 | 8.46 |
| C1: **PL mapping** | **17.25** | **31.77** |
| C0: BB scrambling + BCH (split) | 7.26 | 9.02 |
| C0: native→offset conversion | 0 | **14.04** |
| C0: shaping | 20.81 | 19.90 |
| Work, core 0 / core 1 | 28.19 (88.1%) / 25.63 (80.1%) | 43.08 (89.8%) / 42.12 (87.8%) |

The non-split records give BB scrambling + BCH on core 1 as 7.05 (16APSK,
`optimizations/.../apsk16-nosplit-quick`, CPW 9) and 8.74 (32APSK,
`apsk-fec/.../qualified-32-128-1`). Both cores are now near 90%, so the next step needs
less total work, not a different split. The PL mapper is the largest single item, as
the [frontier model](../frontier/frontier.md) says (lever (ii)3). For 32APSK the
core-0 conversion is the next largest.

Both existing mapper loops run from cached flash with flash-resident lookup tables. Their
compiled hot loops are 32 instructions per 4-symbol word (16APSK `put16`) and 42 per
2-symbol word (32APSK `map32`): about 12 and 21 instructions per symbol including run
setup. The measured 17.3 and 31.8 cycles imply about 1.4–1.5 cycles per instruction.

## What was built

All three paths are selected by a new fifth `benchapskfec` argument, `options`, a bit
mask. **0 (the default) runs exactly the previous code**: the original functions are
untouched and the new ones are only reached through the selector.

| Bit | Path | Core | Where it runs |
|---:|---|---|---|
| 1 | Table-driven PL mapper (`dvbs2_apsk_frame_fast`) | 1 | Leaves copied into SRAM at init |
| 2 | Multiply-form 32APSK native→offset conversion (`iq32offsets_from_native_mla`), 32APSK only | 0 | SCRATCH_Y, beside the 32APSK shaper leaf |
| 4 | Diagnostic: bit 1's leaves run in place from cached flash (needs 1) | 1 | Flash |

### Table-driven PL mapper (bit 1)

Normal frames have 16,200 (16APSK) or 12,960 (32APSK) data symbols per interleaver
column. Every 32-symbol data run therefore starts on a frame word. The 16APSK columns sit at word offsets
0/506/1012/1518 with fixed bit shifts 0/8/16/24. The 32APSK columns sit at multiples of 405 words
with no shift. Gold is `gold[run]`. Three leaves exploit that
([dvbs2_apsk.c](../../firmware/src/dvbs2_apsk.c)):

- **16APSK** (`fast16_run0`, `fast16_run2`): four column reads and the existing
  bit-parallel `map16` label/Gold algebra give four planes. Zipping magnitude and
  sign planes nibble-wise forms byte indices into two 256-entry tables (magnitude
  and sign codes of four symbols each). One output word is two loads and an OR.
  Normal frames are 2 mod 4 symbols long, so data runs alternate between fill 0 and
  fill 2. The fill-2 leaf completes the carried pair with symbols 0–1, maps
  symbols 2–29 from planes shifted by two, and carries symbols 30–31.
- **32APSK** (`fast32_run`): five label planes and two Gold planes go through an 8×8 bit
  transpose per byte lane (three rounds of masked swaps). The transpose yields each symbol's 7-bit
  `points32` index (label plus quarter turn), so there is one table load per symbol.
  The 45 header words and 18 words per pilot block are fixed for a configuration,
  so init generates them once with the original code.
- **Driver** (flash C): whole runs are written straight into the native ring between
  64-word credit points. A run that would cross a credit point or the ring end is
  mapped into a 16-word local block and stored in contiguous pieces. Service calls,
  their arguments, ring contents at each call, and the state on a refused service
  are therefore identical to the original. Headers and pilots for 16APSK, the
  final 8-symbol tail of 16APSK frames, short frames, odd fills and a non-empty
  32APSK accumulator at fill 0 all go to the original code.

The lookup tables (512 words for 16APSK; 128 + 189 words for 32APSK) and a copy of the
1,528-byte leaf section live in a 1,024-word arena. The arena is borrowed after the
existing second FE slot in `cfg.ti`, at the same place for split on or off.

Compiled with the firmware's GCC 14.2 Release flags, the hot loops are **102 / 119
instructions per 32 symbols** (16APSK fill 0 / fill 2) and **160 per 32 symbols**
(32APSK). Against today's loops that is about 3.5 instead of 12, and 5.0 instead of 21,
instructions per symbol (`host/test_apsk_fast_pic.py` prints the counts).

### Multiply-form 32APSK conversion (bit 2)

Core 0 turns each native word (two symbols, five-bit I and Q codes in packed lanes)
into rolling radix-17 byte offsets. The original evaluates
`(a + 17(b + 17c)) << 3` twice per word as shift-add Horner chains: 22 instructions per
2 symbols, measured 14.04 cycles/symbol from flash. With `u = s[-1] + 17·s0`, the keys
become `8·s[-2] + 136·u` and `8·u + 2312·s1`. That is the same integer polynomial, so the
output is identical modulo 2³² for every input, including invalid codes that overflow
a lane. Unrolled by two, the loop is **24 instructions per 4 symbols, 6 of them
single-cycle MUL/MLA** ([iq32offsets_mla.c](../../firmware/src/iq32offsets_mla.c)).

Without an empty asm barrier on the constants, GCC turns the products back into
shift-add chains: 19 instructions per 2 symbols, against 12 with MUL/MLA. The function is linked last
(`firmware/CMakeLists.txt`) so that its SCRATCH_Y section follows every existing
scratch leaf.

### Benchmark, records and runner

- `benchapskfec mod cpw ms [split] [options]`: invalid combinations (2 for 16APSK, 4
  without 1, >7) are rejected before anything is touched. The record adds `options`,
  `pl_mapper` (`c_flash` / `fast_sram_arena` / `fast_flash`) and
  `pl_mapper_code_bytes`; 32APSK's `prepare_kernel` names the conversion used.
- `host/apsk_fec_trial.py --options N` sends the token only when it is nonzero, so
  default commands are byte-identical to earlier campaigns. It requires the record to
  name exactly the requested paths, and it validates option combinations. Its
  self-test now covers 240 metadata profiles, including option, mapper, conversion
  and code-size mutations, and deadline controls with options.

## Why this change and not the alternatives

- **PL jobs shared between cores** (`apsk-fec/.../next-experiments.md` #1). They address the
  same 17.3 / 31.8 cycles, but by moving them between cores rather than removing them,
  at the cost of ownership machinery for FE slots and mixed-frame words. With
  both cores at about 90% today, moving work cannot buy a step; removing it can.
  After A3 the mapper is about 5–9 cycles/symbol, and sharing it buys too little to justify the machinery.
- **Split mode with the fast mapper** is counterproductive. It moves 7–9 cycles of
  BCH onto core 0, which becomes the bottleneck. 16APSK split + fast at CPW 7
  predicts core 0 at 100%. **Run A3 with `split 0`.**
- **A faster 16APSK shaper** would help 16APSK only after the mapper is fixed, and
  is the limit for the step after (below).

## Native verification (no device)

`make check` passes. New or extended checks:

| Check | What it proves |
|---|---|
| `test_dvbs2_apsk.py` | Every existing case also through the fast mapper against the **independent polar-geometry, scalar interleaver, Gold and PL header reference**. That is 226 fast cases: all normal and short rates, pilots on and off, every partial fill, Gold codes 0/1/99999/262142, rings of 2 and 128 words, refused services, and real GCS → scalar BCH/LDPC frames whose first-frame CRCs equal the firmware's prefill constants. Plus an **exact A/B over 1,346 cases (82,933 service calls)**: identical return values, service arguments, ring contents at every call, final ring and stop state, for rings of 2/64/128/8192 words, all fills, refusals anywhere, and stale accumulators. An engagement control (corrupting the fast tables changes 2,258 / 6,480 words of a normal 16 / 32APSK frame and none of a short frame). Five rejected inits, and a mismatched fast state that falls back exactly. |
| `test_apsk_fec_native.py` | Real GCS BBFRAMEs → scalar BCH/LDPC → **fast mapper** → native FIR, and → **multiply-form conversion** → offset FIR, equal the host's finite scalar reference bus for three frames and all block partitions. |
| `test_apsk_split_native.py` | The bench's own `encode_frame` / `split_encode_step` leaves with options 1 produce the same 9-frame stream as the original, serial and split, both LDPC packers, four schedules. |
| `test_iq32offsets_native.py` | The multiply form equals the original in 1,281 cases, including the exhaustive radix-17 triples, history partitions and fully random 32-bit input words. |
| `test_apsk_fast_pic.py` (new) | The copied section compiled with the real flags has no relocations, calls, PC-relative data or outside branches and fits 2,048 bytes (1,528 used). The SCRATCH_Y function makes no calls. Prints the loop instruction counts used below. |
| `apsk_fec_trial.py --self-test` | Strict record checks for every option, with negative mutations. |

These run the C on the host (and, for the PIC test, inspect Arm output). They do not
execute Arm code. On the device, two checks cover the compiled binary. First, the prefill
frame 0 goes through the selected mapper and must hit the existing CRC before output
starts. Second, every capture covers the end of one frame and the start of the next, so both 16APSK fills are compared
against the independent reference.

## Cycle budget (estimates)

Model: ALU and MUL 1 cycle, LDR 2, LDRD 3, STR 1, taken branch 2. The upper bound
adds 20% for unmodelled SRAM and bus contention. Run, call and straddle overheads are
estimated from the driver. Applied to the recorded split costs above, the same
bookkeeping reproduces the measured loads within one point.

| Item (cycles/symbol) | Estimate |
|---|---:|
| 16APSK leaf (mean of fill 0 / fill 2: 4.0 / 4.6) + driver, header/pilot, tail | **5.2 – 6.2** |
| 32APSK leaf (6.3) + driver, straddle copies, header/pilot words | **7.6 – 9.1** |
| 32APSK multiply-form conversion | **6.8 – 8.1** (was 14.04) |

Per-core work with `split 0`, from recorded FEC/shaping costs plus the estimates above. A
range is lower–upper estimate. Budget is 4 × CPW cycles per symbol per core.

16APSK, option 1 (C0 = the unchanged flash-resident shaper, 20.8 + 0.1):

| SYS128 CPW / Msym/s | Budget | Core 0 | Core 1 (15.42 FEC + mapper) |
|---|---:|---:|---:|
| 9 / 3.556 | 36 | 58% | 57–60% |
| 8 / 4.000 | 32 | 65% | 64–68% |
| 7 / 4.571 | 28 | 75% | 74–77% |
| **6 / 5.333** | 24 | **87%** | **86–90%** |
| 5 / 6.400 | 20 | 105% | 103–108% |

32APSK (core 1 = 19.10 FEC + mapper):

| SYS128 CPW / Msym/s | Budget | Opt 1: core 0 / core 1 | Opt 3: core 0 / core 1 |
|---|---:|---:|---:|
| 11 / 2.909 | 44 | 77 / 61–64% | 61–64 / 61–64% |
| 10 / 3.200 | 40 | 85 / 67–71% | 67–70 / 67–71% |
| 9 / 3.556 | 36 | 95 / 74–78% | 74–78 / 74–78% |
| **8 / 4.000** | 32 | 106 / 83–88% | **84–88 / 83–88%** |
| 7 / 4.571 | 28 | 122 / 95–101% | 96–100 / 95–101% |

Assumptions: single-cycle MUL/MLA on this M33 (to be confirmed against the Cortex-M33
TRM; the device `prepare_cyc_per_sym` measures it directly); recorded FEC costs carry
over unchanged (non-split BCH on core 1, `ldpcpack 1`); the 16APSK shaper is not slowed
by core 1 now fetching code from SRAM4-7, where its `old` table also lives.

Ring lead also has to survive each FEC burst, now that core 1 runs FEC whole. At
32APSK CPW 8 one frame's BB+BCH+LDPC (~255k cycles) consumes about 4.0k of the 8,192-word ring, so
the lead after mapping (about 7.6k words) should stay above 3k. `min_input_words` in each
record shows the real minimum.

## SRAM, flash and stack

| Resource | Change |
|---|---|
| `cfg.ti` (borrowed, idle-only) | +1,024 words after the second FE slot. 16APSK uses 2,048 old table + 1,040 Gold + 2,027 FE + 1,024 = 6,139 of 8,192 words; 32APSK uses 830 + 2,027 + 1,024 = 3,881 |
| Static RAM / RAM_HI | **0 bytes**. Low RAM above heap is still 552 B (`iqbench`) / 8 B (`pvflight`) |
| SCRATCH_Y | +384 B: 1,888 → **1,504 B free** (default image), 408 → **24 B free** (experiment image). No existing scratch leaf moves (checked against a pristine `HEAD` build). In the experiment image two scratch veneers now follow the new section, so B6's `state` data moves 16 B lower |
| Flash | +1,528 B leaves, +1.4 kB driver, +384 B scratch load image |
| Context | `live_t` grows by the fast state; it stays in the existing capture-buffer tail (static assert) |
| Core 1 stack | +about 150 B (16-word local block, leaf frame). Check with `stackcheck` after the runs |

**Pre-existing configure-order bug, found here, not fixed.** `firmware/CMakeLists.txt`
tests `if(CORE0_STACK_SRAM_HI)` (line 72) before `option(CORE0_STACK_SRAM_HI ... ON)`
(line 115). A fresh `make build` gives core 0 the SDK's 2 KiB stack (`__StackBottom`
0x2007f800). Any reconfigure gives the intended 4 KiB (0x2007f000). Any edit to
`CMakeLists.txt`, including this branch's, triggers that reconfigure in an existing build directory. Two images
of the same source can therefore differ in stack size. Fix: move the `option()` above
the `foreach`. Until then, build A3 in a fresh directory and record `__StackBottom`
from the map.

## Device plan for the coordinator

Build and identify (fresh directory, default image):

```sh
make build BUILD=firmware/build-a3
grep __StackBottom firmware/build-a3/iqbench.elf.map
# flash firmware/build-a3/iqbench.uf2, then:
python3 host/iqbench.py info        # its "git" value is LABEL below
```

Common arguments: `--clocks 128000 --ldpc-pack 1 --bch-bank 1 --expected-git LABEL
--build firmware/build-a3`. `--bch-bank` only matters for split runs; it is kept so every run
has the same selectors. Output under `results/a3-apsk/2026-10-16/`.

1. **Same-image baseline** (must reproduce the split soak within about 1 point):
   ```sh
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 16 --cpw 8 --split ARGS --out results/a3-apsk/2026-10-16/base16-split-1
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 32 --cpw 12 --split ARGS --out results/a3-apsk/2026-10-16/base32-split-1
   ```
2. **Frontiers, 3 s** (non-split). Predicted: 16APSK clean through CPW 6 and failing at 5; 32APSK
   opt 1 clean through 10 and marginal at 9; opt 3 clean through 8. A frontier summary is expected to
   report its infeasible CPW as failed.
   ```sh
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 16 --cpw 9 8 7 6 5 --options 1 ARGS --out results/a3-apsk/2026-10-16/fast16-frontier-1
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 32 --cpw 12 11 10 9 --options 1 ARGS --out results/a3-apsk/2026-10-16/fast32-map-frontier-1
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 32 --cpw 10 9 8 7 --options 3 ARGS --out results/a3-apsk/2026-10-16/fast32-frontier-1
   ```
3. **Qualification, 60 s**, at the fastest 3-s-clean CPW whose recorded `producer_work_busy`
   and `encoder_busy` are both at most 0.92 (expected CPW 6 / CPW 8; otherwise 7 / 9):
   ```sh
   python3 host/apsk_fec_trial.py --ms 60000 --modulations 16 --cpw 6 --options 1 ARGS --out results/a3-apsk/2026-10-16/qualified-16-1
   python3 host/apsk_fec_trial.py --ms 60000 --modulations 32 --cpw 8 --options 3 ARGS --out results/a3-apsk/2026-10-16/qualified-32-1
   ```
4. **Negative controls, 3 s, `--expect-deadline-fault`**. PASS here means the run failed
   early through an output underrun or TX stall, with no transport, ownership or SPI fault:
   ```sh
   # A/B: same image and CPW with the original mapper (core 1 needs ~33 / ~53 cycles of 24 / 32)
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 16 --cpw 6 --options 0 --expect-deadline-fault ARGS --out results/a3-apsk/2026-10-16/neg16-mapper-off-1
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 32 --cpw 8 --options 0 --expect-deadline-fault ARGS --out results/a3-apsk/2026-10-16/neg32-mapper-off-1
   # 32APSK without the conversion change: core 0 needs ~34 of 32
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 32 --cpw 8 --options 1 --expect-deadline-fault ARGS --out results/a3-apsk/2026-10-16/neg32-prep-off-1
   # One step beyond: 16APSK shaping alone (20.8) exceeds 20; 32APSK core 0 (~27) exceeds 24
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 16 --cpw 5 --options 1 --expect-deadline-fault ARGS --out results/a3-apsk/2026-10-16/neg16-beyond-1
   python3 host/apsk_fec_trial.py --ms 3000 --modulations 32 --cpw 6 --options 3 --expect-deadline-fault ARGS --out results/a3-apsk/2026-10-16/neg32-beyond-1
   ```
5. **Optional placement diagnostic**: rerun step 3's CPW for 3 s with `--options 5` (16APSK)
   and `--options 7` (32APSK). This shows whether running the leaves from SRAM rather than
   cached flash matters.
6. `python3 host/iqbench.py raw stackcheck`. Pass: `paint_exhausted` false for both cores,
   and scratch code still matching flash.

**Pass criteria** for every positive run are the runner's existing checks, which must
all hold:

- no underrun, TX stall, ownership error, idle substitution or SPI integrity fault;
- `fe_crc` equal to independent scalar BCH/LDPC;
- `prefill_crc` (frame 0 through the selected mapper) equal to the independent reference;
- all 8,192 captured halfwords equal to the independent GCS → BCH/LDPC → APSK/PL → FIR
  bus at one bounded offset, with the full 90-symbol PLHEADER;
- the record names exactly the requested `options`, `pl_mapper` and `prepare_kernel`.

Then report the measured `encoder_stage_cycles.map / mapped_symbols` and
`prepare_cyc_per_sym` against the estimates above. A rate that passes above 0.92 work
on either core should be stated as passing, not as qualified with margin.

## Theoretical next step for each modulation

- **16APSK, CPW 5 (6.4 Msym/s, 20.6 Mb/s at SYS128): not reachable by mapper work.**
  Core 0's shaper alone costs 20.8 cycles/symbol against a 20-cycle budget. The two cores'
  total after A3 is about 41.5 cycles/symbol against 40 × 0.9 = 36 usable.
  Reaching it needs both a cheaper 16APSK shaper, or shaping shared by both cores as in
  [apsk-dual](../../results/apsk-dual/2026-10-15/README.md), and about 6 cycles/symbol
  less FEC (BCH 7.05 + LDPC 6.84 today).
- **32APSK, CPW 7 (4.571 Msym/s, 18.4 Mb/s): marginal on paper, not on the plan.** The
  total after A3 is about 54–56 cycles/symbol against 2 × 28 = 56. Getting both cores
  under 90% needs 3–6 more cycles/symbol of savings and a near-perfect balance. Candidates are a 5/6 q=30 LDPC packer
  (`next-experiments.md` #3) and folding the offset conversion into core 1's mapper.
  That folding doubles native ring words per symbol and shrinks the FEC-burst lead.
- At SYS150 the same CPWs give 6.25 Msym/s (16APSK, 20.1 Mb/s) and 4.6875 Msym/s
  (32APSK, 18.9 Mb/s), all outside Yapogee's SYS128 plan. All APSK capacities remain below QPSK
  at 16 Msym/s (25.8 Mb/s); APSK still buys bandwidth, not capacity.

## Risks and dead ends

Risks, in order of how likely they are to move the result:

1. **The estimates.** CPI on this M33 is calibrated only indirectly. Core 1 now fetches
   code from SRAM4-7 while core 0 reads the 16APSK `old` table there; the placement
   diagnostic (step 5) separates code placement from arithmetic. If core 1 lands above
   0.92 at CPW 6 / 8, the one-step-slower points are predicted at about 75–78%.
2. **Core 0 at 87% for 16APSK CPW 6** with the flash-resident shaper. It is unchanged
   code, but no prior run held it there for 60 s.
3. **FEC-burst lead** with core 1 doing whole-frame FEC at the higher rates; see `min_input_words`.
4. Single-cycle MUL/MLA for the conversion (Cortex-M33 TRM not checked here).
5. The configure-order stack bug above, and **24 bytes of SCRATCH_Y left in the experiment
   image**. The next scratch addition there will not link.

Dead ends and corrections made along the way:

- The first conversion compiled back to shift-add chains (19 instructions per 2 symbols,
  against the original's 22); the constants had to be made opaque to keep MUL/MLA (12).
- The first conversion placement sat before the BCH scratch-Y leaves and the 32APSK
  shaper leaf, moving them by 384 B. It now lives in its own file, linked last.
- ORing the stream accumulator inside the fill-0 16APSK leaf cost 3 instructions per run
  (a stack spill). It moved to the driver.
- Per-word stores for runs crossing a credit point were estimated at about 0.9
  instructions/symbol for 32APSK (up to one run in four straddles). Contiguous pieces
  should roughly halve that; this is an estimate, not a measurement.
- The exact A/B found one real divergence: the original header ORs a stale 32APSK
  accumulator into its first word at fill 0, the precomputed words did not. Such frames
  now take the original code.
- Rejected without building: split mode with the fast mapper (above), a 2-symbol 32APSK
  table (64 KiB, no SRAM), and hardware APSK shaping (the frontier shows the tables do not fit).

## Files

- `firmware/src/dvbs2_apsk.{c,h}`: fast mapper API, leaves, driver.
- `firmware/src/iq32offsets_mla.c`, `iq32offsets_native.h`: multiply-form conversion.
- `firmware/CMakeLists.txt`: links the conversion last.
- `firmware/src/bench_apsk_fec.{c,h}`, `firmware/src/main.c`: `options` selector and record fields.
- `host/apsk_fec_trial.py`: `--options`, record checks, self-test.
- `host/test_dvbs2_apsk.py`, `test_apsk_fec_native.py`, `test_apsk_split_native.py`,
  `test_iq32offsets_native.py`, new `test_apsk_fast_pic.py`, `Makefile`.
