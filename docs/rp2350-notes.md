# RP2350 and Pico Plus 2 notes

Facts the firmware relies on. RP2350 citations are (§section, p.printed page) in the RP2350
datasheet, build 2025-07-29 [RP2350]. Board citations use the Pimoroni schematic [PPP2-SCH] and
pico-sdk 2.2.0 [SDK]. Tags are listed in [sources/SOURCES.md](sources/SOURCES.md). "Derived"
means arithmetic here; "unverified" means no source states it.

## Design consequences

1. **clk_sys = 128 MHz** (12 MHz XOSC, REFDIV 1, FBDIV 128, VCO 1536 MHz, PD 6/2). It is the
   in-spec (≤ 150 MHz) setting with whole PIO cycles per word at both bus rates: 4 at 32 MW/s,
   2 at 64 MW/s. 144 or 150 MHz would need a fractional divider and add one clk_sys (≈ 6.7 ns) of
   edge jitter (derived; §8.6.3 p576, §11.5.5 p911).
2. **PIO output:** one SM, OUT on 15 contiguous pins (D13:0 + IQ_FLAG), side-set on CLK_IO,
   autopull 32, FJOIN_TX. FDEBUG.TXSTALL is a sticky underrun flag (§11.5.1 p902, Table 984 p946).
3. **DMA:** 16 M words/s at 64 MW/s is one write per 8 clk_sys; the DMA/PIO limit is one per clock.
   PIO FIFOs, DMA registers and USB share the FASTPERI port, so don't poll PIO/DMA from the CPU
   (§2.1 p24, §11.5.3 p906).
4. **Placement:** hot code in SRAM, never XIP. Tables in SRAM4–7, DMA ring in SRAM0–3, hot kernels
   and core-1 stack in SRAM8. Instruction fetch and data loads to the same bank collide (§4.2
   p337–338, §3.7.4.9.12 p144).
5. **Clock locking for the AFE7071:** the PLL reference is XOSC/XIN only. Feed a common CMOS
   reference (≤ 50 MHz) into XIN. GPIN can clock clk_sys only directly, not through the PLL
   (§8.1.2.4 p517, Fig. 40 p575).
6. **GPIO:** fast slew and 8 mA on the bus (`BUS_DRIVE` in config.h; G5 picks 8 or 12 mA), IOVDD
   equal to the AFE IOVDD. Worst-case clk→pad ≤ 4.1 ns at 3.3 V and Bank-0 skew ≤ 2.1 ns (QMI
   tables, not PIO-specific) against 15.6 ns at 64 MW/s (Tables 1291–1292 p1233–1234).

## Board: Pimoroni Pico Plus 2 (PIM724)

- RP2350B (QFN-80), 16 MB flash, 8 MB PSRAM (CS = GPIO47), USB-C. The bench unit is rev **A2**
  (picotool), so erratum E9 applies. `PICO_BOARD=pimoroni_pico_plus2_rp2350`.
- IOVDD is fixed at 3.3 V (no jumper), so the AFE7071 IOVDD must be 3.3 V or level-shifted
  [PPP2-SCH sh1].
- Crystal frequency is not printed; USB enumerates at the SDK default clocks, which requires
  12 MHz.
- GPIO0–22 go straight to the header with no series resistors or termination. GP4/5 also go to
  the Qw/ST connector; GP26–28 carry a 1 kΩ branch to GPIO40–42; 32–36 are on SP/CE only; 25 is
  the LED, 45 the BOOT/user switch [PPP2-SCH sh1–3].
- SDK defaults to disable: UART0 on GP0/1 (use USB CDC stdio) and I2C0 on GP4/5 (leave Qw/ST
  unplugged) [SDK board header].
- The Pico Plus 2 W (PIM726) is a different board: RM2 radio on GPIO23–25/29, no SP/CE.

Pin use. `firmware/src/config.h` is the authority.

| GPIO | Header pin | Use |
|---|---|---|
| 0–13 | 1, 2, 4–7, 9–12, 14–17 | AFE D0–D13 |
| 14 | 19 | IQ_FLAG |
| 15 | 20 | spare bus bit (always 0) |
| 16 | 21 | CLK_IO (PIO side-set) |
| 17, 18, 19, 20, 22 | 22, 24, 25, 26, 29 | PV-SPI SCK, MOSI, CS_N, MISO, READY (or the 4-lane link) |

The bus sits in order on one header row with 4 GND pins between the signals.

## RP2350 reference

**Clocks** (§8.6.3 p576, Table 541 p518)
- clk_sys ≤ 150 MHz; clk_ref ≤ 25 MHz; above 150 MHz is outside the spec.
- PLL: FOUT = (FREF/REFDIV)·FBDIV/(PD1·PD2); FREF/REFDIV ≥ 5 MHz; VCO 750–1600 MHz; FBDIV 16–320,
  integer only.
- A non-12 MHz XIN breaks default USB BOOTSEL unless OTP BOOTSEL_PLL_CFG/XOSC_CFG are set
  (§5.2.8.1 p375).
- GPOUT ≤ 50 MHz, so 64 MHz CLK_IO must come from PIO side-set (§8.1 p514).

**PIO** (§11)
- 3 blocks × 4 SMs, 32 instructions per block, 1 instruction per clk_sys.
- Autopull refills in the same cycle unless TX is empty; an underrun costs ≥ 1 cycle (p910).
- GPIOBASE is 0 or 16 per block (p877, p956).
- Inputs pass a 2-flop synchroniser (2 cycles), bypassable per pin (§11.5.6.3 p913). A `wait` /
  `in` receiver needs about 3 clk_sys per SCK half-period (derived), about 20–25 MHz at 128 MHz.
- Clock divider: 16.8 fixed point; fractional values jitter by one cycle (§11.5.5 p911).

**GPIO electrical** (Table 1436 p1339–1340)
- At 3.3 V: VOH ≥ 2.62 V and VOL ≤ 0.5 V at the selected drive; VIH ≥ 2.0 V, VIL ≤ 0.8 V.
- Pad reset: 4 mA, slow slew, input disabled, isolation latched. `gpio_set_function()` sets IE and
  clears ISO (§9.3 p588, §9.7 p596).

**Memory and bus** (§2.1 p24–25, §4.2 p337)
- SRAM0–3 and SRAM4–7 are two word-striped 256 KB halves; SRAM8/9 are 4 KB, unstriped
  (SCRATCH_X/Y in the SDK).
- One access per bank per cycle; up to six managers in parallel.
- BUSCTRL has 4 saturating 24-bit counters (SRAMn/FASTPERI access and contention) (§12.15.4
  p1255–1260).

**DMA** (§12.6)
- 16 channels, one read and one write per cycle. TRANS_COUNT modes: normal, TRIGGER_SELF, ENDLESS.
- RING_SIZE wraps up to 32 KB. CHAIN_TO resets to 0; point it at the channel itself to disable.
- The CRC sniffer runs at 32 bits per clock. CRC32R with out_rev + out_inv and seed 0xFFFFFFFF
  equals zlib CRC-32 (measured, `snifftest`).

**Cortex-M33 timings** (§3.7.4.9 p137–144; Arm publishes no cycle table)
- LDR 1 cycle + 1 result-use penalty; STR 1; UBFX/BFI no penalty; MUL/MLA 1 + 1.
- "Complex" shifts (other than LSL #0–3) and SEL take the result-use penalty.
- Taken branch ≥ 2 cycles; IT and NOP fold with a 16-bit neighbour.
- LDRD, STM, PKHBT/PKHTB cycle counts: unverified (measured in context only).

**Counters**
- DWT_CYCCNT is 32-bit and wraps every 33.5 s at 128 MHz; read DWT_CTRL at run time (Table 143
  p164 contradicts p145).
- TIMER0/1 are 64-bit µs counters, safe to read raw from both cores (§12.8 p1184).

**Errata** (Appx E p1357–1376)
- **E9** (A2 only): a Bank-0 input with IE = 1 floating between VIL and VIH sits near 2.2 V
  through about 120 µA. Use an external pull ≤ 8.2 kΩ or keep IE = 0.
- **E27:** FASTPERI/APB priority bits are mis-wired; PROC0 controls DMA-write priority.
- **E5 / E8:** DMA abort with CHAIN_TO can re-trigger; CHAIN_TO may not fire on zero length.
- **E2:** SIO writes at 0x180–0x1FC alias spinlocks. **E1:** interpolator OVERF is broken.
- **E12:** USB needs clk_sys ≥ 1.1 × clk_usb.
- No PIO, clock/PLL or GPIO-output errata are listed.

## Unverified

- GPIO toggle limit and PIO-path output delay/skew (G5 measures this).
- Latency with INPUT_SYNC_BYPASS; absolute DREQ latency.
- Sustained PSRAM bandwidth (matters only for a file store).
