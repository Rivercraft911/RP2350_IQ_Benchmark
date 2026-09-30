# RP2350B + AFE7071 transmitter dev board: requirements draft

Status: **proposal**. No schematic, parts are candidates only, nothing ordered. Start the KiCad
project only after the gates below pass on the Pico Plus 2. Facts cite `../../docs/`; estimates are
marked.

## Gates before layout

| # | gate | status |
|---|---|---|
| G1 | Kernel ≤ 75 % of one core at 8 Msym/s, N = 4, bit-exact | **passed**: 11.55 cyc/sym, 72 % (log `lut_asm`) |
| G2 | Continuous DMA/PIO output, 0 underruns, pin capture exact | **passed**: 20 s, 156 249 blocks |
| G3 | Concurrent 16 Mb/s input (PIO receiver + DMA) with G2 still clean | **passed** (on-chip loopback emulator; real host pending) |
| G4 | DVB-S2 encode on-chip at the target rate, bit-exact vs reference | **passed**: 8 Msym/s, 2 cores at 70/72 %, 60 s |
| G5 | Logic-analyzer check of setup/hold and CLK_IO at the header, 64 MW/s | pending (needs equipment) |
| G6 | AFE7071 bring-up on a breakout: spectrum, images, LO leakage after QMC | pending (needs RF bench) |

## Functional blocks

| block | candidate | key constraint |
|---|---|---|
| MCU | RP2350B (QFN-80), 16 MB QSPI flash, optional PSRAM | same family as Samwise; 48 GPIO |
| TX DAC/modulator | TI AFE7071IRGZ | dual-input clock mode; IOVDD 1.71–3.6 V; f_s ≤ 65 MS/s |
| reference | one TCXO, 12 or 40 MHz, 1:4 buffer | feeds LO synth, DAC clock and RP2350 XIN (≤ 50 MHz) |
| DAC clock | synth/clock generator (e.g. CDCE6214) at 2 f_s = 64 MHz | DACCLKP/N differential 0.4–1 V, 40–60 % duty; peak vs p-p ambiguous in SLOS789C p.5 |
| LO | LMX2572 (1.28 GHz and 2.2–2.3 GHz) + harmonic filter | AFE LO input −5 to +5 dBm; TI SLOA313 suggests about +4 dBm |
| PA (IREC) | GRF5613, default-off | see `../../../IREC/Pigeon_Vision/research/notes/grf5613_direct_drive_2026-09-27.md` |
| host link | 4-lane PIO bus + READY from Pi 5/CM5 RP1 PIO; SPI fallback | see `host-link-and-devboard-research.md` |
| SATS command bus | CAN transceiver + can2040, or CAN FD controller on SPI | open; see `sats-self-contained.md` |

Clocking. The AFE requires CLK_IO and DACCLK to be frequency-locked (p.25), and its FIFO absorbs
±4 cycles of phase. Clock the RP2350 PLL from the shared reference so CLK_IO, an integer division
of clk_sys, is locked by construction. 40 MHz → refdiv 5, fbdiv 192, VCO 1536 MHz, ÷6÷2 = 128 MHz;
USB 48 MHz from VCO 960 ÷5÷4. A non-12 MHz XOSC needs OTP boot settings for USB boot; a 12 MHz
reference avoids that. Decision open.

## RP2350B pin budget (draft)

| GPIO | function | notes |
|---|---|---|
| 0–13 | AFE D0–D13 | PIO `out pins, 16`; same as Pico Plus 2 benchmark |
| 14 | AFE IQ_FLAG | carried in bit 14 of the I word |
| 15 | spare bit / block marker | logic-analyzer frame alignment |
| 16 | AFE CLK_IO | side-set |
| 17–22 | host link CLK, D0–D3, READY | 8.2 kΩ or stronger pulls on floating lines (E9 if A2) |
| 23–26 | AFE SPI: SCLK, SDIO, SDENB, RESETB | 3-pin SPI, ≤ 10 MHz (tSCLK ≥ 100 ns) |
| 27 | AFE SYNC_SLEEP | FIFO sync after clocks are stable |
| 28 | AFE ALARM_SDO | FIFO pointer alarms (CONFIG2/3/7) |
| 29–32 | LO synth SPI + lock detect | |
| 33–34 | DAC clock generator I2C/SPI | |
| 35–36 | CAN TX/RX (can2040) | SATS variant |
| 37 | PA enable (default off by pull) | never enabled by reset state |
| 40–43 | ADC: rail and temperature telemetry | |
| 44 | status LED | |
| 47 | PSRAM CS | if fitted |

## Parallel-bus timing budget (estimate)

At 64 MW/s (cpw = 2, 128 MHz), T_sys = 7.8 ns. PIO changes data at t₀ and raises CLK_IO at t₀ + T_sys.

- setup ≥ T_sys − (bank skew ≤ 2.1 ns) − trace skew = 5.7 ns − trace skew; the AFE needs 1 ns
- hold ≥ T_sys − 2.1 ns = 5.7 ns; the AFE needs 1 ns

The 2.1 ns skew and ≤ 4.1 ns clock-to-pad figures are QMI-table values (RP2350 datasheet
pp.1233–1234), not PIO-specific. Verify at G5. Layout intent: D[13:0], IQ_FLAG and CLK_IO matched
within about 5 mm, under 5 cm total, over solid ground, with 22–33 Ω source series resistors as
DNP-able options.

## AFE power (SLOS789C p.4, typical)

1.8 V domains (DVDD18, CLKVDD18, DACVDD18, MODVDD18, FUSEVDD18): about 22–36 mA.
3.3 V domains (DACVDD33, MODVDD33, IOVDD): about 101–102 mA. Total about 334 mW at 65 MS/s.
Use separate low-noise LDOs for the analog 1.8 V and 3.3 V rails. Budget the LO synth and PA
separately.

## Test access

SMA for RF_OUT and an optional external LO. Test points for DACCLK, CLK_IO, IQ_FLAG and
SYNC_SLEEP. A 0.1 in header for D[15:0] + CLK_IO for a logic analyzer. SWD. The AFE's full SPI
readback (4-pin mode) for register verification.
