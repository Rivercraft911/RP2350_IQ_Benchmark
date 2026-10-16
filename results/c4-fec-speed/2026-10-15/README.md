# C4 on the Pico: on-chip 8PSK FEC 10–25% cheaper, still just over the 16 Msym/s budget

Pico Plus 2, RP2350B A2, SYS128, USB only, image `d72d346df6` (`rd/c4-fec-speed`, experiment
build, `CORE0_STACK_SRAM_HI=ON`; `flash.txt`). Command `c3fec code ms slow cpw gap_us options`
(two-core frame-parallel GCS -> BB -> BCH -> LDPC -> packed 8PSK codes, timer-paced, no output
path), 3 s per record, all in `sweep.jsonl`. Options: 1 SRAM LDPC kernels, 2 table mapper with
prepacked header/pilots, 4 scratch-Y streamed BCH, 8 fused GCS/BB scrambling, 16 cached
deadlines; 0 = C3, 31 = all. `stackcheck` after the sweep: code intact, core-0 deepest stack use
1440 of 4096 bytes.

## Steady state at 8 Msym/s (cpw2; 1081/1081 frames on time unless noted)

Work per assigned frame, thousands of cycles, core 0 / core 1 (stage sums; supervision and loop
work are in `busy`):

| Rate | C3 (mask 0) | All C4 options (31) | Change | Busy at 31, core 0 / 1 |
|---|---:|---:|---:|---:|
| 2/3 | 407.5 / 422.1 | **364.0 / 364.3** | −11% / −14% | 59.3% / 54.3% |
| 5/6 | 429.3 / 447.5 | **383.5 / 383.6** | −11% / −14% | 61.4% / 56.7% |
| 3/4 | 505.0 / 530.7 (missed one frame) | **395.6 / 397.7** | −22% / −25% | 63.3% / 58.8% |

Single options (2/3, core 0): LDPC 160.9 -> 150.0k; mapper 128.6 -> 108.1k; fused GCS
32.5 -> 25.3k; BCH unchanged for t=10 but 141.2 -> 108.2k for 3/4's t=12; cached deadlines
reduce busy, not stage sums. Combined (31) LDPC 146.6–154.3k, mapper about 105k.

## At 16 Msym/s (cpw1)

Budget: 355,104 cycles per core per assigned frame. Every rate missed its **first** frame
deadline, at both gap limits, so no steady state was reached: first-frame work 454–510k cycles,
with BCH at 145–185k against 86–110k in the 8 Msym/s steady state (startup transient, not
investigated). Against the steady-state numbers above, the stage work alone is 2.5% (2/3),
8% (5/6) and 12% (3/4) over budget, before supervision (core 0 spends about 57k extra cycles
per assigned frame at 8 Msym/s, roughly half that per frame at 16 Msym/s).

## Reading

- 8 Msym/s with on-chip FEC now needs 54–63% of each core, against 61–80% for C3 and 88% of one
  core for the single-core B2 encoder.
- 16 Msym/s 2/3 is within about 3–10% of fitting. LDPC (about 147k) remains the largest stage;
  the old single-core pack1 path measured about 108k. A benchmark with startup slack is needed
  to measure 16 Msym/s steady state rather than the first frame.
- Output-free CPU benchmark: not yet integrated with the PIO/DMA output engine.
