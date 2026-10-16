# C7 on the Pico: private per-core BCH and transpose copies, and a three-table BCH (−22%)

Pico Plus 2, RP2350B A2, SYS128, image `06f5a1124d` (`rd/c7-bch-place`, experiment build).
`c3pair code 40 layout who options`, all in `pair.jsonl`. Bits on this branch: 32 private BCH
copies per worker, 64 BCH multiword loads, 128 private transpose copies, 256 three-table BCH
(layout 3 only). Output-ring CRCs are identical in every row.

| Change (8PSK 2/3 unless noted) | One core | Both cores together |
|---|---:|---:|
| BCH, scratch-Y leaf (31) | 85.0k | **152.3k** |
| BCH, private striped copies (63) | 84.5k (job A), 89.8k (job B copy in SRAM4-7) | **91.5k** |
| BCH, striped bank-0 leaf (27), reference | | 87.4k |
| Transpose/pack, scratch-X (63) vs private copies (191) | 26.9k vs 26.9k | **34.1k vs 29.2k** |
| BCH multiword loads (255) vs 191 | 82.4k vs 84.5k | 89.0k vs 91.1k |
| **BCH three tables** (447, layout 3) vs 191 (layout 3) | **65.7k** vs 84.5k | **67.2k** |
| 3/4 BCH, flash C (27) vs private striped t=12 copy (63) | 132.3k vs **107.5k** | 119.6k |
| Core-1 loop hammering SCRATCH_X (who 7): pack, 63 vs 191 | 32.1k vs **27.1k** | |

`stackcheck`: code intact, core-0 deepest stack 1424 B of 4096. Design, SRAM placement and native
evidence: [docs/rd/c7-bch-place.md](../../../docs/rd/c7-bch-place.md).
