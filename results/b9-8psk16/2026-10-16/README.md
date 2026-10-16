# B9 on the Pico: complete on-chip 8PSK 2/3 at 16 Msym/s

Pico Plus 2, RP2350B A2, SYS128, USB only, experiment build. `b9 cpw ms invert prio slow options`:
memory GCS source -> BB -> BCH -> LDPC -> packed 8PSK/PL codes (C8 mask 251, both cores,
frame-parallel) -> four PIO producers -> four DMA row loops -> four OUT8 writers and the GPIO28
DDR clock. No CPU work per sample. Core 0 re-arms the producers' input grants from a DMA IRQ.
Every positive run: full duration, no input/output/DMA fault, exact independent final ring CRC,
all 4096 captured pins32 words equal to the independent GCS -> FEC -> 8PSK/PL -> FIR reference,
normal DDR clock phase, resources restored.

**Result:** 60 s clean at 16 Msym/s with DMA bus priority (mask 13), three runs on the merged
image and one before it: 43,257 frames each, **30.97 Mb/s** of payload. Core 0 86–87% busy
(about 9% of it supervision), core 1 84%. With bus priority 0 the same image stopped twice on
a single Q-writer stall (44.9 s, 52.6 s), so 16 Msym/s needs DMA priority.

## Images

| Image | Change | Folders |
|---|---|---|
| `c87c54dfbf` | B9 as delivered | `run-3s-1` |
| `19744a4f40` | frames 2/3 start before output; register faults sampled every fourth service; idle wait | `run-3s-2`, `run-3s-3`, `run-60s-1`, `run-ab*`, `run-slow-1`, `run-clock-1`, `stackcheck.json` |
| `e6d8776b0f` | merged with main (A3, Q1); A3's conversion runs from flash in this image | `merged-*`, `stackcheck-merged.json` |

## First image: ran dry after the prefill

On `c87c54dfbf`, 8 Msym/s ran 3 s clean but 16 Msym/s stopped with SOURCE_EMPTY after 2.8 ms,
exactly the two frames encoded before output started (`run-3s-1`; the cpw 2 / priority 13 case
did not run because the session's tool call failed). Frames 2 and 3 were released only when
output started, so frame 2 had two frame periods (355,104 cycles) for about 305k cycles of FEC
plus supervision, which cost about 840 cycles per 32-µs service (about 20% of core 0).
`19744a4f40` starts frames 2/3 during the prefill and starts output once core 0's frame 2
reaches its credit-gated mapper, reads only memory on three of four services and samples the
PIO/DMA fault registers on the fourth (about every 128 µs), and lets idle core 0 wait for the
next service instead of spinning in 1-µs steps (the 31% "supervision" at 8 Msym/s was mostly
that spin). The 128-µs service gate is unchanged.

## 60 s runs

| Image | Rate | Priority | Result | Core 0 / 1 busy | Supervision | Worst frame, core 0 / 1 | Max service gap |
|---|---|---:|---|---:|---:|---:|---:|
| `19744a4f40` | 16 Msym/s | 0 | clean | 89.7 / 79.5% | 10.0% | 302k / 318k | 118 µs |
| `19744a4f40` | 16 Msym/s | 13 | clean | 87.0 / 84.6% | 10.0% | 304k / 318k | 126 µs |
| `19744a4f40` | 8 Msym/s | 0 | clean | 46.3 / 38.6% | 8.4% | 302k / 319k | 126 µs |
| `19744a4f40` | 8 Msym/s | 13 | clean | 46.1 / 39.1% | 8.3% | 302k / 318k | 123 µs |
| `e6d8776b0f` | 16 Msym/s | 13 | clean, 3 of 3 | 85.6 / 83.8% | 9.2% | 298k / 314k | 108–113 µs |
| `e6d8776b0f` | 16 Msym/s | 0 | Q-writer stall at 44.9 s and 52.6 s | 88.4 / 78.8% | 9.3% | 297k / 313k | 107–111 µs |

Frame budget is 355,104 cycles per alternate frame: the worst frame used 90%. The service gate
(128 µs) came within 2 µs on one run. One 60 s run per row unless noted; not a soak or a
temperature test.

## Controls (3 s)

- FEC A/B: with mask 0 (C3 kernels) 16 Msym/s fails (frames 516–559k cycles, service gap
  130 µs); 8 Msym/s passes at 68.8 / 60.4% against 46.1 / 39.1% with mask 251.
- `slow_us 5000` after the prefill: `source_underrun_detected` at 16 and 8 Msym/s, exact
  drained CRC, no stale-ring replay.
- Inverted DDR clock: `clock_inversion_detected` at both rates with the waveform matching.
- Q1 on the merged image: 9/10 TS at 16 Msym/s with the core-0 adapter, 3 s clean, core 1 71.1%.

`stackcheck` after the runs: code intact, core 0 deepest 2192 of 4096 B, core 1 500 of 2048 B.

## What it does not show

The source is in memory; the external SPI link measured 24.1 Mb/s, below the 30.97 Mb/s this
mode carries (the wide-ingress branch addresses that). No AFE, RF or receiver. 5/6 and 3/4 do
not fit this schedule yet (C9).
