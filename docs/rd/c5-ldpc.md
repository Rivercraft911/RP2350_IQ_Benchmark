# C5: fewer LDPC instructions and passes in the concurrent encoder

C5 is implemented as **option bit 32**, with native bit-exact verification. The
**95,000-cycle LDPC target remains unmeasured**. Instruction reductions make it
worth testing, particularly at 2/3; they do not establish that either rate meets
the target on an M33. No Pico, serial device, flashing command or device runner
was accessed. No Git metadata or existing result evidence was written.

## Selection and implementation

`c3pair code pairs layout who options` and
`c3fec code ms slow_us cpw gap_us options` accept masks 0..63. Bit 32 selects
LDPC mode 2, independently of bit 1, and takes precedence when both are present.
For the useful striped-BCH comparison, **27 is C4; 59 is C4 plus C5**.
32 isolates C5 against C3; 33 combines it with bit 1 and produces the same LDPC
selection. 31/63 include scratch-Y BCH and remain useful contention controls.
The `c3pair` omitted-mask default remains 31; `c3fec` remains 0. Old APIs and
mode 0/1 selections remain available. The original C4 group section was assembled
and compared with HEAD: its instruction bytes are unchanged.

The new `dvbs2_ldpc_prepare_mode`, `copy_mode` and `code_bytes_mode` APIs select
mode 1 or 2. The original functions wrap mode 1. Mode 2 copies a single immutable
bundle into the caller's aligned arena, with DSB/ISB before execution:

1. The group leaf fuses extraction and duplication of the 360-bit source window.
   Successive groups start at bit offsets 0, 8, 16 and 24, so four immediate
   extraction paths replace the two general loops. The 8-bit path can store most
   duplicated words directly from the input registers. Nonzero rotations retain
   C4's scheduled scalar load/shifted-XOR/store body. Zero rotations use small
   three-word bursts. The stack window and group ABI are unchanged.
2. PIC row leaves clear or accumulate up to eight rows per unit. They use bounded
   register bursts, without a C call/dispatcher round trip for every row.
3. A PIC pack leaf applies the exclusive prefix while gathering each tile. This
   eliminates the separate q-by-12-word apply pass. At q60/q30 it also fuses the
   gather with transpose stages 16 and 8. The remaining transpose and both fixed
   packs run in the copied bundle, away from SCRATCH_X. Other q values use an
   internal generic transpose and streaming pack.
4. The separate C5 driver processes four information groups per unit. It retains
   a one-tile pack unit. Stage numbers remain compatible with `c3pair`:
   `[clear, groups, accumulation, prefix, apply, pack]`; C5's apply bucket is zero.
   A tiny tail-call dispatcher avoids allocating the baseline packer's local
   tile on C5 calls. The C driver itself remains in flash.

Both jobs still own their matrices, continuation state and frame. Helpers derive
their addresses from the job's captured group pointer, so `c3pair` layouts 1/2
move the entire bundle correctly. No shared matrix or mutable assembly scratch
was introduced. Initialization/copying remains outside the timed work. Arena
contents and configuration must remain fixed until all workers finish; after
another command borrows the arena, prepare again before a direct mode call.

The transpose hammer's tile moved from `cap_buf + 8192` words to `+4096`.
The old location overlaps the larger C5 bundle in layout 2. The replacement lies
in the unused high job's storage for layouts 0/2; hammering with layout 1 remains
rejected.

## Instructions and cycle budget

Counts below come from interpreting the expanded assembly and the existing
independent DVB-S2 address tables. They count executed opcodes, including group
setup/return, rather than estimating from assembly source lines.

| Item | C4 | C5 |
|---|---:|---:|
| Group setup/return, input offset 0/8/16/24 | 200 each | 77 / 79 / 100 / 99 |
| Nonzero-rotation edge | 72 | 72 |
| Zero-rotation edge | 60 | 35 opcodes; 59 after minimum burst-word expansion |
| 2/3: 120 groups, 460 shifted + 20 aligned edges | 58,320 | 44,470; 44,950 with burst expansion |
| 5/6: 150 groups, 585 shifted + 15 aligned edges | 73,020 | 55,936; 56,296 with burst expansion |
| q60 fused apply/gather/transpose/pack, whole frame | separate passes | 14,356 + 1,344 additional burst words |
| q30 fused apply/gather/transpose/pack, whole frame | separate passes | 7,524 + 672 additional burst words |

For reproduction, group setup is `sum(S[g % 4])`, with
`S = [77,79,100,99]`, over the code's groups. Each address x contributes offset
`360 - x // q`; count it as aligned when that offset modulo 32 is zero. Add
72 per shifted edge and 35 per aligned edge. Add another 24 per aligned edge
for the minimum multiword-transfer expansion. Setup alone removes 13,350 decoded
instructions at 2/3 and 16,709 at 5/6. The pack test prints its frame counts.

The [timing notes](../rp2350-notes.md) distinguish ALU instructions, result-use
penalties and taken branches; LDM/STM/PKH timings are explicitly unverified.
Every additional transferred word is charged here: fewer LDM/STM opcodes do not
mean the same number of cycles saved. Pack push/pop expansion adds another 192
transfers/frame in either case, before branches and dependencies. A conservative
no-contention planning allowance for the fused pack is **19–23k cycles at q60,
10–13k at q30**, plus the C caller. These are estimates, not host timings or M33
measurements.

The measured control in
[ldpc-sub.jsonl](../../results/ldpc-banks/2026-10-15/ldpc-sub.jsonl), job A,
layout 0/who 0/options 27, is:

| Rate | Clear | Groups | Accumulate | Prefix | Apply | Pack | Stage total |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2/3 | 9,946 | 84,486 | 9,662 | 340 | 10,493 | 26,941 | 141,870 |
| 5/6 | 4,984 | 105,580 | 4,774 | 336 | 5,274 | 14,263 | 135,212 |

The remaining savings are removal of setup-loop branches/dependencies and most
cheap-pass dispatch calls. C5 has 59 units/frame at both target rates, versus
312/252 units in C4. Eight-row clear/accumulation leaves have approximately
1k/3.4k word-expanded execution floors at q60, before caller overhead; q30 costs
about half. A reasonable experiment-planning envelope is **roughly 90–105k
cycles at 2/3 and 90–110k at 5/6**. This deliberately spans 95k: SRAM contention,
flash driver/cache behavior and unverified multiple-transfer timings can decide
the outcome. It is not a demonstrated speedup or an upper bound.

At SYS128/16 Msym/s, an alternate frame has 355,104 cycles per core. A 95k LDPC
would leave about 54.5k cycles after the 2/3 GCS/BCH/map stage sum, or 28.6k at
5/6, before supervision and contention. The measured two-core 8–15% penalty can
consume this margin. An LDPC improvement alone is not qualification of the whole
16 Msym/s transmitter.

Four-group grants contain at most 52 edges at either target rate. Using a
deliberately loose 120 cycles/edge plus 250/group and 1k driver allowance gives
8,240 cycles, about 64.4 us at SYS128. This is a scheduling estimate, not a bound
under arbitrary bus traffic. Measure `max_slice_cycles` and service gaps against
the 128-us requirement. Zero grants and completed calls retain their old behavior.

## SRAM and placement

Both commands reserve **8,192 bytes per existing instruction arena**, up from
7,168. This borrows 1,024 additional bytes/lane from existing buffers; it adds no
static code arena. Context sizes are invariant across option masks within the
image. The permanent C5 prepared pointer costs four bytes of lower SRAM.

| Copied component | Exact copied bytes | Aligned extent |
|---|---:|---:|
| C5 group | 6,462 | 6,464 |
| Clear/accumulate | 126 | 128 |
| Pack including private transpose | 1,574 | 1,574 |
| Bundle | **8,166** | **26 bytes spare in 8 KiB** |

`copy_mode` rejects insufficient capacity before any copy. Each copied executable
section has no code relocations or external branch/literal dependency; debug
sections do contain normal debug relocations. The old C4 copy remains 5,578 bytes.

Final experiment-image linker addresses (GCC Arm 14.2, SDK 2.2.0):

| Object / code | Address or extent |
|---|---|
| `ring_buf`, `in_buf`, `cap_buf` | `0x20000000`, `0x20020000`, `0x20051fac` |
| C5 driver, in flash | `0x10011ec0` |
| C5 group source | `0x10017a24` .. `0x10019362` exclusive |
| Row source / accumulate entry | `0x10019364` / `0x1001937c` |
| Pack source | `0x100193e4` .. `0x10019a0a` exclusive |
| Pair low A group / rows / pack | `0x200068f4` / `0x20008234` / `0x200082b4` |
| Pair high B group / rows / pack | `0x200588a0` / `0x2005a1e0` / `0x2005a260` |
| `c3fec` group / rows / pack | `0x2001b564` / `0x2001cea4` / `0x2001cf24` |
| Pair low B job / high B rings | `0x200088f4` / `0x2005a8a0` |
| Lower BSS end | `0x2003fbb4`: 76 B beyond the reserved 1 KiB heap |
| High SRAM data end / core-0 stack bottom | `0x2007efe8` / `0x2007f000`: 24 B gap |
| SCRATCH_X code end / core-1 stack bottom | `0x200807f0` / `0x20080800`: 16 B gap |

ARM `sizeof` results: C3 job 19,312 B, pair lane 35,060 B, pair low/high
70,632/51,444 B, `c3fec` context 55,224 B (10,312 B spare in its borrowed 64 KiB).
No high-SRAM table or legacy SCRATCH_X transpose was moved. Only C5 uses the new
striped-SRAM transpose copy. Compare options in one image: arena and flash-layout
changes invalidate exact comparisons with older images.

Disassembly gives C5 driver 64 B plus pack leaf 324 B = **388 B LDPC peak**.
The public dispatcher has no frame. Group/row leaf peaks are 136/32 B. Estimated
core-1 call chains are about 492 B for `c3pair`, 500 B for `c3fec`, excluding SDK
entry and interrupts. These are static stack calculations; use `stackcheck`
before/after commands and after JSON output to verify high water and code integrity.

## Native and build verification

- `make check`: **passed**, including native/reference, assembly semantics,
  protocol, output models and the existing regression suite. No USB is used.
  After the final tail-dispatch adjustment, the parallel, pipeline and command
  suites were also rerun and passed.
- `host/test_dvbs2_parallel.py`: 128 independent Python FEC vectors, all **16
  supported reentrant LDPC rates** (six normal, ten short), modes 0/1/2, real host
  threads and deterministic interleaving. Dirty minimum-sized workspaces,
  canaries, grants 0/1/3/17/128/UINT32_MAX, idempotence and rejected begins are
  checked. Short degree-168 BCH remains unsupported by the concurrent frontend;
  those LDPC cases start with serial-checked BCH information. Normal rates retain
  full frontend coverage. q>64 remains rejected by the concurrent API; the legacy
  all-rate encoder/reference tests remain in `make check`.
- `host/test_c3_pipeline.py`: all 64 masks, three normal rates, 16 continuous
  frames each; independent Python physical rings, carry, wraps and odd-first
  completion. Assertions check bit 32 overrides bit 1.
- Source-instruction semantics: group 336 cases / 6,998,238 interpreted
  instructions, all 64 rows × 361 offsets × four alignments, repeated edges,
  patterns, AAPCS/guards/136-B stack; row leaves 320 cases, continuations and
  32-B stack; pack 768 cases covering every q1..64, fills 0/1/16/31, full/tail
  tiles, arbitrary prefixes, guards/AAPCS/324-B stack. Old C4's 80 cases pass.
- Command/protocol tests accept 32/33/59/63, reject 64, preserve output
  accounting and test slow-worker controls. Native concurrent tests also pass
  AddressSanitizer and UndefinedBehaviorSanitizer without diagnostics.
- `make build` and the requested experiment configuration/build pass. No new
  warnings: the installed toolchain still reports its existing `crtn.o` missing
  `.note.GNU-stack` linker warning.

Build reproduction (the Makefile supplies these SDK/toolchain defaults for its
own commands; export them for the direct CMake invocation):

```sh
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
make build
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
make check
```

Final experiment artifacts, SHA-256 (rebuilding may change the build timestamp):

```text
iqbench.elf f86536c597a5aaa07d31b907afb5e02d8bf14baaa502bc5261991575e611495d
iqbench.uf2 1b385321ee139c3dd1ffd8a4cf240a9d723a1c642fe4c34a29f428f9887d27a4
```

These checks execute portable C and source-instruction semantics, not assembled
Thumb instructions from relocated SRAM. ARM copy guards and helper address
derivation were inspected in source/disassembly. Device execution, two-core
cycles, stack high water and output integration remain coordinator work.

## Coordinator commands and pass criteria

Only the coordinator should run these on the device, after independently flashing
the experiment image. These are command lines to send to the firmware, not shell
commands. Record firmware identity, ELF/UF2 SHA-256, linker map and fresh evidence;
the Git string alone cannot identify uncommitted worktree changes.

```text
info
stackcheck
c3pair 5 40 0 0 27
c3pair 5 40 0 0 59
c3pair 5 40 0 0 27
c3pair 8 40 0 0 27
c3pair 8 40 0 0 59
c3pair 8 40 0 0 27
c3pair 5 40 0 1 27
c3pair 5 40 0 1 59
c3pair 8 40 0 1 27
c3pair 8 40 0 1 59
c3pair 5 40 0 2 27
c3pair 5 40 0 2 59
c3pair 8 40 0 2 27
c3pair 8 40 0 2 59
c3pair 5 40 1 2 59
c3pair 5 40 2 2 59
c3pair 5 40 2 7 27
c3pair 5 40 2 7 59
stackcheck
```

Require identical CRCs for every mask/layout: code 5 A/B =
`1273684834 / 2540800392`; code 8 = `1863126860 / 2777209518`.
For the stated performance target require **both jobs' `avg_stage.ldpc <= 95000`**
in who 0 and who 1 at both rates, with repeated off/on/off comparisons.
Report maxima and all six sub-buckets, not just total frame throughput. Who 2 is
the contention test; it does not replace the single-core target. Who 7 checks
whether moving C5 transpose removes the old SCRATCH_X sensitivity, but its result
can still include core-1 stack/bus effects. For 3/4 regression coverage repeat
the 27/59 who 0/2 commands with code 6.

```text
c3fec 5 60000 0 2 128 27
c3fec 5 60000 0 2 128 59
c3fec 8 60000 0 2 128 27
c3fec 8 60000 0 2 128 59
c3fec 5 60000 0 1 128 27
c3fec 5 60000 0 1 128 59
c3fec 8 60000 0 1 128 27
c3fec 8 60000 0 1 128 59
c3fec 5 3000 5000 1 128 59
stackcheck
```

The cpw2 runs establish the existing 8 Msym/s regression baseline; cpw1 tests the
16 Msym/s CPU-only schedule. Require exact independent output rings/CRCs,
`errors=0`, no late frames, all requested frames published/consumed, service gaps
at most 128 us, slices at most 16,384 SYS cycles and intact stack/code checks.
The final **5,000-us slow-worker command is the negative control**: it must report
a deadline/late-frame failure, while any published output remains exact. It must
not be accepted as a clean 16 Msym/s run. Existing `host/c3_fec_trial.py` checks
the independent rings and slow-worker outcome; it accepts `--options 27 59`
and must use a fresh output directory and an explicit coordinator-owned port.
No integrated B6/B7 or RF success is implied by these CPU-only commands.

## Dead ends, limits and intended commits

Nonzero-rotation LDM/STM bursts reduced 72 opcodes to 50, but 37 transferred
words require at least 25 additional burst-word cycles: 75 before further
dependencies. That candidate was removed. C4's scalar schedule was already good.
Specializing every edge by nearby destination rows would need an edge reorder
and source-window reuse scheme; no saving was established, so it was not added.
Clear cannot simply merge into later accumulation because arbitrary rotate-XOR
updates need initialized rows. Prefix application can merge into pack and does.
The legacy pack1 result was evidence to investigate placement, not a cycle promise
for two private matrices. Moving all C code into static SRAM is infeasible at the
current headroom; only immutable PIC leaves use borrowed arenas here.

Risks: 26 B of arena slack, very small linker gaps, shared striped-SRAM fetch/data
contention, a larger four-group service slice, and unmeasured Thumb execution.
Native exactness does not prove the 95k target, QPSK device timing, or a complete
16 Msym/s schedule. There is no device evidence in this report.

No commits were created. Intended small logical commits:

1. `firmware/src/dvbs2_ldpc_asm.S`, new `dvbs2_ldpc_c5_rows.S` and
   `dvbs2_ldpc_c5.S`, `dvbs2.{c,h}`, `firmware/CMakeLists.txt`:
   **Add opt-in C5 reentrant LDPC preparation and fused SRAM parity packing.**
2. `firmware/src/c3_fec.{c,h}`, `bench_c3_fec.c`, `bench_c3_pair.c`, `main.c`,
   `host/c3_fec_trial.py`: **Expose C5 option bit and keep bank diagnostics disjoint.**
3. `Makefile`, `host/test_ldpc_asm.py`, new `host/test_c5_ldpc_{asm,rows,pack}.py`,
   `host/native/{dvbs2_parallel_test,c3_pipeline_test}.c`,
   `host/test_{dvbs2_parallel,c3_pipeline,c3_bench,c3_fec_trial}.py`:
   **Verify C5 assembly semantics, all concurrent rates and option combinations.**
4. `docs/rd/c5-ldpc.md`: **Record C5 budgets, verification and coordinator A/B criteria.**
