# Two-core FEC and SRAM banks: the scratch-bank BCH code is the big conflict; LDPC is mostly instruction-bound

Pico Plus 2, RP2350B A2, SYS128, USB only. Branch `rd/ldpc-banks`, command `c3pair code pairs
layout who options` (`firmware/src/bench_c3_pair.c`): encodes frame 0 (job A) and frame 1 (job B)
40 times with C3/C4's per-frame FEC (GCS -> BB -> BCH -> LDPC -> packed 8PSK codes) and reports
cycles per stage, per job. `who` 0 = core 0 runs both jobs (no concurrent FEC), 1 = core 1 runs
both, 2 = core 0 and core 1 start their jobs together (both cores always in the same stage, the
worst case), 3..7 = core 0 runs both while core 1 hammers one resource. `layout` 0 = everything in
SRAM0-3; 1 = job B's buffers, rings, gold, LUT and its own LDPC leaf copy in SRAM4-7; 2 = layout
0 with the LDPC leaf fetched from SRAM4-7. Output-ring CRCs are identical across all layouts and
modes (2/3 `84834`, 5/6 `9518` low digits), so placement does not change the produced codes.
Records: `pair.jsonl`, `pair-bch.jsonl`, `hammer.jsonl`, `hammer2.jsonl`, `ldpc-sub.jsonl`,
`split-code.jsonl`, with the flashed images.

## 1. BCH code in the single SCRATCH_Y bank is the dominant conflict

C4 option 4 runs the streamed BCH leaves from SCRATCH_Y, one unstriped 4 KiB bank. Both cores
fetching it at once (image `2c74cfca1a`, `pair-bch.jsonl`, thousands of cycles per frame):

| Rate | BCH alone | BCH both cores, SCRATCH_Y (options 31) | BCH both cores, striped SRAM0-3 (options 27) | Frame both cores, 31 -> 27 |
|---|---:|---:|---:|---:|
| 2/3 | 84.3 | **151.8** | 87.9 | 474 -> 410 |
| 5/6 | 105.6 | **188.5** | 109.6 | 495 -> 413 |
| 3/4 | 107.2 (t=12 leaf) / 131.6 (C, flash) | 184.9 | 139.8 (C, flash) | 510 -> 461 |

Alone, the two placements cost the same for t=10 (84.3k either way). Use options 27 (all of C4
except the SCRATCH_Y BCH) for two-core work. 3/4 needs a striped-SRAM copy of the t=12 leaf; it
has none yet.

## 2. With BCH fixed, both cores together cost 8–15% more than one core

| Rate (options 27) | One core alone | Both cores together | Difference |
|---|---:|---:|---:|
| 2/3 | 356–360k | 390k (`45393cb567`) / 410k (`2c74cfca1a`) | +8–15% |
| 5/6 | 374–376k | 403.5k / 413k | +7–10% |

The same source measured 390k on one image and 410k on another that differed only in about 60
bytes of lower-SRAM layout (where small shared globals and code fall in the four striped banks).
In the 390k image the LDPC sub-steps changed alone -> together (2/3): clear 9.9 -> 11.0, rotate-XOR
groups 84.5 -> 90.5, row accumulation 9.7 -> 10.1, apply 10.5 -> 10.7, transpose/pack
26.8 -> 33.5. The pack step's transpose runs from SCRATCH_X, which also holds core 1's stack; a
core-1 loop running that transpose alone adds 5.3k to core 0's pack step.

## 3. Moving buffers or code to the other bank group barely helps

| Change (2/3, both cores, options 27) | LDPC per frame |
|---|---:|
| Everything in SRAM0-3 (layout 0) | 156.4k / 174.5k (two images) |
| Core 1's job, rings, tables and LDPC leaf in SRAM4-7 (layout 1) | 152.4k / 171.2k |
| LDPC leaf fetched from SRAM4-7, data in SRAM0-3 (layout 2) | 154.0k |

Alone, fetching the LDPC leaf from the other bank group changes nothing (84.5k -> 84.8k groups):
the rotate-XOR loop is limited by its instructions (about 700 cycles per 360-bit group, roughly
1.4 cycles per instruction), not by code and data sharing banks. Single core-1 hammers cost core 0
5–19k per frame (flash-code loop 8k, SRAM-resident loop 6k, SRAM0-3 or SRAM4-7 copies 17–19k).

## Reading

- Fixed: keep FEC code out of the single-bank scratch memories when both cores run it (BCH now;
  the transpose next). That removes 64–83k cycles per frame in the worst case.
- Remaining two-core penalty: 8–15%, spread over many stages and sensitive to linker layout.
- The 16 Msym/s gap is mainly the single-core cost: 2/3 needs 356k per frame alone against a
  355,104-cycle budget per core, before supervision. LDPC's rotate-XOR groups (84k), the mapper
  (100k), BCH (84k) and pack (27k) must shrink in instruction count, not just move.
- Worst case only: both cores here run the same stage at the same moment. In a continuous
  16 Msym/s schedule the cores are staggered by one frame period.
