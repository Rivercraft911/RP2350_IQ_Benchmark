# C8 on the Pico: 8PSK 2/3 FEC keeps up with 16 Msym/s on two cores

Pico Plus 2, RP2350B A2, SYS128, USB only, experiment build of `rd/c8-fec-combined` (C5 LDPC,
C6 mapper and C7 BCH placement merged into one image). Every output CRC equals the independent
reference in every row.

**Result:** the CPU-only two-core frame schedule (`c3fec`) encodes normal 2/3 8PSK at the
16 Msym/s cadence for 60 s: 43,254 of 43,254 frames on time, worst core-0 service gap 94 µs
(limit 128), core 0 / core 1 busy 82.7% / 79.3%. 5/6 and 3/4 still miss their first frame.
This is FEC only: in-memory GCS source, no ingress, no PIO/DMA output path.

## Images

| Image | Change | Used for | Flash record |
|---|---|---|---|
| `5ff1086b77` | C7 merge | `pair.jsonl`, `cpw1-ab-1`, `cpw1-gap256-diag-1` | `flash-5ff1086b77.txt` |
| `f62a75c746` | + gap diagnostics | `gap-diag-f62a75c746.jsonl` | not kept |
| `eb0b4af27d` | + gap gate starts after worker launch | `cpw1-ab-2`, both 60 s runs, negative control, `stackcheck-after-c3fec.json` | `flash-eb0b4af27d.txt` |

This branch predates main's CMake fix (`a04e6ba`), so these images have a 2 KiB core-0 stack;
deepest use was 1448 B, paint not exhausted, code intact.

## Stage work per frame, `c3pair code 40 layout who options` (`pair.jsonl`)

Thousands of cycles per frame. Masks: 27 = C4 base, +32 C5 LDPC, +64 C6 mapper (123),
+128 private BCH copies (251), +1024 three-table BCH (`best` = 1275, layout 4 only).

One core (who 0), normal 2/3, job A:

| Mask | Frame | BCH | LDPC | Map |
|---|---:|---:|---:|---:|
| 27 | 366.2 | 84.8 | 145.3 | 101.0 |
| 59 (C5) | 297.5 | 84.8 | 84.6 | 99.9 |
| 91 (C6) | 333.7 | 85.0 | 145.7 | 68.9 |
| 123 (C5+C6) | 264.9 | 85.0 | 84.9 | 67.9 |
| best, layout 4 | **247.3** | 65.7 | 87.0 | 67.6 |

Both cores at once (who 2), job A on core 0 / job B on core 1:

| Rate | 123, layout 0 | 251, layout 0 | 251, layout 4 | best, layout 4 |
|---|---:|---:|---:|---:|
| 2/3 (t=10) | 285.4 / 285.7 | 288.2 / 288.6 | 290.3 / 288.1 | **263.0 / 260.9** |
| 5/6 (t=10) | 316.8 / 317.0 | 319.1 / 319.2 | 321.9 / 319.3 | **287.6 / 284.9** |
| 3/4 (t=12) | 353.1 / 353.8 | 326.1 / 326.8 | 334.2 / 331.3 | **305.9 / 303.0** |

Running both cores at once costs 6–9% over one core (shared SRAM and XIP).
Private BCH copies (251 vs 123) help only 3/4, whose t=12 BCH otherwise runs flash C
(143.4k to 116.6k); for t=10 they cost about 3k. The three-table BCH removes 27–34k per
frame at every rate. The A/B/A repeats agree within 0.8k.

## 16 Msym/s cadence, `c3fec code ms 0 1 128 options`

Budget: two frame periods per assigned frame, 2 × 22,194 × 8 = 355,104 cycles per core,
including core 0's supervision. `c3fec` cannot use the three-table BCH: its context borrows
only the 64 KiB OUT8 tail, and the wide table needs up to 120 KiB.

3 s runs (`cpw1-ab-2`):

| Rate | 0 (C3) | 123 | 251 |
|---|---|---|---|
| 2/3 | first frame 1618 µs late, slices over 128 µs | **2162/2162 on time** | **2162/2162 on time** |
| 5/6 | first frame 1685 µs late | first frame 194 µs late | first frame 225 µs late |
| 3/4 | first frame 2046 µs late | first frame 512 µs late | first frame 294 µs late |

60 s, normal 2/3:

| Mask | Frames | Late | Max gap | Busy, core 0 / 1 | Mean frame, core 0 / 1 | Max frame, core 0 / 1 |
|---|---:|---:|---:|---:|---:|---:|
| 251 | 43,254 / 43,254 | 0 | 94 µs | 82.7% / 79.3% | 270.3k / 275.4k | 299.4k / 318.7k |
| 123 | 43,254 / 43,254 | 0 | 96 µs | 83.2% / 78.8% | 272.4k / 273.6k | 294.1k / 314.1k |

The worst single frame used 90% of the 355k budget (core 1); core 0 also spends 6.6% of wall
time on supervision. One 60 s run per mask, not a soak or a temperature test.

Negative control (`cpw1-negative-slow5000-1`): a 5000 µs delay in each worker made the first
frame 5450 µs late; the run stopped and the drained output matched the reference.

## The service-gap gate measured setup

On `5ff1086b77` every 2/3 run failed with one core-0 service gap of 154–180 µs while every
frame was on time (`cpw1-ab-1`; with the gate relaxed to 256 µs, 2162/2162 frames on time,
`cpw1-gap256-diag-1`). `f62a75c746` counted gaps over 128 µs: exactly one per run, ending at
the first service, so it spanned the two job releases, the core-1 launch and the first slice.
`eb0b4af27d` starts the gate after the launch and reports that interval as `startup_us`
(58–64 µs). Deadlines still count from the earlier epoch. After the change the worst gap
during the cadence was 88–106 µs (3 s) and 94–96 µs (60 s).

## What it does not show

No ingress, output path, PIO producers or RF. The integrated 16 Msym/s 8PSK transmitter
still needs B6's producers and these two FEC workers in one image, and B6 currently has its
own core-1 worker. 5/6 and 3/4 need about 30–40k fewer cycles per frame on core 0; the
three-table BCH would supply that if its table fits the cadence image's memory.
