# Live 8PSK at 16 Msym/s, SYS128: runtime data, PIO-computed table addresses

**Result.** Four alternate-parity PIO producers turn packed 8PSK axis codes into table-row
addresses, four independent control/sample DMA chains fetch one-word 8-bit rows, and B5's four
OUT8 writers plus the GPIO28 DDR clock put them on the pins. With **new symbols generated at
runtime** (`bench4prod8live`, non-repeating xorshift 8PSK stream, finite 512-word input DMA
grants), 6 of 6 60-s runs at **16 Msym/s** and 6 of 6 at 8 Msym/s were clean; the cyclic variant
was 12 of 12. At 16 Msym/s core 0 spent 19.35% generating codes (1.55 cycles/symbol) and 3.5%
supervising; core 1 is reserved for grant re-arming (it spins; observed worst round 298 cycles).
No CPU computes an address or a sample. No overclock.

Pico Plus 2, RP2350B A2, USB only, image `76f12ca956` (`rd/b6-pio-producers`, experiment build,
`CORE0_STACK_SRAM_HI=ON`; `flash.txt`). Validation (`host/iq4prod_trial.py`): 4096 stride-17
GPIO0..31 snapshots against an independent scalar 8-bit FIR of the same symbol stream (for live
runs the host regenerates the runtime PRBS), GPIO28 against IQ_FLAG, grant accounting.
Design, budgets and ownership proof: [docs/rd/b6-pio-producers.md](../../../docs/rd/b6-pio-producers.md).

| Run set | Source | Rate | Priority | Duration | Result |
|---|---|---:|---:|---:|---|
| `cyclic-short/` | immutable rings | 8, 16 Msym/s | 0, 13 | 3 s | 4/4 clean |
| `cyclic-inverted/` | immutable | 8, 16 | 0, 13 | 3 s | 4/4 inversion detected, data exact |
| `live-short/` | runtime | 8, 16 | 0, 13 | 3 s | 4/4 clean, 4/4 inversion detected |
| `live-starved/` | runtime, publication stopped at 100 ms | 8, 16 | 0, 13 | — | 4/4 `SOURCE_EMPTY` at 103.95–107.54 ms, data exact up to the stop |
| `live-60s/` | runtime | **16 Msym/s** | 0, 13 | 60 s x 3 each | **6/6 clean** |
| `live-60s/` | runtime | 8 Msym/s | 0, 13 | 60 s x 3 each | 6/6 clean |
| `cyclic-60s/` | immutable | 16 and 8 Msym/s | 0, 13 | 60 s x 3 each | 12/12 clean |

Each 16-Msym/s live run granted 60,000,256 code words per producer (16 symbols per word,
960 M symbols per axis in 60 s, as expected).

## Against the loop-latency prediction

`results/dmaloop/2026-10-15` (branch `rd/stall-dmaloop`) measured four PIO-pointer chains with
one-word rows at 14.0 cycles per row against a 16-cycle deadline and called this design
marginal. It ran 24/24 clean. Two cycles of margin plus the 8-bit rows' 128–144-cycle writer
FIFO runway was enough here; B2's one cycle with 14-bit rows (about 68 cycles of runway) was
not. Twelve 60-s runs are still not a soak.

## Not established

FEC or real ingress feeding the producers (C3 measures CPU fit separately), freeing core 1 from
grant service, AFE pad timing, spectrum (the 8-bit table's modeled upper-mask margin is
+0.026 dB), EVM, receiver decode, temperature and power.
