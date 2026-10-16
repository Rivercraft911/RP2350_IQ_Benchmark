# R&D round 1 (2026-10-15)

Three Codex agents worked from `integration/yapogee-2026-10-15` (`8014ed3`); the
coordinating session ran every device measurement. Reports are the agents' own, with
device results added in each branch's `results/` folder.

| Branch | Work | Device result | Here |
|---|---|---|---|
| `rd/a1-qpsk-hwshape` | `dualptr`: live SPI/FEC feeding the PIO QPSK pointer engine; even/odd FEC ownership model | 8 Msym/s clean 60 s, core 0 47.2% → 25.7%; 16 Msym/s stalls the two writers within 3–9 ms ([results](../../results/dualptr/2026-10-15/README.md)) | [report](a1-qpsk-hwshape.md), merged |
| `rd/b1-l7-4level` | Four-level L7/N4 full-history table with PIO keys for QPSK/8PSK/VCM | cyclic 8PSK 8 Msym/s N4 clean 60 s at 1.8% supervision ([results](../../results/b1-four-level/2026-10-15/README.md)) | [report](b1-l7-4level.md), merged (`IQ4HIST_EXPERIMENT=ON`) |
| `rd/c1-fec-throughput` | `ldpcxor`: scheduled LDPC row updates in SCRATCH_Y | 3.9–5.9k cycles/frame faster alone, but +11.0 / +5.5 points live (core 0/1); placement test supports SCRATCH_Y fetch contention | not merged; see that branch's `results/ldpcxor/2026-10-15/README.md` |

The [frontier model](../frontier/frontier.md) bounds what these paths can reach.
