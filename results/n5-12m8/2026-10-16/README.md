# N5 on the Pico: cyclic 8PSK at 5 samples/symbol, 12.8 Msym/s at SYS128

Pico Plus 2, RP2350B A2, SYS128, USB only, experiment image `6f145bef10` (`rd/n5-12m8` on
main `a04e6ba`; `flash-6f145bef10.txt`). `bench5quad8 cpw ms invert prio`: L7/N5 alpha 0.20
pulse, 8-bit samples, 128 KiB two-word row table (CRC 0xa299c7c1), four SRAM-fed DMA chains,
four seven-word PIO writers, GPIO28 DDR clock at 64 MHz. Cyclic 4096-symbol PRBS source; no
CPU work per sample. Every positive run classifies `digital_quad_clean`: full duration, no
output/pointer errors or TXSTALL, all 4096 captured bus words equal to an independent scalar
FIR of the frozen taps, GPIO28 = IQ_FLAG, resources and selectors restored.

| Run | cpw | Rate | Priority 0 | Priority 13 | Supervision |
|---|---:|---:|---|---|---:|
| N4 control, `bench4quad8` (3 s) | 2 / 1 | 8 / 16 Msym/s | clean / clean | clean / clean | 0.75–0.77% |
| N5, 3 s | 2 / 1 | 6.4 / 12.8 Msym/s | clean / clean | clean / clean | 0.75–0.79% |
| N5, inverted DDR clock (3 s) | 2 / 1 | | detected / detected | detected / detected | |
| **N5, 60 s** | 2 / 1 | 6.4 / **12.8 Msym/s** | clean / **clean** | clean / **clean** | 0.76–0.79% |

`stackcheck` afterwards: code intact, core 0 deepest 1712 of 4096 B.

## What it means

At 12.8 Msym/s each core has 443,880 cycles per alternate FEC frame instead of 355,104. The
measured two-core 8PSK work (C8/C9: 5/6 about 304–318k, 3/4 about 312–328k per frame) fits that
budget with 25–30% to spare, before supervision. A complete 8PSK 5/6 transmitter at 12.8 Msym/s
would carry 31.0 Mb/s; it needs live producers for N5 (the four B6 producers shift
4-byte rows; N5 rows are 8 bytes) and the B9 schedule at a 1.25x frame period. Not built yet.

## Limits

The pulse passes the held-DAC Annex A mask model by +0.027 dB on the upper side
(`docs/rd/n5-model.json`): a model gate, not analog margin. A 6-bit one-word row fails the
mask. Cyclic source only; no AFE, RF or receiver. The first sampling image's lower edge moves to about 56.3 MHz
(64 MSPS minus half of the 15.36 MHz occupied bandwidth); the analog filter plan has not been checked
against it.
