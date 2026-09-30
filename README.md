# RP2350 I/Q waveform benchmark

Can an RP2350 replace the FPGA that generates pulse-shaped QPSK for a TI AFE7071 transmitter?
It would serve the IREC PigeonVision video downlink (8 Msym/s) or the SATS image downlink
(≤ 1 Msym/s).

**Digital result so far: yes on the benchmark board, with margin.** On a Pimoroni Pico Plus 2
(RP2350B rev A2, stock clocks), one core generates 8 Msym/s QPSK at 4 samples/symbol with
RRC α = 0.20. It streams continuously through DMA and PIO onto a 16-bit bus at 64 M words/s
while receiving the 16 Mb/s input over a PIO link, using 69 % of one 128 MHz core. Output at
the pins is bit-exact with the Python model. This says nothing yet about RF spectrum, EVM
after the AFE, clock jitter, or link closure. The AFE7071, clocks and LO are not built.

## Measured (Pico Plus 2, 128 MHz, SDK 2.2.0, GCC 14.2)

| test | result |
|---|---|
| LUT kernel, N = 4 samples/symbol, L = 10 (`lut_asm`) | **11.05 cycles/symbol** = 11.6 Msym/s per core; CRC-exact |
| same, N = 2 (`lut_pair`) | 7.0 cycles/symbol |
| same, N = 8 (`lut_asm`, `iqasm.S`) | 20.9 cycles/symbol |
| stream 8 Msym/s, N = 4, 1 core, 4-lane input link | 69.2 % busy, 0 underruns, 0 TX stalls, 8 176 bus words captured at the pins with 0 mismatches |
| same, 2 cores alternating blocks | 34.8 % + 36.4 % |
| soak: 20 s, L = 12 | 156 249 blocks, 0 faults, 32 752 words exact |
| stream at 150 MHz, PIO limit (2 clocks/word = 75 MW/s) | 9.375 Msym/s, 72.5 % busy, clean |
| SATS rate: 1 Msym/s, N = 8, 1-lane link | 16.6 % busy, clean |
| input link, 1 lane × 10.7 MHz (below the 16 Mb/s need) | **fails as expected**: 7 884 underruns flagged |

Optimization steps at N = 4, L = 10, all bit-exact (`results/plots/progress_kernels.png`):

| step | cycles/symbol | change |
|---|---|---|
| v0 `conv`: direct convolution | 917.7 | – |
| v1 `lut_shift`: table, shift-register history | 58.6 | table lookup replaces L·N MACs |
| v2 `lut_win`: 16-symbol windows, UBFX, compiled C | 29.1 | GCC spills and packs with UXTH/ORR |
| v3 `lut_pair`: PIO does the I/Q interleave | 13.8 | CPU stores table words unmodified |
| v4 `lut_asm`: hand-written Thumb-2 | 11.55 | 8 instructions/symbol; predicted 11.5 |
| v4 + placement: tables SRAM4–7, ring SRAM0–3, code SRAM8 | 11.05 kernel; stream busy 73.3 → 69.2 % | bank collisions with DMA removed |
| v5 `lut_asm_p`: software-pipelined `.S` | **10.80**; stream busy 67.8 % | next UBFX hides load-use stall; floor ≈ 10 |

Every run is appended to `results/optimization-log.jsonl` (git revision, clock, parameters,
verification). `make plots` regenerates the figures.

## Findings that change the plan

1. **2 samples/symbol is not usable with the AFE7071.** Nothing filters between its DAC and
   modulator except a ZOH sinc and a 4th-order filter whose widest setting is 18 dB down only at
   20 MHz. At 16 MS/s the modelled images are −19 dBc and ACLR −25 dB. At 32 MS/s they are
   −49 dBc (`docs/derivations.md` §2). This doubles the bus rate to 64 MW/s.
2. **Load scales with the DAC rate, not the symbol rate**, at about 2.4–2.8 cycles per complex
   sample. Low SATS symbol rates need the narrow filter (tune 8) and N = 8–32 to keep images down
   (`docs/sats-self-contained.md` §1).
3. **Memory placement matters more than compiler flags.** The RP2350 has no data cache on SRAM;
   the SRAM banks are the shared resource. Keep instruction fetch off the banks the DMA reads.
4. Input: RP2350 USB (≤ 9.7 Mb/s) and the hardware SPI slave (≤ 12.5 Mb/s) cannot carry
   16 Mb/s. A PIO receiver with READY flow control can
   (`docs/host-link-and-devboard-research.md`).

## Estimates, not measured

- DVB-S2 BCH + LDPC + PL framing on-chip: ≈ 0.2 M cycles per normal frame, which is ≈ 5 % of a
  core at 1 Msym/s and ≈ 38 % at 8 Msym/s (`docs/sats-self-contained.md` §2). The reference
  encoder is in progress in `reference/dvbs2/`.
- Parallel-bus timing margin ≈ 5.7 ns setup and hold at 64 MW/s against 1 ns required, from
  QMI-table pad data rather than a PIO figure (`hardware/devboard/README.md`).

## Resources (current firmware)

SRAM: 22 KB .data (hot code copied to RAM) + 184 KB .bss + 128 KB SRAM4–7 (tables, capture)
+ 1.2 KB SRAM8, out of 520 KB. PIO0 uses SM0 (output) and SM1 (capture, verification only);
PIO1 uses SM0 (link receiver) and SM1 (host emulator, test only). DMA uses 2 channels for output,
1 for capture and 2 for the link. GPIO0–16 carry the AFE bus (D0–13, IQ_FLAG, spare, CLK_IO);
GPIO17–22 the input link.

## Reproduce

```sh
make test          # host-native kernels vs Python model, bit-exact
make analyze       # filter/image analysis -> results/plots
make build flash   # needs ~/.pico-sdk (VS Code extension install); board in BOOTSEL or running iqbench
make bench         # kernel sweep on the board, appended to the log
python3 host/iqbench.py stream lut_asm 4 10 --cores 0 --cpw 2 --ms 3000 --cap 4096 --lanes 4
make plots
```

`python3 host/iqbench.py -h` lists the commands. The firmware speaks line commands over USB CDC
(`firmware/src/main.c` header).

## Layout

```
reference/   Python model (iqlut.py), filter analysis, coefficient generator, dvbs2/ (in progress)
firmware/    Pico SDK project: kernels (iqgen.c, iqasm.S), PIO/DMA output (iqout.c), input link
host/        board driver + verification (iqbench.py), native test, progress plots
results/     optimization log (jsonl), reference analysis, plots
docs/        derivations, board/RP2350/host-link research, SATS feasibility; sources/ (not tracked)
hardware/    dev-board requirements draft and gates
```

## Next steps

1. DVB-S2 encoder in firmware (BCH, LDPC group form, PL framing), bit-exact against the
   reference, then streamed through the shaper at 1 and 8 Msym/s.
2. Real input: a Pi 5/CM5 RP1-PIO or SPI master into GPIO17–22, with external pulls (E9 on A2).
3. Logic analyzer on GPIO0–16 at 64 MW/s: setup/hold, skew, CLK_IO duty.
4. AFE7071 breakout with a locked DACCLK and an external LO: spectrum, images, QMC calibration.
5. Kernel: 2-word unrolling would remove about half the remaining 0.8 cycles/symbol of loop overhead (≈ 4 %).
