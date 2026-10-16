# QPSK 9/10 at 16 Msym/s and the t=8 BCH kernel: device results

**Result:** QPSK 9/10 with pilots runs the complete SPI/FEC pipeline at 16 Msym/s, N4,
GPIO28 DDR 64 MHz: 27.94 Mb/s BB capacity (+8.1% over 5/6). 60 s with TS input passed for
both BCH kernels; the opt-in four-word t=8 kernel (`bchkernel 3`) cuts the BCH stage 36%
and core 0 by 4.7 points live. **One unexplained device hang** occurred in the first 60 s
kernel-3 TS attempt (after the runner's legacy baselines); it did not recur in an
immediate repeat. A longer soak and a stack high-water check are still needed.

Image `e5c9736da8` (branch `rd/c2-qpsk-910`, `flash.txt`, `iqbench.uf2` sha256 in each
manifest). Pico Plus 2, RP2350B A2, on-chip PV-SPI master, SYS128, four CPU-descriptor
writers, L8, `mapkernel 1`, `ldpcpack 1`, `bchbank 1`, bus priority 0.

Stage A/B/A, `dvbs2 10 1 20` (`stage-aba/`, identical CRC 1350531209 in all three):

| BCH kernel | BCH stage | Whole frame |
|---|---:|---:|
| 2 | 147,422 | 331,961 |
| 3 | 94,762 (−35.7%) | 279,301 (−15.9%) |
| 2 | 147,422 | 331,961 |

Live, 9/10 at 16 Msym/s, every capture 0 bus and 0 clock-parity mismatches:

| Run | Input | BCH kernel | Duration | Core 0 | Core 1 | Result |
|---|---|---:|---:|---:|---:|---|
| `target-k3-3s/` | TS | 3 | 3 s | 81.4% | 91.2% | clean |
| `target-k3-3s/` | GCS | 3 | 3 s | 81.4% | 78.7% | clean |
| `target-k3-3s/` controls | — | — | 1 s | — | — | wrong row, starvation, inverted clock: detected |
| `target-k3-60s/` | TS | 3 | — | — | — | **device stopped answering**; USB re-enumerated; needed a power cycle |
| `control-910-k2-60s/` | — | 2 | — | — | — | not run: device still hung |
| `repro-1/` | TS | 3 | 60 s | 81.4% | 91.0% | clean (after power cycle) |
| `repro-1/` | TS | 2 | 60 s | 86.1% | 91.0% | clean |

The hang left no device record; the host saw no reply to the 60 s `dualstream` and every
later command timed out until the board was unplugged. Unknowns: cause (stack overflow into
SCRATCH_Y code is one hypothesis C2 flagged; a USB/host event is another), and its rate.

`control-56-60s/` (5/6 GCS 60 s control, started 18:49:56) has only its manifest and an empty
log: the device stopped answering again. Both hangs were later traced to a core-0 stack overflow
into SCRATCH_Y code that C2's new code exposed; the core-0 stack now lives in SRAM4-7
(`results/round2/2026-10-15/STACK.md`).
