# Scheduled LDPC rows (`ldpcxor 1`): faster in isolation, slower live

**Result:** the scheduled LDPC leaf saves 3.9–5.9k cycles per frame in the isolated stage,
within 5–8% of the predicted saving, with identical frame CRCs. In the complete 16 Msym/s
QPSK pipeline it makes both cores **busier**: core 0 +11.0 points, core 1 +5.5 points.
Keep it off by default.

Image `4d599b1edc` (branch `rd/c1-fec-throughput`, UF2 sha256 `5f36c997…a3f5`, `flash.txt`),
SYS128, `bchkernel 2`, `mapkernel 1`, `ldpcpack 1`, `bchbank 1`, GPIO28 DDR clock.
`driver.py` → `runs.jsonl`, `driver-output.txt`. Pico Plus 2, RP2350B A2, on-chip SPI master.

Stage A/B/A, `dvbs2 <code> 1 20` (LDPC stage cycles; both A runs identical):

| Code | xor 0 | xor 1 | Saved | Predicted |
|---|---:|---:|---:|---:|
| normal 2/3 | 107,579 | 103,094 | 4,485 | 4,720 |
| normal 3/4 | 114,426 | 108,957 | 5,469 | 5,735 |
| normal 5/6 | 111,074 | 105,132 | 5,942 | 6,240 |
| normal 9/10 | 100,172 | 96,278 | 3,894 | 4,214 |

Live QPSK 5/6, 16 Msym/s, SPI GCS, four writers, L8, 60 s each, all clean (0 bus and 0
clock-parity mismatches, no underruns):

| Run | Core 0 active | Core 1 work | Encoder busy |
|---|---:|---:|---:|
| A: xor 0 | 82.87% | 81.88% | 69.63% |
| B: xor 1 | 93.80% | 87.40% | 75.15% |
| A2: xor 0 | 82.87% | 81.88% | 69.63% |

A and A2 agree to 0.01 points, so the regression is not drift. The leaf lives in SCRATCH_Y
(4 KiB, unstriped), which also holds core 0's `quad_interp` shaping kernel and, with
`bchbank 1`, its BCH copy. Core 0's work is unchanged by the selector yet rises by 11 points,
which fits instruction-fetch contention in that one bank; this is a hypothesis, not a
measured cause. The test is `ldpcxor 1` with `bchbank 0` (and the leaf moved out of
SCRATCH_Y if space can be found). Isolated stage savings do not predict live gains when hot
code shares a bank.
