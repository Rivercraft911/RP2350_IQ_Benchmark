# RP2350B + AFE7071 transmitter dev board: requirements draft

Status: **proposal**. No schematic, or any parts chosen. This is just a super preliminary doc as I think through this. 
[`docs/sources/SOURCES.md`](../../docs/sources/SOURCES.md); estimates are marked [EST].

## Gates before layout

| # | gate | status |
|---|---|---|
| G1 | Kernel ≤ 75 % of one core at 8 Msym/s, N = 4, bit-exact | **passed**: 10.80 cyc/sym, 67.8 % streaming |
| G2 | Continuous DMA/PIO output, 0 underruns, pin capture exact | **passed**: 60 s, capture exact |
| G3 | Concurrent host input with G2 still clean | **passed**: real CM5 over PV-SPI at 20 MHz, 210 s |
| G4 | DVB-S2 encode on-chip at the target rate, bit-exact vs reference | **passed**: 8 Msym/s, encoder 80 %, shaper 72 % |
| G5 | Logic-analyzer check of setup/hold and CLK_IO at the header, 64 MW/s | pending (needs equipment) |
| G6 | AFE7071 bring-up on a breakout: spectrum, images, LO leakage after QMC | pending (needs RF bench) |

## Functional blocks

| block | candidate | key constraint |
|---|---|---|
| MCU | RP2350B (QFN-80), 16 MB QSPI flash, optional PSRAM | 48 GPIO |
| TX DAC/modulator | TI AFE7071IRGZ | dual-input clock mode; f_s ≤ 65 MS/s |
| reference | one TCXO (12 or 40 MHz) + 1:4 LVCMOS buffer (e.g. LMK1C1104) | feeds LO synth, DAC clock and RP2350 XIN |
| DAC clock | clock generator, e.g. CDCE6214 integer mode, at 2 f_s = 64 MHz | locked to the shared reference |
| LO | LMX2572LP (1.28 GHz) or LMX2572 (also 2.2–2.3 GHz) + harmonic LPF | AFE LO input −5 to +5 dBm; about +4 dBm nominal |
| PA (IREC) | GRF5613, default-off | `IREC/Pigeon_Vision/research/notes/grf5613_direct_drive_2026-09-27.md` |
| host link | PV-SPI from the CM5 | [`docs/host-link.md`](../../docs/host-link.md) |
| SATS command bus | CAN transceiver + can2040, or CAN FD controller on SPI | open; [`docs/sats-self-contained.md`](../../docs/sats-self-contained.md) |

## AFE7071 facts that constrain the board [AFE7071]

- Interleaved 14-bit I/Q on D[13:0]; f_DAC ≤ 65 MS/s, input word rate ≤ 130 MW/s (p.5).
- Clock modes (pp.17, 25–27):
  - **Dual-input (use this):** DACCLK at 2 f_s; CLK_IO is an input at the word rate. The FIFO
    absorbs ±4 cycles of phase, but DACCLK and CLK_IO must be frequency-locked.
  - Dual-output: the AFE drives CLK_IO. The PIO would have to follow a 64 MHz clock, 2 clk_sys per
    period: too tight.
  - Single DDR: no FIFO, 0 ns setup / 2 ns hold to DACCLK: not practical from the RP2350.
- DACCLKP/N: 0.4–1 V differential, 40–60 % duty, referenced to the 1.8 V clock supply. Whether
  0.4–1 V is peak or peak-to-peak is not stated. TI's EVM feeds LVPECL levels through a
  transformer into 100 Ω [SLOU337A]. Provide AC coupling, 100 Ω termination and a pad footprint.
- CMOS inputs at IOVDD 3.3 V: VIH ≥ 2.3 V, VIL ≤ 1.0 V. RP2350 VOH ≥ 2.62 V, VOL ≤ 0.5 V:
  compatible.
- Serial config port ≤ 10 MHz (t_SCLK ≥ 100 ns).
- Power (p.4, typical): 1.8 V domains about 22–36 mA, 3.3 V domains about 101–102 mA; 334 mW at
  65 MS/s. Use separate low-noise LDOs for the analog rails.
- TI (SLOA313): sideband suppression degrades 15–30 dB with ±1 dB of LO drive change; TI
  recommends the LMX2572LP plus a harmonic filter.
- Parts, 2026-09-29: AFE7071IRGZT $26.55 at 1–99 on ti.com, ACTIVE; stock unknown. The
  AFE707xEVM (SLOU337A) appears discontinued, and it has no LO synthesizer.

## Clocking

One reference for everything. Clock the RP2350 PLL from it so CLK_IO, an integer division of
clk_sys, is locked to DACCLK by construction. An independent DACCLK oscillator would walk the FIFO
pointers off within milliseconds.

- From 40 MHz: REFDIV 5, FBDIV 192, VCO 1536 MHz, ÷6÷2 = 128 MHz; USB 48 MHz from VCO 960 ÷5÷4.
  A non-12 MHz XIN needs OTP boot settings for USB BOOTSEL; a 12 MHz reference avoids that.
  Decision open.
- Jitter is not demanding [EST]: jitter-limited SNR = −20 log10(2π f σ). At 5 MHz baseband and
  σ = 1.7 ps that is 85.5 dB, about ideal 14-bit SNR. A fractional-N generator is adequate.
- LO phase noise [EST]: an in-band floor near −108 dBc/Hz (ADF4351 class) over 100 kHz gives
  ≈ 1.8 mrad rms, negligible for QPSK. Choose the synthesizer by coverage, harmonics, spurs and
  output-power stability instead.

| LO candidate | covers 1.28 / 2.25 GHz | notes |
|---|---|---|
| TI LMX2572LP | yes / no | TI's AFE7070 recommendation; $6.68 at 1k |
| TI LMX2572 | yes / yes | two outputs; H3 ≈ −13 dBc, needs LPF; $21.34 at 1k |
| TI LMX2582 | yes / yes | ~250 mA |
| ADI ADF4351 | yes / yes | common; RFOUTB not independent |

## RP2350B pin budget (draft)

| GPIO | function | notes |
|---|---|---|
| 0–13 | AFE D0–D13 | same as the Pico Plus 2 benchmark |
| 14 | AFE IQ_FLAG | bit 14 of the I word |
| 15 | spare bit / block marker | logic-analyzer frame alignment |
| 16 | AFE CLK_IO | side-set |
| 17–22 | PV-SPI SCK, MOSI, CS_N, MISO, READY | pull-up on CS_N, pull-down on READY (E9 if A2) |
| 23–26 | AFE SPI: SCLK, SDIO, SDENB, RESETB | ≤ 10 MHz |
| 27 | AFE SYNC_SLEEP | FIFO sync after clocks are stable |
| 28 | AFE ALARM_SDO | FIFO pointer alarms |
| 29–32 | LO synth SPI + lock detect | |
| 33–34 | DAC clock generator I2C/SPI | |
| 35–36 | CAN TX/RX (can2040) | SATS variant |
| 37 | PA enable (default off by pull) | never enabled by reset state |
| 40–43 | ADC: rail and temperature telemetry | |
| 44 | status LED | |
| 47 | PSRAM CS | if fitted |

## Parallel-bus timing budget [EST]

At 64 MW/s (2 clk_sys per word), T_sys = 7.8 ns. PIO changes data at t₀ and raises CLK_IO at
t₀ + T_sys.

- setup ≥ T_sys − (bank skew ≤ 2.1 ns) − trace skew = 5.7 ns − trace skew; the AFE needs 1 ns.
- hold ≥ T_sys − 2.1 ns = 5.7 ns; the AFE needs 1 ns.

The 2.1 ns skew and ≤ 4.1 ns clock-to-pad figures are QMI-table values, not PIO-specific. Verify at
G5. Layout intent: D[13:0], IQ_FLAG and CLK_IO matched within about 5 mm, under 5 cm, over solid
ground, with DNP-able 22–33 Ω source resistors.

## Test access

SMA for RF_OUT and an optional external LO. Test points for DACCLK, CLK_IO, IQ_FLAG and
SYNC_SLEEP. A 0.1 in header for D[15:0] + CLK_IO for a logic analyzer. SWD. The AFE's 4-pin SPI
readback for register verification.

## Open

- DACCLK swing definition (peak vs peak-to-peak) and bias; ask TI E2E or measure.
- LO match from a differential synthesizer output to LO_P/N (single-ended plus termination, as on
  the EVM, or a balun), and the harmonic LPF.
- AFE7071 availability.
