# N5 follow-up (2026-10-16)

| Branch | Work | Status | Here |
|---|---|---|---|
| `rd/n5-12m8` | L7/N5 pulse, padded 8-bit rows and four SRAM-fed writers at calculated 12.8/6.4 Msym/s | 60 s clean at 12.8 and 6.4 Msym/s, priorities 0 and 13, 0.8% supervision; mask model margin +0.027 dB ([results](../../results/n5-12m8/2026-10-16/README.md)) | [report](n5-12m8.md), [model](n5-model.json) |

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

# R&D round 2 (2026-10-15)

Merged on `rd/round2-merge` with the core-0 stack moved out of SCRATCH_Y
([round-2 results](../../results/round2/2026-10-15/README.md), [stack fix](../../results/round2/2026-10-15/STACK.md)).

| Branch | Work | Device result | Here |
|---|---|---|---|
| `rd/b2-8psk-live` | `bench4fec`: live GCS 8PSK 5/6 FEC through the four-level table; DMA read bus priority | 8 Msym/s clean 60 s on its own image; stalls at 40–51 s on the merged image ([results](../../results/b2-8psk-live/2026-10-15/README.md)) | merged |
| `rd/c2-qpsk-910` | QPSK 9/10 and the t=8 BCH kernel | 27.94 Mb/s at 16 Msym/s, 60 s clean, CPU and hardware paths | [report](c2-qpsk-910.md), merged |
| `rd/a2-quad-pointers` | `dualptr4`: four pointer-fed writers via a router | 8 Msym/s clean; 16 Msym/s stalls when capture starts | [report](a2-quad-pointers.md), merged |

# R&D round 3 (2026-10-15): 8PSK at 16 Msym/s at SYS128

Merged on `rd/round3`. Every device run is at the rated 128 MHz system clock.

| Branch | Work | Device result | Here |
|---|---|---|---|
| `rd/b3-8psk-rows` | Regression study of the B2 stall; four-writer router mode for `bench4fec` | not run; superseded by B5/B6 and explained by `dmaloop` | [report](b3-8psk-rows.md), [regression](b3-regression.md); code not merged |
| `rd/b4-8bit-rows` | 64 KiB 8-bit one-word rows (held-DAC mask margin +0.026 dB), seven-instruction pointer SM, OUT8 writers | 8 Msym/s clean; one chain per axis stalls in ~20 us at 16 Msym/s ([results](../../results/b4-8bit-rows/2026-10-15/README.md)) | [report](b4-8bit-rows.md), merged |
| `rd/b5-quad-rows` | Four independent DMA chains (two per axis), four writers, SRAM pointers, cyclic source | **16 Msym/s 8-bit: 12/12 60-s runs clean**; 14-bit 5/6 ([results](../../results/b5-quad-rows/2026-10-15/README.md)) | [report](b5-quad-rows.md), merged |
| `rd/b6-pio-producers` | Four alternate-parity PIO address producers; runtime 8PSK source with finite DMA grants | **live runtime data at 16 Msym/s: 6/6 60-s runs clean**, core 0 19% + 3.5% ([results](../../results/b6-pio-producers/2026-10-15/README.md)) | [report](b6-pio-producers.md), merged |
| `rd/stall-dmaloop` | `dmaloop`: measured DMA row-loop latency, 10–18 SYS cycles | explains every one-symbol-row stall ([results](../../results/dmaloop/2026-10-15/README.md)) | merged |
| `rd/c3-8psk-fec16` | Frame-parallel two-core 8PSK FEC into packed code rings (`c3fec`) | fits 8 Msym/s (61–80% per core); 13–39% over budget at 16 Msym/s ([results](../../results/c3-8psk-fec16/2026-10-15/README.md)) | [report](c3-8psk-fec16.md), merged |
| `rd/b7-live8` | `bench4fec ... 8`: live on-chip GCS -> BCH/LDPC -> 8PSK -> 8-bit hardware shaping | **8 Msym/s complete transmitter: 6/6 60-s runs clean** at priority 13 and 0; 14-bit stalls in 4.4 ms at priority 0 ([results](../../results/b7-live8/2026-10-15/README.md)) | merged |
| `rd/r2-hw-offload` | Survey of every RP2350 block against every non-shaping stage; DMA-only grant reloads; Hazard3 `zip` mapper prototype | no block offloads BCH/LDPC; self-re-arming grants 24/24 exact on the Pico; Hazard3 estimate 27k cycles/frame, not built (no RISC-V toolchain) ([results](../../results/r2-hw-offload/2026-10-15/README.md)) | [report](r2-hw-offload.md), merged |
| `rd/c4-fec-speed` | Selectable FEC speedups: SRAM LDPC kernels, table mapper, streamed BCH, fused GCS scrambling, cached deadlines | all on: 2/3 364k cycles/frame (−11–14%), 8 Msym/s at 54–63% per core; 16 Msym/s still 3–12% over and misses its first frame ([results](../../results/c4-fec-speed/2026-10-15/README.md)) | [report](c4-fec-speed.md), merged |
| `rd/ldpc-banks` | `c3pair`: per-stage FEC cycles, one core vs both, shared vs split SRAM banks, core-1 hammers | SCRATCH_Y BCH code is the big two-core conflict (BCH 84k -> 152k); with it in striped SRAM both cores cost +8–15%; moving buffers or code to SRAM4-7 saves only 3–8k; single-core 2/3 is 356k vs the 355k 16 Msym/s budget ([results](../../results/ldpc-banks/2026-10-15/README.md)) | merged |

# FEC follow-up (2026-10-16)

| Branch | Work | Result | Here |
|---|---|---|---|
| `rd/c5-ldpc` | C5 LDPC bundle, bit 32 in C8 | Measured 89.3k concurrent LDPC cycles at 2/3 ([results](../../results/c5-ldpc/2026-10-16/README.md)) | [report](c5-ldpc.md), opt-in |
| `rd/c6-map8` | Packed 8PSK mapper, bit 64 in C8 (32 on original branch); layout 3 code-placement diagnostic | Measured 67.7k map cycles alone, 72.7k together at 2/3 ([results](../../results/c6-map8/2026-10-16/README.md)) | [report](c6-map8.md), opt-in |
| `rd/c7-bch-place` | Private BCH/transpose, LDM and three-table BCH; bits 128/256/512/1024 and layout 4 in C8 | Measured 67.2k concurrent three-table BCH cycles at 2/3 on the original C7 image ([results](../../results/c7-bch-place/2026-10-16/README.md)) | [report](c7-bch-place.md), historical bit/layout names |
| `rd/c8-fec-combined` | C5+C6+C7 merge, external Gold arena, final mask 2047 and context-specific `best` | 8PSK 2/3 FEC at the 16 Msym/s cadence, 60 s, 43,254/43,254 frames on time, cores 83%/79% busy; 5/6 and 3/4 miss the first frame; `best` both cores 261–306k per frame ([results](../../results/c8-fec-combined/2026-10-16/README.md)) | [report](c8-fec-combined.md), opt-in |
| `rd/b9-8psk16` | Complete memory-GCS 8PSK 2/3 through C8 two-core FEC and B6 four-producer OUT8 waveform path; finite IRQ grants | **Complete on-chip 8PSK 2/3 at 16 Msym/s (30.97 Mb/s), 60 s clean** at priorities 0 and 13; core 0 87–90%, core 1 80–85% ([results](../../results/b9-8psk16/2026-10-16/README.md)) | [report](b9-8psk16.md), opt-in |
| `rd/q1-qpsk` (agent branch) | IREC QPSK 2/3 on `dualptr`; fused TS CRC-8/copy adapter (`pvkernel` 1/2/3) | 2/3 8 Msym/s TS 21.4% / 43.2%; 16 Msym/s 9/10 TS core 1 95.9% -> 70.9% with k3 ([results](../../results/q1-qpsk/2026-10-16/README.md)) | [report](q1-qpsk.md), opt-in |
| `rd/a3-apsk` (agent branch) | Table-driven 16/32APSK PL mapper (option 1), multiply-form 32APSK conversion (option 2) | 60 s clean: 16APSK cpw 7 4.571 Msym/s 14.73 Mb/s, 32APSK cpw 9 3.556 Msym/s 14.33 Mb/s ([results](../../results/a3-apsk/2026-10-16/README.md)) | [report](a3-apsk.md), opt-in |
| `rd/ingress-wide` | Source-clocked four-bit PV message receiver (`pvwide`) with on-chip sender | 60 s clean at 30.5 / 40.2 / 47.8 Mb/s, 76.9 Mb/s 3 s; internal sender only ([results](../../results/ingress-wide/2026-10-16/README.md)) | [report](ingress-wide.md), opt-in |
