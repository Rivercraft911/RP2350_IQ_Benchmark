# Q1 on the Pico: IREC QPSK 2/3 on the PIO pointer engine, and TS input at 16 Msym/s with core 1 at 71%

Pico Plus 2, RP2350B A2, SYS128, USB only, experiment image `0a4e1893db` (Q1 merged with main
`1b52c48`; `flash-0a4e1893db.txt`). On-chip PV-SPI source, `dualptr` PIO pointer engine,
QPSK, L8 pulse, GPIO28 DAC clock. Every positive row classifies `digital_live_clean`: full
duration, no output/pointer/input faults, all 4096 snapshots equal to the independent reference,
GPIO28 = IQ_FLAG, poll gap at most 256 µs, credit runway at least 1360 words, BBFRAME and SPI CRCs
matching.

## A. TS adapter alone, `pvbench 64 k` (`pvbench/`)

Cycles per BBFRAME, 2/3 / 9/10. Output CRC [656557369, 408789036] for both kernels.

| Adapter | 2/3 | 9/10 |
|---|---:|---:|
| Existing CRC-8 + copy + byte swap | 41,456 | 57,905 |
| Kernel 1, fused leaf in SRAM | **29,043** | **42,768** |
| Kernel 2, same in C from flash | 32,692 | 49,681 |

## B. Pigeon Vision profile, 2/3 at 8 Msym/s (`pv23-*`)

| Input | Engine | Core 0 / core 1, 60 s, priority 0 | Priority 13 |
|---|---|---:|---:|
| TS | `dualptr` | **21.4% / 43.2%** | 21.1% / 43.9% |
| GCS | `dualptr` | 21.4% / 38.7% | 21.1% / 39.2% |
| TS | CPU descriptor control (`dualstream`) | 43.2% / 43.1% | |

Today's `pvflight` path measured 78.8% / 66% for this profile. The 3 s screen also detected
grant starvation and an inverted DAC clock for both inputs.

## C. TS at 16 Msym/s, 9/10, priority 13, 60 s each (`ts16-*`)

Run in the order k0, k1, k3, k2, k0:

| Adapter | Core 0 | Core 1 |
|---|---:|---:|
| k0, existing (first and last run) | 45.7% | 95.9% / 95.9% |
| k1, fused leaf on core 1 | 45.7% | **89.2%** |
| k2, fused C from flash | 45.7% | 89.3% |
| k3, fused adapter on core 0 in 4-packet steps | 63.8% | **70.9%** |
| GCS input reference (no TS adapter) | 45.7% | 81.9% |

Round 2 measured k0 at 97.0%; this image gives 95.9%. With k3, TS input costs core 1 less than
GCS input does, because GCS adaptation still runs on core 1. k3's largest ingress step is 7371
cycles (58 µs) and the poll gap stays at 243–249 µs.

Inverted clock and grant starvation were detected for k1 and k3 (`ts16-neg-k1`, `ts16-neg-k3`).

## Runner fix

`ts16-3s-k3` and `ts16-3-k3` were classified failed with `core-0 TS ingress` although every
other check passed: the runner and its test fixture read `ts_busy` inside `pv_spi`, while the
firmware prints it beside `pv_spi`. Fixed in the runner; `ts16-3s-k3-fixed` and `ts16-6-k3`
are the reruns on the same image.

`stackcheck` after the runs: code intact, core 0 deepest 2368 of 4096 B, core 1 468 of 2048 B.
Digital results only; no AFE or RF.
