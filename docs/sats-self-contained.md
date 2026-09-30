# SATS: self-contained RP2350 downlink (feasibility)

User request (2026-09-29): payload data arrives over CAN, or directly over SPI, is stored in
RP2350-side memory, and is downlinked with a standard protocol such as DVB-S2, all on the
RP2350. Status: **analysis and estimates only**, except where a line cites a measurement. SATS
rates come from `../SATS/Next Satellite/research/mcu-qpsk-feasibility.json`: 0.25–1 Mb/s
information, 5 MB images, 100 images/day, 600 s passes. These are illustrative assumptions, not
requirements.

## Pipeline

```
payload ──SPI (PIO slave, DMA)──► RP2350 SRAM/PSRAM file store ──► DVB-S2 BBFRAME/BCH/LDPC/PL
   └──CAN (commands, can2040 or CAN FD controller)                    framing (core 1)
                                        LUT pulse shaper (core 0) ──► DMA ──► PIO ──► AFE7071
```

## 1. Pulse-shaping cost is set by the DAC rate, not the symbol rate

The kernel moves about 2 words and does about 2.4 cycles of work per complex output sample
(measured 11.55 cycles/symbol at N = 4). CPU load ≈ 2.4 f_s / f_clk. The DAC rate f_s must place
the first image, at f_s − R_s(1+α)/2, where the AFE7071 filter and the ZOH sinc reject it:

| R_s | filter tune | f_s | N | image edge | ZOH + filter (typ.) | load at 128 MHz |
|---|---|---|---|---|---|---|
| 8 Msym/s (IREC) | 0 | 32 MS/s | 4 | 27.2 MHz | 15 + 29 = 44 dB at the edge; worst image PSD −49 dBc | 72 % measured |
| 1 Msym/s (SATS) | 8 | 8 MS/s | 8 | 7.4 MHz | 22 + 32 = 54 dB | 16.6 % measured (streaming, link on) |
| 1 Msym/s | 0 | 4 MS/s | 4 | 3.4 MHz | 15 + 0 = 15 dB | inadequate |
| 0.25 Msym/s | 8 | 4 MS/s | 16 | 3.85 MHz | 28 + 12 = 40 dB | marginal; N = 32 at 8 MS/s gives 68 dB |

Values are ZOH + filter attenuation at the image's inner edge, computed with `reference/iqlut.py` (`afe_filter_db`). The tune-8 figures use the datasheet's typical points (1 dB at 2.5 MHz, 18 dB at 5 MHz, 42 dB at
10 MHz). Intermediate tunes are not tabulated. Low rates therefore favour tune 8 with f_s ≈ 8 MS/s,
and the kernel must support N = 8–32. For N ≥ 8 the entry per axis is ≥ 16 bytes, so use LDM from
the computed address and one STM with interleaved register numbers (I in odd, Q in even
registers). Predicted ≈ 23 cycles/symbol at N = 8; measured 20.9 (`lut_asm`, `firmware/src/iqasm.S`).

## 2. DVB-S2 encoding on the RP2350

Frame: normal QPSK 1/2 with pilots is 33 282 symbols carrying 32 128 data-field bits (0.965 bit
per symbol). At 1 Msym/s that is 0.965 Mb/s and 33.3 ms per frame; at 8 Msym/s, 4.16 ms.

| step | method | estimate per normal frame |
|---|---|---|
| BB header, CRC-8, BB scrambling | table CRC; scrambling XOR with a precomputed 32-bit-word PRBS | < 5 k cycles |
| BCH (t = 12, 192 parity bits) | byte-wise LFSR, 256 × 24 B table; ≈ 25 instr/byte over 4 026 bytes | ≈ 100 k |
| LDPC (IRA) | 360-bit group form: each table entry XORs a rotated 360-bit vector (12 words) into one of q rows; about 450–480 entries × ≈ 70 cycles; then transpose + prefix-XOR accumulation (≈ 25 k) | ≈ 60 k |
| QPSK map, PLHEADER, pilots | bit-plane copy | < 5 k |
| PL scrambling | fixed Gold sequence (n = 0) precomputed as bit planes. Rotation by k·90° becomes swap/invert of (bI, bQ): k=1 → (¬bQ, bI), k=2 → (¬bI, ¬bQ), k=3 → (bQ, ¬bI); ≈ 8 ops / 32 symbols | ≈ 10 k |
| **total** | | **≈ 0.17–0.2 M cycles** |

Load: 0.2 M / 33.3 ms = 6 Mcycles/s ≈ 5 % of a core at 1 Msym/s, and ≈ 38 % at 8 Msym/s. An
IREC-rate full DVB-S2 transmitter needs both cores: shaping on one (≈ 75 %) and FEC on the other.
Nothing here is benchmarked yet. A verified Python DVB-S2 reference is being built in
`reference/dvbs2/` to test a firmware encoder bit-exactly.

## 3. Ingest

| link | raw | payload goodput (est.) | 5 MB image | notes |
|---|---|---|---|---|
| classic CAN 1 Mb/s (can2040, PIO) | 1 Mb/s | 0.47–0.58 Mb/s × bus share | ≥ 70–85 s | 64 data bits per 111–135-bit frame incl. stuffing |
| CAN FD 0.5/2 Mb/s, 64 B frames | – | ≈ 1.5 Mb/s | ≈ 27 s | RP2350 has no CAN FD: needs e.g. MCP2518FD on SPI |
| direct SPI, PIO slave, 20 MHz | 20 Mb/s | ≈ 18 Mb/s | ≈ 2.2 s | point-to-point, READY flow control; external pulls (E9 on A2) |

Recommendation (proposal): CAN for commands and housekeeping, point-to-point SPI for bulk data.
The RP2350 PL022 SPI slave is limited to 12.5 Mb/s; a PIO receiver has no clock-ratio limit
below about f_clk/6.

## 4. Storage

A 600 s pass at 0.965 Mb/s carries 72 MB. The Pico Plus 2's 8 MB PSRAM holds one pass's worth
only if the payload keeps the archive and streams files on demand (payload as file server, TT&C
as modem). If TT&C must own the store, the board needs NAND/NOR (hundreds of MB). PSRAM through
QMI and the 16 KB XIP cache far exceeds the 0.12 MB/s drain rate. Its bandwidth matters only for
the IREC rate (2 MB/s), where it should still be adequate but must be measured.

## 5. Protocol

Fixed MODCOD (CCM) DVB-S2, one-way, received with an SDR and gr-dvbs2rx. Files are carried as
numbered segments with CRC32 inside BBFRAMEs (GSE is the standard encapsulation). Missing
segments are recovered by request over the LoRa command link, or by packet-level erasure coding
across frames. The segmentation and retransmission design is open.

## Staged tests to add

1. DVB-S2 reference (Python) verified against an independent implementation. *(in progress)*
2. Firmware BCH, LDPC and PL framing kernels: cycle counts plus bit-exact CRC against the reference.
3. Stream a DVB-S2 PLFRAME sequence through the existing shaper and PIO at 1 and 8 Msym/s.
4. PIO SPI slave with DMA into the file store, with loopback emulation on-chip (GPIO17–22), then a
   real payload master.
5. can2040 command path concurrently; measure CPU and latency impact.
6. Ground decode: capture I/Q samples (or AFE output with an SDR later) and decode with gr-dvbs2rx.
