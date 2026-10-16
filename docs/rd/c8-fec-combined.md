# C8: combine the FEC experiments

The current C6 merge conflicts are resolved with **C5 LDPC and C6 mapping together**.
C7 is not merged yet: the coordinator must stage/commit this resolution and start
`git merge rd/c7-bch-place`. Git metadata and recorded results were left unchanged;
no device transport, flashing or serial port was used.

## Option contract

| Bit | Feature | Rule / current status |
|---:|---|---|
| 1 | C4 SRAM LDPC | Unchanged |
| 2 | C4 table mapper | Unchanged |
| 4 | C4 scratch-Y BCH | Unchanged |
| 8 | Fused GCS/BB | Unchanged |
| 16 | Cached deadlines | Unchanged |
| 32 | C5 LDPC | Overrides bit 1; works without it |
| 64 | C6 mapper | Overrides bit 2; works without it |
| 128 | C7 private BCH copies | Reserved for next merge; supersedes bit 4 |
| 256 | C7 BCH LDM loads | Reserved for next merge |
| 512 | C7 private transpose | Reserved for next merge |
| 1024 | C7 three-table BCH | Reserved for next merge; incompatible with 256 |

One firmware `C8_ALL` controls all option bounds: **127 now**, **2047 after C7**.
The host runner mirrors it by name and tests the accepted/rejected boundary.
Unimplemented bits are rejected now. Defaults remain `c3fec=0`, `c3pair=31`;
output bits remain identical. C6's branch-local bit 32 is now 64; historical
reports/results retain their original mask meanings.

Layouts 0/1/2 keep their existing meanings; C6 layout **3** selects only the high
mapper code copy and requires bit 64. C7's three-table layout must become **4**.
All masks reserve the same maximum arenas: LDPC **2048 words / 8192 B**, mapper
`DVBS2_8PSK_CODES_C6_WORDS` (**1252 words / 5008 B**). Copies and preparation stay
outside timed work. C5's four-group LDPC grants and C6's 32-word map slices remain.

## Verification and budgets

`make build` and the required Release experiment configure/build passed for both
`iqbench` and `pvflight`, without introduced warnings. Reproduce:

```sh
make build
PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0" \
PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1" \
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
make check
```

The native pipeline samples **59 masks × 3 rates × 16 continuous frames** (2832
frames): every C4 combination plus C5/C6 combinations, bare feature bits, both
overrides and masks 123/127. Every pair's physical rings/carry match independent
Python references; odd-first completion, wraps and canaries pass. The native code
also asserts the selected LDPC and mapper modes. Command tests cover combined
positive/slow-worker cases; runner tests accept 127 and reject 128. **Full
`make check` passed**, including component assembly-semantics checks and Markdown
links. Native execution does not execute relocated Thumb instructions.

At SYS128, 16 Msym/s is 8 SYS cycles/symbol. Each alternate frame gives one core
`2 × 22194 × 8 = 355104` cycles, or **2774.25 us**, before supervision.
[C5 measurements](../../results/c5-ldpc/2026-10-16/README.md) give 89.3k LDPC cycles
with both cores; [C6 measurements](../../results/c6-map8/2026-10-16/README.md) give
72.7k map cycles. Adding approximately 22k GCS and 88k striped BCH suggests
**272k stage cycles / frame** for combined 2/3, leaving roughly 83k cycles.
This is cross-image arithmetic, not a combined measurement or a scheduling bound.
The eventual three-table combination suggests about **250k**, also unmeasured in
combination. Host elapsed times provide no M33 cycle evidence. Instruction counts
and timing assumptions remain in [C5](c5-ldpc.md), [C6](c6-map8.md) and
[the M33 notes](../rp2350-notes.md).

Current ARM borrowed sizes: `c3fec` context **58296 B** (7240 B spare in 64 KiB),
pair lane **38132 B**, low context **76776 B** (below the hammer at 98304 B), high
context **54516 B** (2828 B spare in `cap_buf`). The transpose hammer retains C5's
safe `cap_buf + 4096` tile, outside active high code in layouts 0/2/3. No permanent
arena was allocated. Final linker addresses/headroom are recorded below.

Experiment Release ELF/map/DWARF addresses (code entries omit the Thumb bit):

| Object | Address / extent |
|---|---|
| `ring_buf`, `in_buf`, `cap_buf` | `0x20000000`, `0x20020000`, `0x20051fac` |
| `c3fec` context | `0x20010000..0x2001e3b8` |
| `c3fec` Gold / mapper arena | `0x20019834` / `0x2001add4` |
| `c3fec` C4 / C6 mapper entries | `0x2001b164` / `0x2001b564` |
| `c3fec` C5 group / rows / pack | `0x2001c164` / `0x2001daa4` / `0x2001db24` |
| Pair low-A LUT / C4 / C6 mapper | `0x20006164` / `0x200064f4` / `0x200068f4` |
| Pair low-A C5 group / rows / pack | `0x200074f4` / `0x20008e34` / `0x20008eb4` |
| Pair low-B job / low context end | `0x200094f4` / `0x20012be8` |
| Pair high-B LUT / C4 / C6 mapper | `0x20058110` / `0x200584a0` / `0x200588a0` |
| Pair high-B C5 group / rows / pack | `0x200594a0` / `0x2005ade0` / `0x2005ae60` |
| Pair high rings / high context end | `0x2005b4a0` / `0x2005f4a0` |
| Lower BSS end / minimum heap end | `0x2003fbb4` / `0x2003ffb4`: **76 B free** |
| High data end / core-0 stack bottom | `0x2007efe8` / `0x2007f000`: **24 B free** |
| Scratch-X code end / core-1 stack bottom | `0x200807f0` / `0x20080800`: **16 B free** |
| Scratch-Y code end / bank end | `0x20081e68` / `0x20082000`: **408 B free** |

C5's copied bundle is 8166 B, leaving only **26 B** in its arena. The experiment
`pvflight` image has only **8 B** after its minimum heap; its high gap is 344 B.
Default-build lower gaps are 552 B (`iqbench`) / 8 B (`pvflight`). No static
placement was moved. Small linker gaps, shared SRAM traffic and relocated-code
execution remain risks requiring same-image device checks.

Experiment image SHA-256 (the source diff identifies this uncommitted resolution):

```text
iqbench.elf 80fdde103bb7075f36ffa1f31afd28b694db743fc981a0e9a05f3fc182fecd59
iqbench.uf2 55f93f0507b1b5e0f49bf0ad51595179c0328a97b198487c58ddb46135718500
```

## Coordinator: current C5+C6 image

Send these firmware commands after independently flashing the experiment image;
retain ELF/UF2 hashes, map, source diff, firmware identity and fresh result files.
27 is the C4 striped-BCH control; 59 adds C5, 91 adds C6, 123 adds both.

```text
info
stackcheck
c3pair 5 40 0 0 27
c3pair 5 40 0 0 123
c3pair 5 40 0 0 27
c3pair 5 40 0 2 27
c3pair 5 40 0 2 59
c3pair 5 40 0 2 91
c3pair 5 40 0 2 123
c3pair 5 40 0 2 27
c3pair 5 40 1 2 123
c3pair 5 40 2 2 123
c3pair 5 40 3 2 123
c3pair 5 40 0 2 31
c3pair 5 40 0 2 127
c3fec 5 3000 0 2 128 27
c3fec 5 3000 0 2 128 123
c3fec 5 3000 0 1 128 27
c3fec 5 3000 0 1 128 123
c3fec 5 3000 0 1 128 27
c3fec 5 60000 0 1 128 123
c3fec 5 3000 5000 1 128 123
stackcheck
```

Repeat the positive 27/123 pair and cadence comparisons with code 6 (3/4) and 8
(5/6), including who 1 for core-1-only attribution. Scratch-Y 31/127 diagnoses
contention; it is not the recommended two-core configuration. Require equal pair
CRCs across masks/layouts (code 5 A/B: **1273684834 / 2540800392**; code 8:
**1863126860 / 2777209518**), all requested jobs, and intact stack/code checks.
Record each core's stage averages/maxima, total cycles and service slices.

`c3fec ... 1 128 ...` is the actual **16 Msym/s cadence and service gate**. Pass:
independent ring/carry agreement, `errors=0`, zero late frames/lateness, target =
published = consumed frames, `max_service_gap_us <= 128`, and slices <= **16384
SYS cycles**. The last 5000-us slow-worker command is the negative control: it must
fail the deadline check and drain exact published output. Startup failure counts
as failure; increasing `gap_us` does not qualify 16 Msym/s.

The runner verifies independent CRCs automatically; repeat at `--cpw 2` and then
60000 ms after the short positive run, with a fresh `--out` each time:

```sh
python3 host/c3_fec_trial.py --port "$PORT" --expected-git "$REV" \
  --build firmware/build-exp --codes 5 6 8 --options 27 59 91 123 \
  --cpw 1 --gap-us 128 --ms 3000 --out results/c8-fec-combined/coordinator-cpw1-ab
```

## C7 next-merge preparation and unresolved work

Read-only review of `git diff main...rd/c7-bch-place` found these required changes:

- Remap C7 bits 32/64/128/256 to 128/256/512/1024; extend `C8_ALL` to 2047
  everywhere, retaining separate capability rejection. Three-table BCH remains
  `c3pair` layout 4 only; `c3fec` must explicitly reject bit 1024 because it has no
  table allocation. Reject table3+LDM, hammers in layouts 1/4, and layout 3 without
  C6 bit 64. Preserve C7's code 9/10 diagnostic coverage.
- Translate command bit **256** to frontend-local `S2_FE_LDM_BCH=64` in both
  `c3_job_step` frontend paths. C7 currently passes masked command options into an
  8-bit frontend field; blindly remapping that expression would discard the flag.
  Keep frontend-local test literals and API flags in their own bit domain.
- A direct union makes C7's compact wide context **59028 B**, exceeding 57344 B
  by **1684 B**. One concrete unimplemented fix is to externalize its 5536-B Gold:
  reserve ring byte offsets `[0,122880)` for the maximum table,
  `[122880,128416)` for Gold, `[128416,128544)` for shared counters (128-B reserve),
  and `[128544,129568)` for core-0 BCH/transpose copies. This leaves 1504 B in the
  ring and reduces the wide high context to **53492 B**, with 3852 B spare.
  Refactor only layout 4's table access/init, add overlap/size assertions, and
  verify actual ARM offsets. Merely keeping the smaller C5/C6 arena is incorrect.
- C5's pack leaf already contains a private local transpose. C7 bit 512's callback
  affects LDPC modes 0/1; in C5 mode 2 it is redundant on ARM. Document this and test
  both paths instead of promising another 5k saving on top of C5.
- Extend sampled native options and C7 production-command tests, including bare
  bits, overrides, invalid combinations and the corrupted-table negative control.
  Private leaves require caller installation after begin; option bits alone in
  the portable pipeline do not exercise placement. Keep the assembly checks.

After resolving those items and rebuilding/retesting, the planned exact combined
A/B commands are (not accepted by the current image):

```text
c3pair 5 40 0 2 123
c3pair 5 40 0 2 251
c3pair 5 40 0 2 763
c3pair 5 40 0 2 1019
c3pair 5 40 4 0 763
c3pair 5 40 4 0 1787
c3pair 5 40 4 0 763
c3pair 5 40 4 2 763
c3pair 5 40 4 2 1787
c3pair 5 40 4 2 763
c3fec 5 3000 0 1 128 123
c3fec 5 3000 0 1 128 763
c3fec 5 3000 0 1 128 1019
c3fec 5 60000 0 1 128 763
c3fec 5 3000 5000 1 128 763
stackcheck
```

251 = 123+private BCH; 763 adds private transpose; 1019 adds LDM; 1787 adds
three-table BCH instead of LDM. Repeat the layout-4 off/on/off at codes 6/8 and
retain all current pass criteria. Compare table3 within layout 4 so the job
placement is held fixed. The 250k expectation belongs to this large-table pair
diagnostic; it does not establish `c3fec` timing. Three-table storage consumes
the live waveform LUT space, and the B6 core-1 grant-worker conflict also remains;
these CPU-only commands do not qualify integrated shaping or RF performance.

No commits were made. Intended small logical commits:

1. `firmware/CMakeLists.txt`, `firmware/src/{c3_fec.c,c3_fec.h,bench_c3_fec.c,bench_c3_pair.c,main.c}`:
   **Resolve C5/C6 FEC merge with independent option bits and maximum arenas.**
2. `host/{c3_fec_trial.py,test_c3_pipeline.py,test_c3_bench.py,test_c3_fec_trial.py}`,
   `host/native/c3_pipeline_test.c`:
   **Verify independent and combined C5/C6 selectors against native references.**
3. `docs/rd/{c8-fec-combined.md,README.md}`:
   **Record combined FEC validation, device trials and C7 allocation constraints.**
