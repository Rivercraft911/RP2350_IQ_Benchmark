# A3 on the Pico: 16APSK to 4.571 Msym/s and 32APSK to 3.556 Msym/s at SYS128

Pico Plus 2, RP2350B A2, SYS128, USB only, default image `02025355fd` (A3 merged with main
`9586dcc`, plus the capture-aim fix below; `flash-02025355fd.txt`). Complete on-chip pipeline:
on-chip PV-SPI GCS source -> BB/BCH/LDPC -> APSK/PL map -> CPU shaper at 2 SPS -> DAC bus,
5/6 with pilots, `ldpcpack 1`, `bchbank 1`. Every positive run: no underrun, BBFRAME and FEC CRCs
equal to the independent reference, 8192 captured bus words equal to the independent
GCS -> FEC -> APSK/PL -> FIR reference including the full 90-symbol PLHEADER.

**Result:** 60 s clean at 16APSK cpw 7 (4.571 Msym/s, 14.73 Mb/s, was cpw 8 / 12.89 Mb/s) and
32APSK cpw 9 (3.556 Msym/s, 14.33 Mb/s, was cpw 12 / 10.75 Mb/s). A3 predicted one CPW step
further for each; those underran in the first frames.

## Measured kernels (cycles per symbol)

| Path | Before | A3 | A3 estimate |
|---|---:|---:|---:|
| 16APSK PL map, option 1 | 17.43 | **5.71** | 5.2–6.2 |
| 32APSK PL map, option 1 | 32.16 | **8.77** | 7.6–9.1 |
| 32APSK native-to-offset conversion, option 2 | 13.4–13.9 | **7.16** | 6.8–8.1 |
| 16APSK map from flash instead of SRAM, option 5 | | 6.52 | |
| 32APSK map from flash, option 7 | | 8.83 | |

Running the mapper from SRAM saves 0.8 cycles/symbol for 16APSK and nothing measurable for
32APSK.

## Frontiers, 3 s, no split

| Mode | Options | CPW: core 0 / core 1 busy | Fails at |
|---|---|---|---|
| 16APSK | 1 | 9: 57 / 60%, 8: 63 / 67%, 7: 72 / 77% | 6 (underrun, core 1 99%) |
| 32APSK | 1 | 12: 68 / 60%, 11: 74 / 64%, 10: 82 / 71%, 9: 90 / 78% | not reached |
| 32APSK | 3 | 10: 69 / 72%, 9: 76 / 78% | 8 (underrun, core 1 99.6%) |

Same-image split baselines (option 0) reproduce the earlier soak: 16APSK cpw 8 88.4% / 81.0%,
32APSK cpw 12 89.7% / 88.9%.

## 60 s

| Mode | CPW | Rate | BB capacity | Core 0 / 1 | Mean / worst frame, core 1 | Frame budget | Min input lead |
|---|---:|---:|---:|---:|---:|---:|---:|
| 16APSK, option 1 | 7 | 4.571 Msym/s | 14.73 Mb/s | 72.2 / 76.5% | 357k / 438k | 467k | 75 words |
| 32APSK, option 3 | 9 | 3.556 Msym/s | 14.33 Mb/s | 75.9 / 78.3% | 376k / 461k | 480k | 13 words |

One run each, not a soak. The worst frame used 94% (16APSK) and 96% (32APSK) of its budget,
and the 32APSK input ring came within 13 words of empty: these pass with little margin.

## Why the predicted rates failed

A3 predicted from mean loads (16APSK cpw 6 at about 89%). The limit is the worst frame:
core 1's slowest frame is 23% above its mean (438k vs 357k at cpw 7), and cpw 6 allows 400k.
The first frame is also expensive (BB 62k vs 26k, LDPC 161k vs 118k), and both failures came
in the first 3–7 ms. The cause of the frame-to-frame spread is not identified.

## Negative controls (3 s, `--expect-deadline-fault`, all faulted as required)

- Original mapper at the new rates: 16APSK cpw 7 option 0, 32APSK cpw 9 option 0 (core 1 99.8%).
- One step beyond: 16APSK cpw 6 option 1, 32APSK cpw 8 option 3.

The planned conversion-only control at 32APSK cpw 8 was not run: option 1 already passes at
cpw 9 (core 0 90%), so option 2 shows up as margin (90% -> 76% core 0), not as a pass/fail.

## Capture aim fixed

On `52bc64985c` (folder `52bc64985c-capture-aim/`) the split 16APSK cpw 8 baseline failed three
times with 0 of 8192 mismatches: the capture began 62–103 bus words after the frame header and
the runner requires the whole header. The firmware aimed one block (1024 symbols) before the
header and the capture landed about one block after the poll (32APSK cpw 12: 367 symbols).
`02025355fd` aims two blocks before; the 8192-word capture spans two blocks, so offsets of
90–2048 symbols are covered. The host check is unchanged.

`stackcheck` after all runs: code intact, core 0 deepest 1728 of 4096 B, core 1 456 of 2048 B.
APSK is still 2 SPS; digital results only.
