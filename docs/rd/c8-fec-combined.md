# C8: combined C5/C6/C7 FEC

The C7 merge after `0a55e75` is resolved in the working files. C5 LDPC, C6 mapping,
and C7 BCH/transpose experiments have independent selectors in one image. C7's
three-table diagnostic is layout **4**; C6 retains layout **3**. Layout 4 now keeps
Gold outside its high-SRAM context, so the maximum C5/C6 code arenas fit.
Git metadata and recorded result evidence were left unchanged. The coordinator must
stage the resolved files to clear Git's unmerged index entries. No device, flash,
serial port or `picotool` was accessed.

## Final option and layout contract

| Bit | Symbol | Feature / precedence |
|---:|---|---|
| 1 | `C4_LDPC` | C4 SRAM LDPC leaf |
| 2 | `C4_MAP` | C4 table mapper |
| 4 | `C4_BCH` | C4 scratch-Y BCH |
| 8 | `C4_BB` | Fused GCS/BB scrambling |
| 16 | `C4_DEADLINES` | Cached absolute deadlines |
| 32 | `C5_LDPC` | C5 group/rows/fused pack bundle; overrides bit 1, works alone |
| 64 | `C6_MAP` | Packed 8PSK mapper; overrides bit 2, works alone |
| 128 | `C7_BCH_PLACE` | Per-worker private BCH copies; overrides bit 4 |
| 256 | `C7_BCH_LDM` | BCH multiword-load leaf; alone uses its source in XIP, with 128 uses private copies |
| 512 | `C7_TRANSPOSE` | Private transpose callback for LDPC modes 0/1; redundant with C5 on Arm |
| 1024 | `C7_TABLE3` | Three-table BCH; implies private BCH, requires `c3pair` layout 4; incompatible with 256 |

`C8_ALL = 2047` is the final mask and numeric parser boundary everywhere. It describes
all allocated bits, **not a legal all-features combination**: 256+1024 is rejected.
`c3fec` additionally rejects any numeric mask with bit 1024, because its context has
no wide table. The internal frontend bit `S2_FE_LDM_BCH = 64` is a separate API flag;
C3 explicitly translates command bit 256 into it in both fused and ordinary BB paths.
Defaults remain `c3fec=0` and `c3pair=31`, with bit-identical output.

The named 8PSK candidate is `C8_BEST = 1275 = 27+32+64+128+1024`.
`c3pair 5 40 4 2 best` selects it and **requires layout 4**. It excludes scratch-Y
BCH, LDM and the redundant transpose copy. `C8_BEST_BASE = 251 = 27+32+64+128`
is its supported non-wide counterpart: `c3fec 5 3000 0 1 128 best` and the host
runner's `--options best` select 251. The numeric 1275 remains rejected by `c3fec`;
there is no silent masking of numeric requests. “Best” names a candidate assembled
from separate measured improvements; its combined device timing remains unmeasured.

| Layout | Placement and constraints |
|---:|---|
| 0 | Both jobs and shared Gold/LUT/LDPC code in SRAM0–3; C7 private code split between striped groups |
| 1 | Job B, its rings, Gold/LUT/LDPC code in SRAM4–7; hammers rejected |
| 2 | Layout 0 data, LDPC code fetched from SRAM4–7 |
| 3 | Layout 0 data/LDPC, C6 mapper code fetched from SRAM4–7; requires bit 64 |
| 4 | Both jobs and shared mapper/LDPC code in `cap_buf`; wide BCH table, Gold, counters and core-0 C7 code in `ring_buf`; hammers rejected |

Pair codes 5/6/8/9/10 remain supported; cadence codes remain 5/6/8. C7's historical
[report](c7-bch-place.md) and [measurements](../../results/c7-bch-place/2026-10-16/README.md)
use their original bits 32/64/128/256 and layout 3. They are immutable provenance,
not the command contract of this combined image.

C5's Arm pack bundle contains its own local transpose. Installing bit 512 still
captures a private callback, but C5 mode 2 executes the bundle and never calls it.
There is no additional C7 transpose saving to add to C5. Modes 0/1 use the callback;
the native C5 model also uses it while applying C5's fused prefix. Both routes are
checked against independent references, alongside the separate assembly semantics
checks for C5's local transpose and C7's copied transpose.

## Storage fix and placement

Every mask reserves the same maximum 8192-byte C5 LDPC and 5008-byte C6 mapper
arenas. Preparation, code copying and table construction occur before timed work.
No new static arena, DMA channel, PIO state machine or PIO instruction is added.
C7 adds 8 bytes to the existing frontend state in the static dual encoder; changed
code/alignment also affects linker headroom. The large experiment contexts borrow
idle-only buffers, and commands restore the input PRBS afterwards.

The old direct union would make the wide context 59028 bytes, 1684 bytes larger
than `cap_buf` (57344 bytes). Layout 4 externalizes its 5536-byte Gold array.
The ring's fixed half-open byte intervals are:

| Reservation | Offsets from `ring_buf` | Bytes |
|---|---|---:|
| Maximum wide BCH table | `[0,122880)` | 122880 |
| Gold | `[122880,128416)` | 5536 |
| Shared worker state and substep counters | `[128416,128544)` | 128 reserved |
| Core-0 BCH / transpose code | `[128544,129056)` / `[129056,129568)` | 512 each |
| Unused ring tail | `[129568,131072)` | 1504 |

Compile-time size/alignment/overlap assertions cover the table, Gold, shared-state
reserve, private code, high/low contexts, rings and hammer tiles. Layout-4 off/on
runs reserve identical regions. The table's actual degree-dependent size is
81920 bytes (t8), 102400 (t10), or 122880 (t12). Its maximum ends exactly where
Gold begins. Both C7 code slots fit their complete copied leaves. The who-7
transpose hammer stays outside the active high C5/C7 executable arenas.

Final Release experiment ELF/map and ARM DWARF (code entries omit the Thumb bit):

| Object | Address / extent |
|---|---|
| `ring_buf` / `in_buf` / `cap_buf` | `0x20000000..0x20020000` / `0x20020000..0x20028000` / `0x20051fac..0x2005ffac` |
| `c3fec` context / size | `0x20010000..0x2001e7f8`, 59384 B; 6152 B spare in its 64 KiB |
| `c3fec` Gold / mapper arena | `0x20019864` / `0x2001ae04` |
| `c3fec` C4 / C6 mapper entries | `0x2001b194` / `0x2001b594` |
| `c3fec` C5 group / rows / pack | `0x2001c194` / `0x2001dad4` / `0x2001db54` |
| `c3fec` core-0 BCH / transpose | `0x2001e194` / `0x2001e394` |
| `c3fec` core-1 BCH / transpose | `0x20051fac` / `0x200521ac` |
| Pair normal lane / low / high sizes | 39180 / 78872 / 55564 B; high has 1780 B spare |
| Pair layout-0 A Gold / LUT / C6 | `0x20004bdc` / `0x2000617c` / `0x2000690c` |
| Pair layout-0 C5 group / rows / pack | `0x2000750c` / `0x20008e4c` / `0x20008ecc` |
| Pair layout-0 core-0 BCH / transpose | `0x2000950c` / `0x2000970c` |
| Pair layout-0 B job / low context end | `0x2000990c` / `0x20013418` |
| Pair layout-1 B Gold / LUT / C6 | `0x20056b88` / `0x20058128` / `0x200588b8` |
| Pair layout-1/2 high C5 group | `0x200594b8` |
| Pair layout-0/1/2/3 core-1 BCH / transpose | `0x2005b4b8` / `0x2005b6b8` |
| Pair layout-1 B rings / high end | `0x2005b8b8` / `0x2005f8b8` |
| Pair layout-3 C6 entry (both jobs) | `0x200588b8`; low LUT/Gold/jobs retain layout-0 addresses |
| Pair layout-4 A job / B job / end | `0x20051fac` / `0x2005a318` / `0x2005f0a0` |
| Pair layout-4 LUT / C6 | `0x20056b88` / `0x20057318` |
| Pair layout-4 C5 group / rows / pack | `0x20057f18` / `0x20059858` / `0x200598d8` |
| Pair layout-4 core-1 BCH / transpose | `0x20059f18` / `0x2005a118` |
| Pair layout-4 Gold / shared reserve | `0x2001e000..0x2001f5a0` / `0x2001f5a0..0x2001f620` |
| Pair layout-4 core-0 BCH / transpose / reserved end | `0x2001f620` / `0x2001f820` / `0x2001fa20` |
| Existing byte BCH table | `0x20071f50..0x20079f50` |
| Lower BSS end / minimum heap end | `0x2003fbfc` / `0x2003fffc`: **4 B free** |
| High allocation end / core-0 stack bottom | `0x2007efe8` / `0x2007f000`: **24 B free** |
| Scratch-X end / core-1 stack bottom | `0x200807f0` / `0x20080800`: **16 B free** |
| Scratch-Y end / bank end | `0x20081e68` / `0x20082000`: **408 B free** |

ARM `c3_job_t` / frontend / LDPC state are 19336 / 48 / 92 B; the wide context is
53492 B, leaving 3852 B. Shared counters occupy 92 of 128 reserved bytes.
C5 copies 8166 of 8192 bytes (26 B spare); C6 copies 2516 bytes into its 3072-byte
code slice. C7 existing t8/t10/t12 copies are 208/240/258 B; LDM 186/210/224 B;
three-table 132/156/176 B; transpose 308 B. Every private function fits 512 B.
The experiment `pvflight` has **0 B** after its minimum heap and 344 B below its
core-0 stack. Default-build lower gaps are 544 B (`iqbench`) / 0 B (`pvflight`).
No existing static buffer or hot section was deliberately reassigned to a different
SRAM region; linked addresses can shift with code/state growth.

## Offline verification

`make build` and the required Release experiment configure/build passed for both
`iqbench` and `pvflight`, without warnings. **Full `make check` passed**, including
all existing native/model/reference tests and the final Markdown link check.
`git diff --check` passes; no conflict markers remain in the resolved source/docs.
Targeted native checks passed:

- **84 masks × 3 rates × 16 continuous frames = 4032 frames** through the actual C3
  graph. All C4 combinations, every bare C5/C6/C7 bit, override combinations,
  best 251/1275, private transpose with/without C5, LDM and wide-table combinations
  are sampled. The harness installs actual native leaf/table/callback selections
  after begin and checks frontend flags, LDPC/map modes, table CRC, canaries,
  odd-first completion, every pair's physical rings and carry against Python.
- **435 valid production `c3pair` runs**, including a real pthread core-1 worker,
  five rates, all five layouts, same-layout table3 off/on, and private transpose
  with/without C5; every physical word/CRC matches independent Python. Ten invalid
  requests check rejection and restoration. Layout 4 validates external Gold,
  table, state and private-code intervals against diagnostic addresses.
- Production `c3fec` virtual-time tests sample 35 valid masks per rate and slow
  controls, and reject negative/out-of-range/table3 requests before input/ring
  mutation. Compiled production `main.c` parser/dispatch branches verify `best`,
  numeric boundaries and unchanged defaults for both commands. Synthetic clocks
  test accounting and are not device measurements.
- C7 native BCH checks cover all eleven normal rates, all 15360 wide-table row
  indices across degrees, uneven/arbitrary remainder continuation, frontend pairs,
  guards and the corrupted-used-table negative. Assembly-semantics checks pass
  31848 cases, including 1058 copied-transpose cases; C5 pack semantics pass 768
  cases. Existing component/reference checks remain included in `make check`.

Native execution uses C equivalents rather than executing relocated Thumb code.
Assembly checks interpret actual instruction semantics; neither establishes silicon
timing. In particular, the native C5 fallback uses a callback, while the separately
checked Arm C5 pack bundle owns its local transpose.

To honor the explicit ban on `picotool`, these local build caches set
`PICO_NO_PICOTOOL=ON`. This disables SDK post-link `coprodis` and UF2 conversion;
ELF/BIN/HEX and map/disassembly still build. Existing UF2 files in the ignored build
directories predate this resolution and must not be flashed as this image.
Reproduce the offline builds without invoking that tool:

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

Experiment artifacts from this uncommitted resolution (Git identity alone is insufficient):

```text
iqbench.elf bcc80a5c6a3a25027951339bb5b79777ceffb66f10e7e4af5bff17bd3dfd89cc
iqbench.bin d7a35ac95b27f64ef8b021a841f773c6873a345475c8de7b303e85d202fdc679
pvflight.elf c5a2f6ab103a022ef50c7516111e513f5032c98f95ada50e7ca53afc29ff4390
```

## Cycle budget and evidence

At SYS128 and 16 Msym/s, there are 8 SYS cycles/symbol. The 22194-symbol PLFRAME
gives each alternating core `2 × 22194 × 8 = 355104` cycles (2774.25 us), before
supervision. The 128-us service gate is 16384 SYS cycles per slice. C5 keeps its
four-group LDPC slices; C6 keeps its 32-word mapper slices.

Separate-image measurements at 2/3 give concurrent C5 LDPC 89.3k cycles
([C5](../../results/c5-ldpc/2026-10-16/README.md)), C6 mapping 72.7k
([C6](../../results/c6-map8/2026-10-16/README.md)), and C7 three-table BCH 67.2k
([C7](../../results/c7-bch-place/2026-10-16/README.md)). Adding approximately 22k
GCS gives **251k stage cycles**, leaving about 104k before supervision. Using
C7 private byte-table BCH (91.5k) instead gives roughly **276k**, leaving 79k.
These are cross-image arithmetic estimates, not combined measurements, timing
bounds or evidence of successful startup/cadence.

The [C7 instruction model](c7-bch-place.md#instruction-work-and-estimates) counts
33 t10 three-table loop instructions, 45 after expanding LDM transfers. With a
taken branch it plans roughly 46 cycles/whole word: `1345 × 46 = 61870` cycles,
then slices/entry/append produce an optimistic 63–65k solo estimate. The measured
C7 result was 65.7k solo / 67.2k concurrent, above the original 65k target.
The unchanged t12 planning range is 86–91k and t10 5/6 is 79–82k, excluding new
contention. See [M33 timing notes](../rp2350-notes.md); host timings and mocked
cycle counters provide no M33 performance evidence.

## Coordinator: exact same-image A/B commands

The following console lines were **not executed here**. The coordinator packages
and flashes the matching experiment image, archives ELF/UF2 hashes, map and source
diff, and uses fresh result files. Do not compare old branch-local mask numbers.
Run at SYS128; repeat off/on/off at least three times. `who=0` runs sequentially
on core 0, `1` sequentially on core 1, and `2` releases one job on each core together.

First isolate C5/C6/private BCH at code 5, then the same combined A/B at all three
rates. Mask 123 is C4 27+C5+C6; 251 adds private BCH. Mask 1275 adds table3; `best`
is exactly 1275 here. Compare table3 within layout 4 to keep job placement fixed.

```text
info
stackcheck
c3pair 5 40 0 0 27
c3pair 5 40 0 0 59
c3pair 5 40 0 0 91
c3pair 5 40 0 0 123
c3pair 5 40 0 0 251
c3pair 5 40 0 2 123
c3pair 5 40 0 2 251
c3pair 5 40 0 2 123
c3pair 5 40 3 2 251
c3pair 5 40 4 0 251
c3pair 5 40 4 0 best
c3pair 5 40 4 0 251
c3pair 5 40 4 2 251
c3pair 5 40 4 2 best
c3pair 5 40 4 2 251
c3pair 8 40 0 2 123
c3pair 8 40 0 2 251
c3pair 8 40 0 2 123
c3pair 8 40 4 2 251
c3pair 8 40 4 2 best
c3pair 8 40 4 2 251
c3pair 6 40 0 2 123
c3pair 6 40 0 2 251
c3pair 6 40 0 2 123
c3pair 6 40 4 2 251
c3pair 6 40 4 2 best
c3pair 6 40 4 2 251
```

Repeat the layout-4 251/best/251 commands with `who=1` for codes 5/8/6. Attribute
changes using both cores' stage averages/maxima and LDPC substeps, retain exact
reported code/table/job addresses, and require all requested runs. Pair CRCs must
remain equal across every mask/layout and match these independent values:

| Code | crc_a | crc_b |
|---:|---:|---:|
| 5 | 1273684834 | 2540800392 |
| 8 | 1863126860 | 2777209518 |
| 6 | 4157714373 | 1725042067 |
| 9 | 1538593740 | 3547307867 |
| 10 | 2021468233 | 2222914226 |

Check private transpose with and without C5, and isolate the optional LDM leaf:

```text
c3pair 5 40 0 2 155
c3pair 5 40 0 2 667
c3pair 5 40 0 7 155
c3pair 5 40 0 7 667
c3pair 5 40 0 2 251
c3pair 5 40 0 2 763
c3pair 5 40 0 2 507
c3pair 5 40 4 2 1275
c3pair 5 40 4 2 1787
```

155 = C4 27+private BCH (no C5/C6); 667 adds private transpose. With C5, 251/763
and 1275/1787 should preserve output and should show no intrinsic pack reduction
from bit 512. 251/507 isolates LDM. Repeat useful comparisons for codes 8/6.
The original instruction target remains ≤65000 concurrent BCH cycles at code 5,
≤81300 at code 8 and ≤87800 at code 6; record misses honestly. Whole-stage sums
must also stay below 355104 before attempting cadence qualification.

Full schedule A/B at **cpw 1, gap_us 128**, with explicit lines for every rate:

```text
c3fec 5 3000 0 2 128 123
c3fec 5 3000 0 2 128 best
c3fec 5 3000 0 1 128 123
c3fec 5 3000 0 1 128 best
c3fec 5 3000 0 1 128 123
c3fec 8 3000 0 1 128 123
c3fec 8 3000 0 1 128 best
c3fec 8 3000 0 1 128 123
c3fec 6 3000 0 1 128 123
c3fec 6 3000 0 1 128 best
c3fec 6 3000 0 1 128 123
c3fec 5 60000 0 1 128 best
c3fec 8 60000 0 1 128 best
c3fec 6 60000 0 1 128 best
c3fec 5 3000 5000 1 128 best
stackcheck
```

Here `best` is 251. The runner independently checks the actual published rings and
carry. Run short positives first, then 60-second runs, each with a fresh `--out`:

```sh
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 5 8 6 --options 123 best \
  --cpw 1 --gap-us 128 --ms 3000 --out results/c8-fec-combined/coordinator-cpw1-ab
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 5 8 6 --options best \
  --cpw 1 --gap-us 128 --ms 60000 --out results/c8-fec-combined/coordinator-cpw1-soak
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 5 --options best --slow-us 5000 \
  --cpw 1 --gap-us 128 --ms 3000 --out results/c8-fec-combined/coordinator-negative
```

Pass requires independent ring/carry agreement, `errors=0`, zero late frames and
lateness, target = published = consumed, both cores' maximum slices ≤16384 SYS
cycles, service gap ≤128 us, intact code and stack checks. Startup failure counts
as failure. Relaxing the gap does not qualify 16 Msym/s. The 5000-us slow-worker
negative must report deadline error/lateness, stop releases and drain exact published
output; a healthy-looking negative means the deadline check failed.

Also try invalid requests `c3pair 5 1 0 2 1275`, `c3pair 5 1 4 2 1531`
(table3+LDM), and `c3fec 5 100 0 1 128 1275`: each must reject before worker launch
or buffer setup. The native table-corruption control must detect a parity mismatch.

## Risks, dead ends and intended commits

The direct wide union was rejected because it overflowed `cap_buf`; shrinking C5/C6
arenas would corrupt executable copies. External Gold preserves maximum arenas and
all existing layout meanings. A second C7 transpose benefit on top of C5 was rejected
because C5 already has a private transpose. Static SRAM and C5 code reserves remain
very tight; linker/toolchain changes require fresh maps and copied-leaf checks.
Default output is unchanged, but extra dispatch and linked addresses can change
baseline timing, so all measurements must use same-image A/B.

The wide table consumes waveform LUT space and cannot coexist with the current B6
OUT8 shaping allocation. Two FEC workers also conflict with B6's dedicated core-1
grant worker. Pair/cadence success would establish CPU-only FEC behavior, not complete
16-Msym/s shaping, DMA/FIFO runway or RF performance. Shared SRAM/XIP/USB contention,
startup timing and relocated Thumb execution remain device checks.

No commits were made. Intended small logical commits:

1. `Makefile`, `firmware/CMakeLists.txt`, `firmware/src/{c3_fec.c,c3_fec.h,bench_c3_fec.c,bench_c3_pair.c,dvbs2.c,dvbs2.h,dvbs2_asm.S,dvbs2_bch_fast.S,main.c}`:
   **Combine C5/C6/C7 FEC selectors and fit layout 4 with external Gold.**
2. `host/{c3_fec_trial.py,test_c3_pipeline.py,test_c3_bench.py,test_c3_fec_trial.py,test_c7_pair.py,test_c7_bch.py,test_bch_fast_asm.py}`,
   `host/native/{c3_pipeline_test.c,c7_bch_test.c}`:
   **Verify combined selectors, placement layouts and best aliases against independent references.**
3. `docs/rd/{README.md,c8-fec-combined.md,c7-bch-place.md}`:
   **Record final FEC masks, layout budgets and coordinator A/B qualification.**
