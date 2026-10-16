# C9 on the Pico: 5/6 and 3/4 move closer to the 16 Msym/s cadence but still miss the first frame

Pico Plus 2, RP2350B A2, SYS128, USB only, experiment image `bc314bfa19` (`rd/c9-fec-fit` on
main `9586dcc`; `flash-bc314bfa19.txt`). New bits: 2048 compact four-plane BCH table, 4096
cheaper `c3fec` schedule with larger BCH/LDPC slices, 8192 fused q=45 LDPC packer. All 20
`c3pair` runs (100 frames per job) give the independent output CRCs for codes 5/8/6.

## Stage work, both cores at once (`pair.jsonl`, `c3pair code 100 0 2 mask`)

Thousands of cycles per frame, job A on core 0 / job B on core 1. 251 = C8 control,
507 = +LDM BCH, 4603 = 507 + schedule, 6395 = 251 + compact BCH + schedule,
12539 / 12795 = q45 packer without / with LDM, 14587 = compact BCH + schedule + q45.

| Rate | 251 | 507 | 4603 | 6395 | 12539 | 12795 | 14587 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2/3 | 288.5 / 288.9 | 284.7 / 285.1 | 278.3 / 278.7 | **274.9 / 275.1** | | 278.5 / 279.0 | |
| 5/6 | 318.2 / 319.0 | 311.1 / 311.8 | 304.7 / 305.4 | **303.9 / 304.0** | | 306.0 / 306.7 | |
| 3/4 | 328.4 / 329.6 | 327.6 / 328.8 | 321.9 / 323.0 | | 312.9 / 314.1 | **311.8 / 312.9** | 314.6 / 315.7 |

The final 251 repeat of each code agrees with the first within 0.1k. The q45 packer cuts 3/4
LDPC from 105.7k to 92.8k; the larger slices (4096) save about 6k; LDM and the compact table each
save 4–8k of BCH at t=10.

## 16 Msym/s cadence (`code8-ab-1`, `code6-ab-1`, `c3fec code 3000 0 1 128 mask`)

Every run missed its **first** frame deadline; none reached steady state:

| Rate | 251 | 507 | 4603 | 6395 | 12539 | 12795 | 14587 | 251 again |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 5/6, lateness | 239 µs | 152 µs | 37 µs | **27 µs** | | | | 174 µs |
| 3/4, lateness | 266 µs | | 169 µs | | 111 µs | **109 µs** | 120 µs | 273 µs |

Supervision fell from 9.2–9.8% to 6.7–8.1% with bit 4096. The 60 s runs and slow-worker
negatives were not run, since no mask passed the short run.

## Reading

`c3fec` starts frames 0 and 1 at the epoch with no output runway, so frame 0 gets exactly one
steady-state budget while cold. In steady state 5/6 at mask 6395 needs about 304k plus about
8% supervision against 355k per frame. A transmitter with a prefill (as B9 encodes two frames
before output starts) is the fair test of that; B9 currently admits only 2/3.

`stackcheck` after the runs: code intact, stack paint not exhausted.
