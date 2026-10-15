# Encoder optimizations: device measurements

**Result:** two opt-in changes from branch `opt/limits-hotpaths` cut measured CPU work at
SYS128 with bit-identical output. Together they free 3.2 points of core 0 and 6.3 points
of core 1 for QPSK 5/6 at 16 Msym/s. With the LDPC packer, SPI **TS** input at
16 Msym/s 5/6 now passes 60 s; every earlier attempt underran at 99.9% core 1.

Firmware `5e38990269` (UF2 sha256 `f2c3ffc0…2da2373`), branch `opt/limits-hotpaths`
at the commit in `source-commit.txt`, sources in `firmware-source.tar.gz`, flash record
`flash.txt`. It is based on `34195ce` and does not contain the DDR clock change; all
runs here use the HSTX test clock. Pico Plus 2, RP2350B A2, USB only, stock voltage,
on-chip SPI master. `driver.py` produced `runs.jsonl` and `driver-output.txt`.

| Option | Command | What changes |
|---|---|---|
| LDPC streaming packer | `ldpcpack 1` | For q ≤ 64, parity columns are written straight after an unaligned Kldpc, replacing transpose + OR packing and a `copy_bits` pass |
| Scratch-Y BCH loop | `bchbank 1` | Core 0's front-end BCH runs from a copy in SCRATCH_Y instead of striped SRAM0–3 |

## LDPC stage, `dvbs2 <code> 1 20` (pilots, BCH kernel 2, packed mapper)

| Code | LDPC stage, packer off → on | Whole frame | Frame CRC |
|---|---:|---:|---|
| normal 2/3 | 107,834 → 107,834 (0%) | 270,401 → 270,401 | identical |
| normal 3/4 | 144,444 → 114,667 (−20.6%) | 354,390 → 324,613 (−8.4%) | identical |
| normal 5/6 | 126,800 → 111,314 (−12.2%) | 311,408 → 295,922 (−5.0%) | identical |
| normal 9/10 | 111,004 → 100,422 (−9.5%) | 347,994 → 337,412 (−3.0%) | identical |

Cycles per frame on one core. 2/3 (q = 60) already had its own packer and is unchanged.

## QPSK 5/6 at 16 Msym/s, 60 s each

`dualstream 1 60000 2 <source> 4096 0 0 0 4 8`: four writers, L8 pulse, BCH kernel 2,
packed mapper, SYS128. Every run: no underruns, 0 capture mismatches against the
independent reference.

| Input | bchbank | ldpcpack | Core 0 active | Front-end BCH | Core 1 work | Result |
|---|---:|---:|---:|---:|---:|---|
| GCS | 0 | 0 | 86.3% | 49.1% | 88.3% | clean (baseline) |
| GCS | 1 | 0 | 82.9% | 45.2% | 88.0% | clean |
| GCS | 0 | 1 | 86.9% | 49.4% | 82.3% | clean |
| GCS | 1 | 1 | 83.1% | 45.2% | 82.0% | clean |
| TS | 0 | 1 | 86.7% | 49.2% | 94.2% | clean |
| TS | 1 | 1 | 83.3% | 45.3% | 93.7% | clean |

The baseline matches the earlier record of this configuration (86.0% / 88.2%). The
scratch-Y copy supports the hypothesis that core 0's BCH fetches contended with other
traffic in SRAM0–3; it does not identify which traffic. TS at 16 Msym/s now has about 6%
core-1 margin: one 60 s run per setting, not a soak or a temperature test.

Percentages are the runner's instrumented work/active fractions of wall time, not idle
or power measurements.

## APSK split encoder at 150 MHz

`benchapskfec ... split 1` moves BB scrambling and BCH from core 1 to core 0, in
96-word slices between shaped blocks; `ldpcpack 1` and `bchbank 1` are on. Runner:
the branch's `host/apsk_fec_trial.py --split --ldpc-pack 1 --bch-bank 1`. 3 s frontier
(`apsk*-quick/`), 5/6 with pilots, live on-chip SPI, every capture checked:

| Mode | CPW | Symbol rate | Core 0 work (BCH part) | Core 1 encoder | Result |
|---|---:|---:|---:|---:|---|
| 16APSK, no split (control) | 9 | 4.167 Msym/s | — | 92.1% | clean |
| 16APSK, no split (control) | 8 | 4.6875 Msym/s | — | 99.8% | underrun |
| 16APSK, split | 9 | 4.167 Msym/s | 77.9% (20.1%) | 71.4% | clean |
| 16APSK, split | 8 | 4.6875 Msym/s | 88.1% (22.8%) | 80.2% | clean |
| 32APSK, split | 14 | 2.679 Msym/s | 76.6% (16.1%) | 76.5% | clean |
| 32APSK, split | 13 | 2.885 Msym/s | 82.8% (17.3%) | 81.9% | clean |
| 32APSK, split | 12 | 3.125 Msym/s | 89.7% (18.8%) | 87.8% | clean |

Core 0 work is the runner's `producer_work_busy` (shaping, publication and BCH slices).
The split balances the two cores rather than removing work; at the new rates both run
at 80–90%.

60 s qualification (`apsk16-split-soak/`, `apsk32-split-soak/`, `--expected-git
5e38990269`), every run clean with no underruns and 0 capture mismatches:

| Mode | Clock | CPW | Symbol rate (before → now) | BB capacity, scaled from the recorded rate | Core 0 | Core 1 |
|---|---:|---:|---|---:|---:|---:|
| 16APSK 5/6 | 128 MHz | 8 | 3.556 → 4.000 Msym/s | 12.89 Mb/s | 88.1% | 80.1% |
| 16APSK 5/6 | 150 MHz | 8 | 4.167 → 4.6875 Msym/s | 15.10 Mb/s | 88.1% | 80.1% |
| 32APSK 5/6 | 128 MHz | 12 | 2.286 → 2.667 Msym/s | 10.75 Mb/s | 89.8% | 87.8% |
| 32APSK 5/6 | 150 MHz | 12 | 2.679 → 3.125 Msym/s | 12.60 Mb/s | 89.7% | 87.7% |

BB capacity multiplies the README's recorded capacity at the old rate by the rate ratio
(same MODCOD, pilots and BBHEADER). APSK remains 2 SPS, whose reconstruction images the
Yapogee notes have not qualified; these are digital results only.
