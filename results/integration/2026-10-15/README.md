# Integration check: DDR clock + LDPC packer + scratch-Y BCH + APSK split

One image from `integration/yapogee-2026-10-15` at `8014ed33b9` (clean tree; UF2 sha256
`9e0c6daa…974d`, `flash.txt`) combines the GPIO28 DDR clock, the LDPC streaming packer,
the scratch-Y BCH copy and the APSK split encoder. Placement differs from each separately
measured image, so the key points were re-run. Pico Plus 2, RP2350B A2, USB only, on-chip
SPI master. `driver.py` → `runs.jsonl`; APSK via `host/apsk_fec_trial.py` → `apsk-split-quick/`.

| Run | Duration | Core 0 | Core 1 | Bus / clock parity | Result |
|---|---:|---:|---:|---|---|
| QPSK 5/6 16 Msym/s, GCS, DDR 64 MHz, pack+bank | 60 s | 82.9% | 81.8% | 0 / 4096 of 4096 | clean |
| QPSK 5/6 16 Msym/s, TS, DDR 64 MHz, pack+bank | 60 s | 83.0% | 93.5% | 0 / 4096 of 4096 | clean |
| Inverted DDR clock control, 8 Msym/s PRBS | 1 s | — | — | 0 / 0 of 4096 | detected |
| 16APSK 5/6 split, 150 MHz, CPW 8 (4.6875 Msym/s) | 3 s | 87.9% | 80.2% | 0 mismatches | clean |
| 32APSK 5/6 split, 150 MHz, CPW 12 (3.125 Msym/s) | 3 s | 89.8% | 88.0% | 0 mismatches | clean |
| 32APSK 5/6 split, 150 MHz, CPW 8 (probe beyond frontier) | 3 s | 89.7% | 99.2% | — | underrun, expected |

The combined image matches the separate measurements within 0.5 points. Digital only.
