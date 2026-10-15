# C1: opt-in M33 LDPC throughput experiment

The implemented candidate is `ldpcxor 1`: scheduled LDPC row updates with
precomputed byte offsets, using the existing metadata arrays. Native correctness
passes for every supported code rate and pilot setting. **There are no new device
measurements.** The estimated normal 5/6 saving is **5–8k cycles/frame**, nominally
6.24k, before SRAM contention. A separate planar QPSK conversion is a dead end;
a smaller BCH scheduling idea is deferred for lack of executable SRAM space.

No Pico access, serial-port opening, flashing, picotool invocation, or modification
of existing results evidence was performed. Base: `8014ed3`, branch
`rd/c1-fec-throughput`, from `integration/yapogee-2026-10-15`.

## Implementation and correctness argument

- `ldpcxor 0` selects the existing row-update kernel and metadata format; it remains
  the power-on default. `ldpcxor 1` selects the new batch assembly leaf. Selection
  survives `dvbs2_init`; changing it while idle rebuilds metadata for the current
  code. Invalid selectors leave the selection unchanged. The shared LDPC scratch
  and tables still permit only one LDPC operation at a time.
- Existing `a_row` and `a_off` halfword arrays are reused. For selector 1, they hold
  `row * 48` bytes and `(source_byte_offset << 8) | shift`. Bounds are 6,432 and
  11,295 respectively, safely inside 16 bits. Division/modulo remains initialization
  work, outside stage timing. No additional metadata table or frame buffer exists.
- The new `dvbs2_ldpc_xor_entries` leaf is called once per 360-bit information
  group, rather than once per table entry. Each of its twelve word updates still
  uses seven instructions. Loading the destination row earlier hides the original
  `LDR row; EOR` dependency. Two XORs replace shift/OR/XOR: the two shifted source
  fields occupy disjoint bits, so OR and XOR give the same rotated word. For zero
  shift, Arm's `LSR` with register shift value 32 produces zero; the portable
  C equivalent explicitly avoids a C shift by 32.
- Parity accumulation, packing, canonical FEC layout, QPSK mapping, BCH and existing
  defaults are preserved. The selector adds a group dispatch to the common path;
  default cycle neutrality is **not** claimed. Compare both selectors in this
  same binary, and retain an integration-binary control when checking regressions.
- `dvbs2` and `dualstream` report `ldpc_xor`. `dualstream` now also reports
  `ldpc_pack` and `bch_bank`. The existing runner accepts `--ldpc-xor`,
  `--ldpc-pack`, and `--bch-bank`, records them and checks reported selections.
  All three CLI defaults are zero; cleanup resets them to zero.

Sources: [encoder](firmware/src/dvbs2.c), [assembly leaf](firmware/src/dvbs2_ldpc_asm.S),
[API](firmware/src/dvbs2.h), [runner](host/dual_trial.py).

## Native verification

Completed successfully:

```sh
python3 host/test_dvbs2_native.py
python3 host/test_dvbs2_ldpc_asm.py
make build
make check
git diff --check
```

`make build` built both `iqbench` and `pvflight` without warnings, using Pico SDK
2.2.0 and Arm GNU 14.2.Rel1 / GCC 14.2.1. Native C used Apple clang 17.0.0 with
`-O2 -std=c11 -Wall -Wextra -Werror`.

The expanded native harness checks both XOR selectors, both parity packers and
all three BCH selectors for all **21 rates × 2 pilot settings**. Full PLFRAME words
match the existing Python reference for every setting. Additional C equivalence
checks exercise three successive varied frames through the staged APIs without
flushing between frames. Separate/in-place parity is checked against bit-serial
LDPC, including unaligned Kldpc boundaries; parity packers also agree on guard words.
selector changes without reinitialization and persistence through initialization
are exercised. All reported waveform-word mismatches are zero.

The additional [assembly semantics test](host/test_dvbs2_ldpc_asm.py) parses the
actual leaf and its macro in instruction order. It passes **1,313 cases /
2,189,609 modeled instructions**: all 32 shifts, all 12 source-word offsets,
135 rows, repeated row updates, random/dense/constant patterns, zero entries with
null data arguments, destination guards, read-only inputs, the exact 36-byte
callee stack frame and AAPCS preservation. It is a source ISA model, not Arm
machine-code execution or an M33 timing measurement. The firmware build separately
verifies assembly encoding/linkage. Device CRC/capture checks remain necessary.

## Cycle estimates, with checked timing assumptions

The local RP2350 datasheet text was checked at §3.7.4.9.1–2, printed pp.138–139;
§3.7.4.9.5–6, p.140; and §3.7.4.9.7, pp.140–142. It describes uncontended
single-core timing with no active DMA/cache misses. LDR has a one-cycle result-use
penalty. Standalone LSL/LSR/ASR/ROR, including register-controlled shifts, have
**no** such penalty. Complex shifted operands in ALU instructions can have it.
The abbreviated wording in [the repository notes](docs/rp2350-notes.md) is clarified.

For a nonzero-shift table entry:

| Work | Existing selector 0 | New selector 1 |
|---|---:|---:|
| Twelve word updates | 84 instructions + 12 load-use stalls | 84 instructions, row-load stalls hidden |
| Decode, initial load and entry-loop overhead | approximately 18 cycles | approximately 11 cycles |
| Ideal total per entry | approximately 114 cycles | approximately 95 cycles |

This gives approximately **19 cycles per rotated entry**, of which twelve are
the explicit row-load stalls and seven come from metadata/loop bookkeeping.
The new generic zero-shift case costs approximately 15 more cycles than the
existing specialization. Batch call/setup/save/restore overhead is approximately
31 additional cycles per group. Multiple-register timing and branch folding make
the total less certain than the twelve removed dependencies.

For normal 5/6, the checked tables contain E = 600 entries, G = 150 groups and
Z = 15 zero-shift entries. Thus:

```text
removed row-load stalls          = 12 × (600 − 15) = 7,020 cycles/frame
nominal net estimate             = 19 × (E − Z) − 15 × Z − 31 × G
                                = 11,115 − 225 − 4,650 = 6,240 cycles/frame
planning range, before contention: 5,000–8,000 cycles/frame
```

The table counts come from `DVBS2_CODES` / the existing Python `code(...).rows`;
`Z = count((360 − address // q) % 32 == 0)`. The same approximate model gives:

| Normal rate | E | G | Z | Estimated net cycles/frame saved |
|---|---:|---:|---:|---:|
| 2/3 | 480 | 120 | 20 | 4,720 |
| 3/4 | 540 | 135 | 10 | 5,735 |
| 5/6 | 600 | 150 | 15 | 6,240 |
| 9/10 | 504 | 162 | 10 | 4,214 |

Pilots do not change FEC work. They change available airtime per frame. At
16 Msym/s normal QPSK with pilots:

```text
frame symbols     = 32,400 + 90 + 22 × 36 = 33,282 symbols
frames/second     = 16,000,000 / 33,282 = 480.7403 s⁻¹
one-core budget   = 128,000,000 / 480.7403 = 266,256 cycles/frame
nominal load gain = 6,240 / 266,256 = 2.34 percentage points of one core
```

Using the [prior measured 111,314-cycle packed LDPC stage](results/optimizations/2026-10-15/README.md), the isolated estimate is
approximately 105k cycles/frame. Applying 2.34 points directly to the prior 82.0%
live core-1 work would suggest approximately 79.7%, **only if contention and
scheduling do not change**. This is a hypothesis for the device experiment.

BB+BCH + LDPC + QPSK PL mapping total approximately 296k measured stage cycles per
5/6 frame before this work. That is approximately 55.6% of the two-core aggregate
532,512-cycle airtime budget, before SPI/BB construction, output supervision and
shaping. It is not a parallel scheduling proof: the current LDPC matrix is serial.
QPSK N4 at SYS128 already reaches 64 MS/s per axis. The AFE limit is 65 MSPS,
and integer writer divider 1 is already used. This experiment buys scheduling
margin, not a faster qualified QPSK waveform. At the existing SYS128 split APSK
rates, the same nominal saving would free approximately 1.17 core points for
16APSK at 4 Msym/s and 0.97 points for 32APSK at 2.667 Msym/s. Their PL mappers
and shaping remain additional limits; N2 reconstruction is still unqualified.

## SRAM and bandwidth

Built ELF comparison, including the existing 1 KiB heap and both reserved 2 KiB
stacks:

| Region | Integration base | This implementation |
|---|---:|---:|
| `iqbench` SRAM0–3 remaining | 680 B | 612 B |
| `pvflight` SRAM0–3 remaining | 136 B | 68 B |
| Scratch X remaining before reserved stack | 16 B | 16 B |
| Scratch Y remaining before reserved stack | 304 B | 8 B |
| SRAM4–7 `.sram_hi` remaining | 4,464 B | 4,464 B |

The leaf is 300 bytes at `0x20081620`; scratch Y ends at `0x200817f8`, with the
reserved stack beginning at `0x20081800`. Alignment/veneer changes make the net
scratch-Y growth 296 bytes. `ldpc_matrix` grows 56 bytes; total low-SRAM growth
is 68 bytes including alignment and the four-byte selector in ordinary zeroed
BSS. The cold 160-byte metadata helper runs from flash. No existing hot function
was deliberately moved to another bank. Existing addresses can still shift when
linking. `.sram_hi` remains **NOLOAD data**, not a linker destination for code.

Per entry, selector 1 performs 13 source-word loads, 12 row loads, 12 row stores
and two halfword metadata loads: **152 bytes**. At 5/6 that is 91,200 B/frame,
or 43.84 MB/s at the QPSK frame rate. Selector 0 saves one source load for each
zero-shift entry: only 60 B/frame less. The leaf additionally saves/restores nine
registers once per group, adding 10,800 B/frame, or 5.19 MB/s, plus count-argument
and caller bookkeeping traffic. This is a cycle optimization, not a bandwidth
reduction. Group construction, accumulation, transpose and output DMA traffic are
additional. The N4 output transfers 256 MB/s of packed 16-bit I/Q data through
DMA; reads/writes and different bank ports must not be combined into one fictitious
single-bus bandwidth number.

The critical risk is **instruction-fetch contention in scratch Y**: core 0 uses
`quad_interp` and, with `bchbank 1`, `dvbs2_bch160_continue_y`, while core 1 now
fetches this leaf there. Isolated cycle savings can disappear in the live pipeline.
The [previous bank-placement evidence](results/memory-placement/2026-10-15/comparison.json)
makes both BCH-bank controls necessary.
There is almost no remaining code space; no runtime stack high-water measurement
was made, and the new leaf adds 36 saved-register bytes to the nested call chain.

## Exact coordinating-engineer procedure

These are instructions for the hardware owner; none were run here. Run from this
worktree. Build directory: `firmware/build`; images:
`firmware/build/iqbench.uf2` and `firmware/build/iqbench.elf`.

First complete the commits described below, then build so the embedded identity
is clean and current. The hardware owner should explicitly load that UF2 using
their normal procedure and record its SHA-256/source identity. The runners never
build or flash.

```sh
make build
make check
git describe --always --dirty --abbrev=10
shasum -a 256 firmware/build/iqbench.uf2
```

Stage controls, SYS128, BCH kernel 2, packed mapper and packer. Each following
`raw` invocation prints the selected setting. Use `--port YOUR_PORT` before the
subcommand if automatic selection is inappropriate.

```sh
python3 host/iqbench.py raw 'clock 128000'
python3 host/iqbench.py raw 'bchkernel 2'
python3 host/iqbench.py raw 'mapkernel 1'
python3 host/iqbench.py raw 'ldpcpack 1'
python3 host/iqbench.py raw 'bchbank 1'

# Stage A / B / A for every code and both pilot settings; each device command is
# exactly dvbs2 <code> <pilots> 20. Code8,pilots1 is dvbs2 8 1 20.
for code in $(seq 0 20); do
  for pilots in 0 1; do
    for xor in 0 1 0; do
      python3 host/iqbench.py raw "ldpcxor $xor"
      python3 host/iqbench.py --note "C1 code=$code pilots=$pilots xor=$xor" \
        dvbs2 "$code" --pilots "$pilots" --reps 20
    done
  done
done
```

The host requires the independent Python PLFRAME CRC and symbol count and prints
`crc ok`. For normal 5/6, the expected flushed CRCs are **1,662,785,928 / 32,490
symbols without pilots** and **1,689,259,845 / 33,282 symbols with pilots**, using
seed `0x9E3779B9 ^ 8`. A/B/A CRCs must be identical. Check `ldpc_xor`, `ldpc_pack`
and `bch_kernel` in the JSON logged by iqbench. Inspect `cyc_ldpc`, `cyc_frame` and
`prof.ldpc_groups`; `ldpc_accum` and `ldpc_transpose` should be stable. iqbench's
existing results log receives these new measurements; preserve prior entries.

Correctness passes only with all CRCs/reference lengths equal and no firmware or
host error. An optimization claim additionally requires B's group/LDPC cycles to
improve against both A runs; repeat a noisy comparison. A 5–8k reduction on normal
5/6 is the prediction, not a mandatory correctness threshold. The initial init
time may change because metadata setup changed. `bchbank` affects bounded
frontends, not the unsplit `dvbs2` BCH timing.

Full QPSK SPI/FEC/output, 60 seconds **per stream case**, SYS128, four writers,
L8 / N4 and GPIO28 DDR clock. Replace `BUILD_LABEL` with the exact clean identity
above. Use fresh output directories every time. The existing `gcs` profile runs
both TS and GCS at 8 and 16 Msym/s, and includes injected fault/clock controls.

```sh
# Same-image A/B/A at each BCH placement. Run in the listed order.
for bank in 1 0; do
  for phase in A B A2; do
    xor=0
    if test "$phase" = B; then xor=1; fi
    python3 host/dual_trial.py gcs --clocks 128000 --ms 60000 \
      --writers 4 --row-symbols 4 --quad-profile 1 --code-index 8 --map-kernel 1 \
      --ldpc-pack 1 --bch-bank "$bank" --ldpc-xor "$xor" --dac-clock ddr \
      --expected-git BUILD_LABEL --build firmware/build \
      --out "results/c1-local/bank${bank}-${phase}"
  done
done
```

The primary target is source 6 (GCS), CPW 1, 16,000,000 sym/s, 64 MHz DACCLK.
Require complete duration, runtime frontend/backend frames, no errors, underruns,
ownership faults, TX stalls or SPI integrity faults, zero independently checked
capture mismatches, and GPIO28 equal to IQ_FLAG in every normal capture.
Injected negative controls must be detected for the intended reason. Require
reported `ldpc_xor`, `ldpc_pack=1`, and requested `bch_bank`. Compare
`s2_busy`, `core0_active_busy`, `split_bch_busy`, maximum gaps/lead and frame work,
not just the isolated stage. Keep the selector off if the live path loses margin.

The built-in baseline stages in this runner use the requested selectors; the
explicit A/A2 invocations above provide the selector-0 controls. Also retain the
integration branch's established 60-second profile as a separate binary control
for default-path placement/dispatch regressions. Existing published 83.1%/82.0%
figures are context, not an automatic pass threshold for this different binary.

After manual stage work, restore idle defaults:

```sh
python3 host/iqbench.py raw 'ldpcxor 0'
python3 host/iqbench.py raw 'ldpcpack 0'
python3 host/iqbench.py raw 'bchbank 0'
python3 host/iqbench.py raw 'mapkernel 0'
python3 host/iqbench.py raw 'bchkernel 0'
```

## Candidate ranking and dead ends

| Candidate | Native correctness status | Estimated M33 benefit | Disposition |
|---|---|---|---|
| Scheduled LDPC + byte offsets | All rates/pilots pass; actual source ISA model passes | 5–8k cycles/normal 5/6 frame before contention | Implemented, opt-in `ldpcxor 1` |
| BCH address schedule + loop alignment | Audited only; no new implementation/test | approximately 6,728 cycles/5/6 frame, 5,380 at 2/3 | Deferred: executable SRAM/placement constraints |
| Standalone planar QPSK mapper | No new implementation; canonical reference retained | Gain requires free planar input; separate conversion adds at least 4–10k cycles/frame | Rejected as a separate pass |

Planar derivation: existing scheduled mapper loop has 28 instructions per
16-symbol word. Planar input could remove its RBIT plus eleven gather operations:
12 × 2,025 = 24,300 instructions/normal frame. A separate converter needs those
same twelve operations plus a load/store, already at least 14 instructions/word;
with loop overhead its net penalty is approximately 4–10k base cycles/frame,
before dependencies. A second FE buffer would need 8,108 bytes, exceeding high
SRAM spare. In-place conversion saves the buffer, not the cycles. A useful planar
design must fuse production with LDPC consumption/parity packing, while preserving
the canonical FEC consumers used by higher-order mappings. That broader redesign
was not justified here.

BCH has a concrete scheduling opportunity, unlike another generic table-method
rewrite: three `ADD ..., LSL #5` instructions immediately feed offset ADDs. Compute
all four indices, then scaled addresses, then offsets to hide three penalties.
Word-align the branch target to remove another cycle when its first instruction
is 32-bit. The present 54-instruction loop is approximately 60 cycles/word versus
56 after those changes: 1,682 full words at 5/6 give 6,728 saved cycles; its
two-byte tail is unchanged. Keeping both original and opt-in kernels plus both
bank placements needs approximately 262 main-SRAM bytes and 242 scratch-Y bytes,
which do not fit. A single new Y copy would conflict with this LDPC leaf and force
core-1 BCH into the shaping bank in unsplit modes. Shared dispatch inside every
BCH word loop consumes roughly the cycles saved. No BCH gain is claimed.

Other audited dead ends: BB copying already fuses scrambling (`bb ^ bb_prbs`);
fusing it into the register-full BCH loop needs additional live pointers/spills.
Most BCH table load-use stalls are already hidden. A per-entry LDPC call would
consume its load-stall saving in save/restore overhead; a compact inner word loop
also spends the saving on branches. A separate new metadata allocation is
unnecessary. Fusing final matrix-prefix XOR into packer gathering might save only
approximately 1–2k cycles at 5/6 while complicating every packing path. None was
added speculatively.

Open questions are the retained gain under live scratch-Y contention, default
dispatch/placement overhead against the integration binary, nested stack margin,
and whether another code placement would win. Native timing cannot answer these.
No new RF, AFE timing, EVM, receiver interoperability or N2 qualification is implied.

## Commit status

The requested local commit was attempted, but the environment denied creation of
`/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.git/worktrees/codex-c1-fec-throughput/index.lock`
with `Operation not permitted`. The configured approval policy does not permit
escalation. No commit was created and no push was attempted. All edits remain in
this worktree. After restoring Git-metadata write access, create the requested
small logical commits with:

```sh
git add firmware/src/dvbs2.c firmware/src/dvbs2.h firmware/src/dvbs2_ldpc_asm.S \
  firmware/CMakeLists.txt firmware/src/main.c firmware/src/bench.c \
  firmware/src/bench_dual.c docs/rp2350-notes.md
git commit -m 'Add opt-in scheduled LDPC row updates with reused metadata'
git add Makefile host/native/dvbs2_test.c host/test_dvbs2_ldpc_asm.py host/dual_trial.py
git commit -m 'Validate scheduled LDPC across all rates and expose trial controls'
git add REPORT.md
git commit -m 'Document C1 estimates, validation and device A/B procedure'
```
