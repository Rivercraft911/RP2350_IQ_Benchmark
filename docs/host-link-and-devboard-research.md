# Host link and dev-board research: Pi 5/CM5 → RP2350 → AFE7071

Research date: 2026-09-29. Desk research only: nothing was measured, bought or requested.

**Labels.** [DS] manufacturer datasheet or document. [SRC] kernel or library source. [ENG] Raspberry Pi engineer, public statement. [3P] third-party report. [EST] estimate derived here. [UNK] unknown.

**Requirement.** 8 Msym/s QPSK × 2 bit/symbol = 16 Mb/s = 2.0 MB/s of framed, FEC-coded bits. It must be sustained without underrun, with flow control. The 2× target is 32 Mb/s (4.0 MB/s).

---

## 1. Recommendations

### 1.1 Host link

**Primary: RP1 PIO → RP2350 PIO over a 4-bit synchronous bus, with hardware back-pressure.**
- The Pi 5/CM5 runs a small PIO program on RP1, driven from Linux through `piolib` with DMA.
- The program clocks out CLK, D[3:0] and FRAME.
- The RP2350 receives with a PIO state machine and DMA into an SRAM ring buffer.
- The RP2350 drives READY. The RP1 PIO program tests READY before each 32-bit word, so a full buffer stalls RP1 PIO.
- The stall propagates back through the RP1 DMA bounce buffers to the Linux `write()` call, which then blocks.
- At a 10 MHz link clock the bus carries 40 Mb/s raw, 2.5× the requirement.
- 10 MHz is 200 MHz / (2 instructions × integer divider 10), assuming the RP1 PIO clock is 200 MHz [UNK]. An integer divider avoids the clock "limping" pelwell saw with a non-integer divisor [ENG: utils#116, 2025-01-17].

Why this is primary:
- The measured RP1 PIO TX DMA ceiling is 10.7 MB/s on older kernels and about 27 MB/s after PR #6994 [3P][ENG]. That is 5–13× the 2 MB/s need.
- A Raspberry Pi engineer stated "2MB/s is well within the capabilities of RP1's PIO and DMA" [ENG].
- Flow control lives in PIO hardware, so no Linux GPIO polling loop is needed.

**Fallback: RP1 SPI0 master (spidev + DMA) → RP2350 PIO SPI receiver.**
- SCLK is 20 MHz (200 MHz / 10), single lane, 16-bit words.
- Flow control is a READY GPIO that the host checks (libgpiod edge wait) before each block of ≥ 16 KiB.
- Raw rate is 20 Mb/s, only 1.25× margin, and the effective rate on Pi 5 is unmeasured [UNK].
- Linux has no dual/quad SPI on RP1 [SRC], so wider SPI is not available.

**Rejected:**
- **RP2350 hardware SPI slave (PL022):** limited to clk_peri/12 = 12.5 Mb/s [DS].
- **USB:** RP2350 is Full Speed only. The bulk payload ceiling is 9.73 Mb/s [DS].
- **SMI:** absent on Pi 5 [SRC][ENG].
- **DPI:** no back-pressure, untested as a data path [SRC][EST].
- **W5500 Ethernet:** adds a chip, and SPI is guaranteed only to 33.3 MHz [DS].

| Interface | Raw rate | Sustained expectation | Flow control | Pins (Pi side → RP2350) | Evidence quality |
|---|---|---|---|---|---|
| **RP1 PIO 4-bit + CLK (primary)** | 40 Mb/s at 10 MHz. Clock is set in the PIO program; source DMA ceiling ~86 Mb/s (old kernel) or ~216 Mb/s (PR #6994) | ≥ 16 Mb/s expected [EST]. 40 µs gaps between DMA buffers (old kernel) are absorbed by the RP2350 buffer | READY → RP1 PIO `wait`/`jmp pin` → DMA stall → blocking `write()` | 7 of GPIO0–27 (D0–D3 consecutive): CLK, D0–D3, FRAME, READY (+GND) | Medium. Engineer statement and third-party DMA measurements, not this configuration |
| RP1 SPI0 → RP2350 PIO RX (fallback) | 20 Mb/s (25 Mb/s at 200/8 is marginal on the RP2350 side) | Unmeasured. At 50 MHz with 8-bit words, SCLK was active 50 % of the time (25 Mb/s effective) [ENG] | READY GPIO checked by host per block; optional status on MISO | GPIO11 SCLK, GPIO10 MOSI, GPIO9 MISO, GPIO8 CE0, + READY | Medium-low |
| 2 × RP1 SPI → 2 × RP2350 PL022 slave | 2 × 11.1 Mb/s = 22.2 Mb/s (200/18) | Unmeasured. Needs two independent streams | READY per link | SPI0 + SPI1 pins, 2 × READY | Low (datasheet limits only) |
| RP1 SPI → RP2350 PL022 slave | ≤ 12.5 Mb/s | **Fails** 16 Mb/s | — | — | High (datasheet) |
| USB (RP2350 FS device) | 12 Mb/s signalling | ≤ 9.73 Mb/s bulk payload; **fails** | USB NAK | USB D+/D− | High (datasheet + USB 2.0 spec) |
| DPI | ≤ 200 MHz pixel clock × ≥ 16 bits | No back-pressure; frame blanking | None (fill frames only) | ≥ 20 GPIO | Low (no data-link use found) |
| SMI | — | Not present on Pi 5 | — | — | High |
| W5500 (Ethernet on RP2350) | ≤ 33.3 Mb/s SPI (guaranteed), 100BASE-TX | Unknown net rate after SPI framing overhead | TCP window | RJ45 + W5500 on RP2350 SPI | Low; not pursued |

### 1.2 Clocking (scaffold for the custom board)

**Use one low-noise reference for everything.** In the AFE7071's FIFO clock mode (dual-input clock), the DAC clock and the data clock "must be frequency locked" [DS]. This mode is the natural one for an RP2350 that drives CLK_IO.
- The reference feeds the LO synthesizer's reference input.
- It feeds the DACCLK path.
- It feeds a 3.3 V CMOS copy to RP2350 XIN (≤ 50 MHz) [DS].

**The host link needs no frequency lock.** It is buffered and flow-controlled.

**DACCLK electrical constraints:**
- The pins are referenced to the 1.8 V clock supply (absolute maximum −0.5 V to CLKVDD18 + 0.5 V).
- The input needs 0.4–1 V differential and 40–60 % duty [DS].
- LVPECL or 3.3 V HCSL must therefore be AC-coupled and re-biased.
- It is not stated whether the 0.4–1 V is peak or peak-to-peak [UNK]. TI's EVM feeds an LVPECL-level DACCLK through a transformer into 100 Ω [DS: SLOU337A §1.4.5].

**Scaffold clock tree (a proposal, not a selection; [EST]):**
- A 40 MHz TCXO feeds a 1:4 LVCMOS fan-out buffer (e.g. LMK1C1104). Its outputs go to:
  - **LO synthesizer reference.** The LMX2572 covers 1.28 GHz and 2.2–2.3 GHz. The LMX2572LP covers 1.28 GHz only, and is the LO TI recommends for AFE7070 in SLOA313.
  - **DACCLK generator** (e.g. CDCE6214 integer mode: 40 MHz × 64 = 2560 MHz VCO → 32 or 64 MHz differential), or a fixed LVDS/LVPECL oscillator if the reference is not shared.
  - **RP2350 XIN** as 3.3 V CMOS.
- RP2350 clock plan from 40 MHz: 40 × 32 = 1280 MHz → 128 MHz clk_sys, exactly 4 × 32 MHz. 40 × 30 = 1200 MHz → 48 MHz USB. clk_ref must be divided to ≤ 25 MHz [DS].
- USB BOOTSEL with a non-12 MHz clock needs the OTP BOOTSEL_XOSC_CFG / BOOTSEL_PLL_CFG entries [DS: RP2350 §5.2.8.1, p. 375].
- Use the AFE7071 **dual-input clock mode**. The RP2350 PIO generates CLK_IO and the data. The AFE7071 FIFO absorbs up to ±4 cycles of phase difference, so RP2350 PLL jitter affects only CLK_IO setup/hold, not the DAC sampling instant [DS: AFE7071 p. 25].

---

## 2. Q1 detail: host → RP2350 link

### 2.1 Rates that drive the design
- Payload is 2.0 MB/s. At a 256 KiB RP2350 ring buffer, one buffer holds 131 ms of data [EST: 262 144 B / 2.0 MB/s].
- That covers Linux scheduling stalls of tens of ms on a stock kernel. The stall distribution on the target image is [UNK] and must be measured.
- RP2350 has "520 kB on-chip SRAM, in 10 independent banks" [DS: RP2350 ch. 1, p. 13]. That is enough for this buffer plus the sample pipeline. Bank placement relative to the DAC-output DMA is [UNK] until the firmware is laid out.

### 2.2 Pi 5 / CM5 RP1 SPI master
**Controllers:**
- RP1 has nine Synopsys DW_apb_ssi controllers [DS: RP1 peripherals §3.6]. Six are on GPIO bank 0.
- SPI0 is a master with 4 CS and quad-capable hardware.
- SPI1/2/3/5 are masters with dual-capable hardware.
- SPI4 is a slave, single lane.
- The same table is in the CM5 datasheet §2.9.2 [DS].
- "DDR operation is not supported" [DS: RP1 §3.6.1].

**Clock:**
- SPI is clocked from clk_sys = 200 MHz [DS: RP1 §2.5.2; SRC: `clk-rp1.c`].
- The driver forces an even divider (`spi-dw-core.c` L401–403) [SRC], so SCLK is 200/2n: 100, 50, 33.3, 25, 20, 16.7, 12.5, 11.1 MHz and so on.
- A request for 80 MHz gives 50 MHz [ENG: pelwell, raspberrypi/linux#6020, 2024-03-15].
- Raspberry Pi guidance is that over 50 MHz at a header pin is unlikely to work [ENG: PhilE, forum t=360436; RPi SPI documentation].

**Gaps (the only primary measurement found):**
- "it's only driving SCLK half the time, so there's an effective rate of 25MHz" [ENG: pelwell, #6020, 2024-03-19]. This was spidev TX at 50 MHz with 8-bit words.
- Hypothesis [EST]: the gaps are a per-DMA-beat limit. The effective rate was 3.1 MB/s with 1-byte beats. RP1 PIO TX saturated at about 2.7 M beats/s × 4 B (10.7 MB/s) on the same DMAC and kernel era.
- If so, 16- or 32-bit SPI words (`bits_per_word`) would remove most of the gap. **Test this first.**
- Whether RP1 SPI has DFS32 (32-bit frames) is [UNK]. The driver supports 4–16 bits without it (L1011–1014) [SRC].

**DMA:**
- Every RP1 SPI node has TX/RX DMA channels on the RP1 DMAC [SRC: `rp1.dtsi` L179–191].
- DMA is used only when the transfer is longer than the FIFO (`spi-dw-dma.c` L246–257) [SRC]. The FIFO is 64 bytes [ENG: pelwell, #5865; not in the RP1 document].
- The RP1 DMAC has 8 channels. It is quoted at "500-600Mbs" typical read bandwidth per channel [DS: RP1 ch. 9].

**spidev:**
- `static unsigned bufsiz = 4096; module_param(bufsiz, uint, S_IRUGO)` (`drivers/spi/spidev.c` L86–87) [SRC].
- It is read-only at runtime, so set it at boot with `spidev.bufsiz=65536` on the kernel command line.
- A single `write()` or `SPI_IOC_MESSAGE` larger than bufsiz is rejected.
- The gap between ioctl calls (syscall plus GPIO chip-select toggle; RP1 SPI uses GPIO CS [DS: RPi SPI docs]) is [UNK] on Pi 5.
- At 16–64 KiB per call, only 122–31 calls/s are needed. A 1 ms gap per call would cost ≈ 12 % at 16 KiB and ≈ 3 % at 64 KiB [EST].

**Continuous streaming:**
- Gapless SCLK is not guaranteed. It is not needed either, because the RP2350 receiver waits for edges.
- Only the average rate matters.

**Dual/quad:** not available from Linux. `mode_bits = SPI_CPOL|SPI_CPHA|SPI_LOOP`, and spi-mem rejects buswidth > 1 (`spi-dw-core.c` L1010, L566–574) [SRC].

**Pins (GPIO numbers are identical on Pi 5 header and CM5)** [DS: RP1 §3.1 Table 4; CM5 §2.9.1]:

| Bus | SCLK | MOSI | MISO | CS | Overlay |
|---|---|---|---|---|---|
| SPI0 | GPIO11 | GPIO10 | GPIO9 | GPIO8, GPIO7 | `dtparam=spi=on` / `spi0-1cs` |
| SPI1 | GPIO21 | GPIO20 | GPIO19 | GPIO18/17/16 | `spi1-1cs` … `spi1-3cs` |
| SPI5 | GPIO15 | GPIO14 | GPIO13 | GPIO12, GPIO26 | `spi5-1cs` |

- The CM5 GPIO bank runs at 1.8 V or 3.3 V, set by GPIO_VREF. Total load on the 28 pins is ≤ 50 mA [DS: CM5 §2.9].
- Tie GPIO_VREF to 3.3 V to match RP2350 IOVDD = 3.3 V.

**SPI slave on the Pi (RP1 SPI4) with the RP2350 as master:**
- The RP1 target-mode driver is in an open PR (raspberrypi/linux#7132, 2025-11-11) with no overlay [SRC]. It is not usable today.
- Linux SPI target mode also needs a transfer queued before the master clocks, which is a poor fit for streaming [EST].

### 2.3 RP1 PIO from Linux (`rp1-pio` driver + `piolib`)
**Hardware:**
- One PIO block, 4 state machines, 32-instruction memory, "almost identical" to RP2040 PIO [ENG: Raspberry Pi news, 2024-12-17].
- PIO is an alternate function on every bank-0 GPIO0–27 [DS: CM5 §2.9.1 Table 1].

**Control path:**
- Most `piolib` calls are RPCs to RP1 firmware, costing ≥ 10 µs each [ENG: same news post].
- Per-word `pio_sm_put`/`get` is therefore slow. The measured blocking get rate is about 250 kB/s [3P: forum t=390556].
- Blocking calls block the whole RP1 firmware interface [SRC: piolib README "known issues"].
- **Use DMA only** for the data stream.

**DMA TX path:**
- `pio_sm_config_xfer()` allocates up to 4 bounce buffers (`DMA_BOUNCE_BUFFER_COUNT 4`; size a multiple of 4 B) [SRC: `rp1-pio.c` L79–80, L1047–1052].
- `pio_sm_xfer_data()` copies from user space and submits one DMA descriptor per buffer.
- When all buffers are in flight it waits in `down_interruptible()` with **no timeout** (L1252–1309, wait at L1268) [SRC]. A PIO program stalled by READY therefore just blocks the writer, which is the back-pressure mechanism.
- Transfers above 65 535 B use the 32-bit ioctl automatically [SRC: piolib `pio_rp1.c` L262–285].
- Cyclic DMA exists only for RX, "FROM_SM" (L1051) [SRC].

**Measured throughput (TX to state machine):**
- 3.99 MB/s at 1 MHz, 7.97 MB/s at 2 MHz, plateau about 10.7 MB/s from 5 MHz up. This is 32-bit words, kernel 6.6.70 [3P: jepler, raspberrypi/utils#116, 2025-01-17].
- pelwell observed "steady transmission of a buffer full of data, with 40us gaps in between the buffers"; the state-machine clock was "capped at ~2.6MHz" [ENG: utils#116, 2025-01-17].
- PR #6994 (merged 2025-08-19) reserves the 8-beat DMA channels for PIO. It "did boost throughput to ~27MB/s" [ENG: pelwell, utils#116, 2025-08-12; SRC: raspberrypi/linux#6994].
- Check against the primary design [EST]: 40 Mb/s = 1.25 M 32-bit words/s. That is below the ≈ 2.6–2.7 M words/s pre-#6994 ceiling. The sustained 16 Mb/s is 0.5 M words/s.

**Open driver risks:**
- TX xfer hang on `dma2chan2` after 6.18.50, fixed by PR #7654 merged 2026-09-29 [SRC: raspberrypi/linux#7642]. Pin the kernel version and soak-test.
- One user reports changed `wait`-on-pin behaviour after the DMA changes [3P: utils#116, 2026-03-10; unresolved]. Keep a `jmp pin` variant of the READY test available.

**Suggested RP1-side program (sketch, [EST]):**
- Autopull 32 bits.
- Per word: test READY with `jmp pin`. Then shift 8 nibbles out as `out pins,4 side 0` / `nop side 1`, with an integer clock divider of 10 (10 MHz link clock).
- Drive FRAME on the first nibble of each block. It gives word alignment and a resync point.
- The RP2350 deasserts READY when free space drops below a threshold. The threshold must be ≫ the in-flight skid of one word plus synchronizer delay.

### 2.4 DPI, SMI, GPCLK on Pi 5
**DPI:**
- RP1 DPI outputs up to 24 bits plus PCLK, DE, HSYNC and VSYNC on GPIO0–27 [DS: RP1 Table 4].
- The DRM driver accepts pixel clocks of 1–200 MHz (`rp1_dpi.c` L239–242) [SRC]. RGB565 uses GPIO0–19 [SRC: overlays README `vc4-kms-dpi-generic`].
- It is a free-running framebuffer scanout, with blanking intervals and no back-pressure.
- Throttling would need host-side fill frames plus a separate status channel [EST].
- No use of Pi 5 DPI as a data link was found. Not recommended.

**SMI:** not on Pi 5.
- RP1 has no SMI function in its GPIO function table [DS: RP1 §3.1].
- The `smi` overlay targets `brcm,bcm2835` [SRC].
- The absence is also stated by a user and not contradicted by PhilE [ENG: forum t=395303].

**GPCLK:**
- GPCLK0–5 are capped at 100 MHz [SRC: `clk-rp1.c`]. The CM5 exposes up to three [DS].
- They are derived from RP1's 50 MHz crystal [DS: RP1 §2.5].
- It is a clock only, not a data path. Do not use it as the RF or DAC reference, because it is not locked to the board reference.

### 2.5 USB
- RP2350 contains a USB 2.0 controller operating as "a Full Speed (FS) device (12 Mb/s)" with "an integrated USB 1.1 PHY" [DS: RP2350 §12.7, p. 1141].
- The USB 2.0 Table 5-9 limit for 64-byte full-speed bulk is 19 transactions per frame = 1 216 000 B/s = 9.73 Mb/s, before bit stuffing [DS: USB 2.0 spec §5.8.4, p. 54].
- **USB cannot carry 16 Mb/s.** The data is already FEC-coded, so it is not compressible.
- Pi 5/CM5 USB host capability is not the limit [DS: RP1 ch. 1; CM5 datasheet].

### 2.6 Ethernet via W5500
- W5500 SPI: "theoretical design speed is 80MHz"; "the minimum guaranteed speed of the SCLK is 33.3 MHz" [DS: W5500 v1.1.0, §5.5 SPI timing, p. 61].
- It has a 100BASE-TX PHY and 32 KB of socket buffer [DS].
- The RP2350 would be SPI master (PL022 master up to clk_peri/2 [DS]). The raw rate is ≤ 33 Mb/s before W5500 framing and register overhead, so the net rate is [UNK].
- TCP gives flow control for free. It still adds a chip, a PHY and magnetics, and it is no faster than the fallback.
- Not pursued.

### 2.7 RP2350 receive side
**PL022 hardware SPI slave:**
- "SSPCLK must be at least 12 times faster than the maximum expected frequency of SSPCLKIN."
- At the maximum clk_peri of 150 MHz, "a peak bit rate of 150 / 12 = 12.5Mb/s" [DS: RP2350 §12.3.4.4, p. 1050]. clk_peri nominal range is 12–150 MHz [DS: §8.1, p. 519].
- **Insufficient** for 16 Mb/s.
- Two PL022 slaves on two host SPI buses give 2 × 11.1 Mb/s (Pi divider 200/18) [EST]. That is possible but awkward.

**PIO receiver:**
- 3 PIO blocks × 4 state machines [DS: §11.1, p. 876]. Each has 4-word TX/RX FIFOs [DS: §11.2, p. 883], with DMA.
- Each GPIO input has a 2-flip-flop synchronizer adding 2 cycles, which can be bypassed per pin for synchronous interfaces [DS: §11.5.6.3, p. 913].
- Clock and data pass through identical synchronizers, so their relative timing is preserved. The limit is edge detection: each SCLK half-period needs about ≥ 3 clk_sys cycles for a `wait`/`in` loop [EST].
- At clk_sys = 150 MHz that means:
  - 20 MHz SCLK = 7.5 cycles/period, workable [EST].
  - 25 MHz = 6 cycles, marginal [EST].
  - 50 MHz is not feasible [EST].
- One report found a PIO SPI *master* on RP2350 failed above about 25 MHz because of round-trip input delay. A reply attributes this to 4 cycles (26.7 ns) of synchronizer plus input logic at 150 MHz [3P: forum t=377694]. A receive-only slave has no round trip.
- The 4-bit primary bus at 10 MHz gives 7.5 clk_sys cycles per half-period. That is the reason to prefer it over 1-bit at 40 MHz (1.9 cycles per half-period).

**Other RP2350 notes:**
- HSTX is output-only [DS: §12.11, p. 1202], so it cannot receive.
- The bench board's RP2350 is revision **A2** (`../backup/original-picotool-info.txt`). Erratum RP2350-E9 applies: a Bank 0 input left in the undefined region can latch near 2.2 V through about 120 µA of leakage [DS: p. 1366].
- Link inputs are driven push-pull by RP1, but idle or unpowered-host states are not. Put external pull resistors ≤ 8.2 kΩ on READY/FRAME/CS if a defined idle level is required [DS: E9 workaround text].

---

## 3. Q2 detail: dev-board component context (no selection)

### 3.1 AFE7071 facts that constrain the board (from SLOS789C)
**Data and clock modes:**
- Interleaved 14-bit I/Q on D[13:0]. f_DAC ≤ 65 MSPS and f_INPUT = 2 × f_DAC ≤ 130 MSPS [DS: p. 5].
- Three clock modes [DS: pp. 17, 25–27]:
  - **Dual-input clock:** DACCLK at 2× the internal sample rate. CLK_IO is a CMOS *input* at the data rate. An internal FIFO tolerates ±4 cycles of phase wander, but the clocks must be frequency-locked.
  - **Dual-output clock:** DACCLK at 2×. The AFE7071 *outputs* CLK_IO. The FIFO is bypassed.
  - **Single differential DDR:** DACCLK at 1×. I is latched on the rising edge and Q on the falling edge. Setup is 0 ns and hold 2 ns relative to DACCLK.

**DACCLK frequency by oversampling (8 Msym/s)** [EST]:

| Samples/symbol | f_DAC | Bus word rate | DACCLK, dual-clock modes | DACCLK, DDR mode |
|---|---|---|---|---|
| 2 | 16 MSPS | 32 MW/s | 32 MHz | 16 MHz |
| 4 | 32 MSPS | 64 MW/s | 64 MHz | 32 MHz |

**Electrical limits:**
- DACCLKP/N: 0.4–1 V differential, 40–60 % duty [DS: p. 5]. Absolute maximum −0.5 V to CLKVDD18 + 0.5 V = 2.3 V at CLKVDD18 = 1.8 V [DS: p. 4].
- LO input: 0.1–2.7 GHz, P_LO −5 to +5 dBm, 15 dB return loss [DS: p. 6]. LO_P/LO_N absolute maximum is −0.5 V to MODVDD33 + 0.5 V [DS: p. 4].
- IOVDD 1.71–3.6 V [DS: p. 4]. CMOS inputs at IOVDD = 3.3 V: V_IH ≥ 2.3 V, V_IL ≤ 1.0 V. Outputs: V_OH ≥ 0.8·IOVDD, V_OL ≤ 0.22·IOVDD at 2 mA [DS: p. 5].
- Serial configuration port: SCLK period ≥ 100 ns, i.e. ≤ 10 MHz [DS: p. 5].

**Which clock mode for an RP2350 source [EST]:**
- *Dual-input* is the fit. The RP2350 drives CLK_IO, the FIFO absorbs phase, and the only condition is frequency lock, which a shared reference provides.
- *Dual-output* would make the RP2350 PIO follow a 32–64 MHz CLK_IO. That is only 2.3–4.7 clk_sys cycles per period at 150 MHz, too tight for `wait`-based pacing.
- *DDR single-clock* needs the RP2350 data phase-aligned to DACCLK with 0 ns setup / 2 ns hold, and has no FIFO.

### 3.2 AFE7071 status, price, EVM, reference designs
**Lifecycle:**
- AFE7071 is **ACTIVE** in the ti.com product-page metadata (fetched 2026-09-29) [DS: ti.com/product/AFE7071]. The package is 48-pin VQFN (RGZ), 7 × 7 mm.
- AFE7070 is also ACTIVE [DS: ti.com/product/AFE7070].

**TI.com prices (2026-09-29), from product-page data:**
- AFE7071IRGZT (250-unit reel): $26.553 at 1–99; $17.355 per unit at 1ku.
- AFE7071IRGZR (2500-unit reel): $23.187 at 1–99; $15.155 at 1ku.
- AFE7070IRGZT: $47.806 at 1–99; $31.246 at 1ku.
- The 2012 launch prices were $11.90 (AFE7071) and $23.75 (AFE7070) at 1ku [3P: Microwave Journal].

**Stock is [UNK]:**
- TI.com inventory needs a login.
- The part-details page carries a purchase-limit note, "to protect sample purchases … will be removed once more stock is available". That hints at constrained supply [DS: ti.com part-details, AFE7071IRGZT; interpretation unverified].
- DigiKey, Mouser and Octopart blocked automated access. A search-engine snippet of DigiKey 3767572 showed $36.02 at qty 1 with 83 in stock, but the capture date is unknown and it was not verified live.

**EVM:**
- The only board is the **AFE707xEVM** (user guide SLOU337A, 2012, revised 2015). It is built for AFE7070, and the guide states that AFE7071 can be evaluated on it [DS].
- The tool pages ti.com/tool/AFE7070EVM and /AFE7071EVM return HTTP 404 (2026-09-29), and the product pages list no orderable EVM. Inferred: it is no longer sold [UNK]. It launched at $499 [3P].

**EVM clocking [DS: SLOU337A §1.3–1.4]:**
- DACCLK and CLK_IO come from a **CDCM7005** clock synchronizer (ACTIVE, $12.54 at 1ku [DS: ti.com]). It uses an onboard 10 MHz reference plus a VCXO-input clock on J4. J4 accepts 1–2.6 V p-p, AC-coupled and re-biased to 1.3 V.
- The quick-start drives J4 with 130 MHz at 0 dBm (2 × 65 MSPS). The CDCM7005 can be bypassed with an LVPECL-level DACCLK on J12, through a 2:1 transformer, with 100 Ω termination (R55) at DACCLKP/N.
- **There is no LO synthesizer on the EVM.** The LO comes in on SMA J10 at −5 to +5 dBm, AC-coupled to LOP, with LON AC-terminated in 50 Ω.
- Data comes from a TSW1400 pattern generator.

**Reference designs:**
- No TIDA reference design lists AFE7070 or AFE7071 (product pages checked; nothing found by search).
- The nearest TI material is application note **SLOA313**, "AFE7070 Optimized Operation in the VHF Band" (2021) [DS]:
  - "The nominal LO drive is 4 dBm."
  - Sideband suppression "degrades 15 to 30 dB from the optimum point with just a ±1 dB" LO drive variation.
  - "For the actual product LO, the LMX2572LP synthesizer plus harmonic filter is recommended." The measured data used that combination.
- A TI E2E thread on AFE7071 with an LMX2572LP LO describes a low-pass filter on the square-wave-like output to suppress the LO 3rd harmonic. It was read via search snippet only and is unverified [3P].

### 3.3 DAC clock options
**Requirement:**
- 16, 32 or 64 MHz (§3.1 table), 40–60 % duty, 0.4–1 V differential, within the 1.8 V-referenced absolute maximum [DS].
- A jitter bound shows this is not demanding. Jitter-limited SNR = −20·log10(2π·f_out·σ_t). At f_out = 5 MHz baseband and σ_t = 1.7 ps that is 85.5 dB, about the ideal 14-bit SNR (86 dB), and far above what QPSK needs [EST].
- So a fractional-N clock generator is adequate.

**Swing ambiguity [UNK]:**
- LVDS VOD 250–450 mV gives 0.5–0.9 V p-p differential. That fits only if "0.4–1 V differential" means peak-to-peak.
- LVPECL is 0.55–0.9 V single-ended p-p (Si510), or 1.2–2.0 V across the pair (SiT9121). It fits a peak-differential reading and exceeds a p-p one.
- TI's EVM uses LVPECL levels through a transformer (§3.2). **Ask TI E2E or measure on hardware before committing.**
- Design so both are possible: AC coupling, 100 Ω differential termination, and a pad or attenuator footprint.

**Candidates (examples, not selections):**

| Role | Part | Key facts | Source |
|---|---|---|---|
| Fixed oscillator | SiTime SiT9121 | 1–220 MHz, any frequency; LVDS or LVPECL; 45–55 % duty; 0.6 ps rms (12 kHz–20 MHz) | SiT9121 datasheet |
| Fixed oscillator | Skyworks Si510/511 | 100 kHz–250 MHz; LVPECL, LVDS or HCSL; 48–52 % duty; 1.8/2.5/3.3 V | Si510/511 Rev 1.4 |
| Clock generator | TI CDCE6214 | Crystal 10–50 MHz or clock input 10–200 MHz; outputs 24 kHz–328 MHz, LVDS-like / LP-HCSL / LVCMOS; VOD 0.25–0.45 V; 350 fs integer mode, 1.7 ps fractional; $2.772 at 1ku | SNAS811A |
| LVCMOS fan-out | TI LMK1C1104 | 1:4, ≤ 250 MHz at 3.3 V, < 50 fs additive; $1.236 at 1ku | SNAS791D |
| LVDS fan-out | TI LMK1D1204 | ≤ 2 GHz; VOD 250–450 mV with a 400–650 mV boost option; 1.71–3.465 V; $3.638 at 1ku | SNAS815C |
| LVDS fan-out | TI CDCLVD1204 | 2:4, ≤ 800 MHz, **2.5 V-only** supply | SCAS898C |
| Mixed fan-out | TI LMK00304 | LVPECL/LVDS/HCSL outputs plus LVCMOS REFout | SNAS577G |
| TCXO | SiTime SiT5356 | 1–60 MHz, ±0.1–0.25 ppm, LVCMOS or clipped sine | SiT5356 datasheet |

**Using a synthesizer output as DACCLK:**
- On the LMX2572, RFoutB can carry the SYSREF divider continuously: f = f_VCO / (4 · pre · post).
- Example: 1.28 GHz LO from a 5.12 GHz VCO, with pre = 2 and post = 20, gives 32 MHz, phase-locked to the LO [EST from SNAS740B Eq. 7].
- For a 2.25 GHz LO (4.5 GHz VCO) there is no integer solution for 32 MHz.
- SYSREF duty, jitter and common mode (1.9–2.3 V; needs AC coupling) are not specified for clock use [UNK]. **Do not rely on it.**
- ADF4351 RFOUTB is not independent of RFOUTA (same divided output or the VCO fundamental) [DS], so it cannot supply a separate DACCLK.

**Sharing one reference:**
- Yes, one reference can feed both the synthesizer and the DAC clock.
- LMX2572 OSCin accepts 5–250 MHz at 0.3–3.6 V single-ended; ADF4351 REFIN accepts 10–250 MHz [DS].
- The RP2350 accepts ≤ 50 MHz CMOS on XIN with XOUT floating, V_IH ≥ 0.65·IOVDD [DS: RP2350 §8.2, p. 555–556; Table 1439, p. 1341].
- A 40 MHz TCXO through an LMK1C1104 serves all three (see §1.2).

### 3.4 LO synthesizers for 1.28 GHz and 2.2–2.3 GHz
**Target:** AFE7071 LO input −5 to +5 dBm [DS]; TI's nominal is +4 dBm, with suppression sensitive to ±1 dB [DS: SLOA313]. The EVM drives LOP single-ended and AC-terminates LON in 50 Ω [DS: SLOU337A].

TI prices are 1ku from ti.com (2026-09-29). ADI status and prices come from search snippets of analog.com and were not verified.

| Part | Covers 1.28 / 2.2–2.3 GHz | Output | Normalized FOM / 1/f (dBc/Hz) | Ref input | Supply | Status, price | Notes |
|---|---|---|---|---|---|---|---|
| TI LMX2572 | yes / yes (12.5 MHz–6.4 GHz) | 2 × differential push-pull. About +3 dBm per side at OUTx_PWR = 31, +9 dBm at maximum, both near 1.3 and 2.25 GHz (read from Fig. 165) | −232 / −123.5 | 5–250 MHz | 3.0–3.5 V, ~75–86 mA | ACTIVE, $21.34 | H3 about −13 dBc, so a harmonic LPF is needed. Two outputs. SYSREF trick above |
| TI LMX2572LP | yes / **no** (12.5 MHz–2 GHz) | +5 dBm at 2 GHz | −232 / −123.5 | 5–250 MHz | 3.3 V, 70 mA | ACTIVE, $6.68 | TI's recommended LO for AFE7070 (SLOA313). No SYSREF |
| TI LMX2582 | yes / yes (20–5500 MHz) | 2 × differential, ~+8 dBm high setting | −231 / −126 | ≤ 1400 MHz | 3.3 V, ~250 mA | ACTIVE, $9.24 | 47 fs rms at 1.8 GHz |
| TI TRF3765 | yes / yes (0.3–4.8 GHz) | 4 × differential open-collector, ≤ +6.5 dBm | −221 (integer-mode floor) | 0.5–350 MHz | 3.3 V | ACTIVE, $8.65 | Cannot make a 32/64 MHz DACCLK |
| TI LMX2594 | yes / yes (10 MHz–15 GHz) | Open-collector, needs pull-up | −236 / −129 | 5–1400 MHz | 3.3 V, ~340 mA | ACTIVE, $56.36 | Overkill |
| TI LMX2491 | PLL only, external VCO | — | −227 / −120 | — | — | ACTIVE, $4.04 | FMCW ramp PLL; not a fit |
| ADI ADF4351 | yes / yes (35–4400 MHz) | −4 to +5 dBm in 3 dB steps; open-collector with 50 Ω pull-ups | −220 / −116 | 10–250 MHz | 3.0–3.6 V | Production, ~$10.56 (snippet) | Widely used; RFOUTB not independent; H3 −13 dBc |
| ADI ADF4356 | yes / yes (53 MHz–6.8 GHz) | — | −227 / −121 | ≤ 250 MHz | 3.3 V + 5 V | [UNK] | Needs a 5 V rail |
| ADI ADF4371 | yes / yes (62.5 MHz–32 GHz) | — | −234 / −127 | — | 3.3 V + 5 V | Production, ~$247.64 (snippet) | Overkill |
| ADI ADF4368 | yes / yes (0.8–12.8 GHz) | Power specified only at 4–12.8 GHz | −239 / −147 | 10–4000 MHz | 3.3 V + 5 V | Recommended for new designs, ~$109.51 (snippet) | Overkill; power at 1.28/2.25 GHz [UNK] |

**Phase-noise class [EST]:**
- In-band floor ≈ FOM + 20·log10(f_out/f_PD) + 10·log10(f_PD). This ignores VCO, reference and flicker noise.
- At 2.25 GHz with f_PD = 25 MHz: about −119 dBc/Hz (LMX2572), −123 (LMX2594), −124 (ADF4368), −108 (ADF4351).
- Integrating −108 dBc/Hz over a 100 kHz loop bandwidth gives −58 dBc, i.e. ≈ 1.8 mrad rms (0.1°) of phase error. That is negligible for 8 Msym/s QPSK.
- The selection will be decided by coverage, harmonics and filtering, output-power stability over temperature, fractional spurs and supply rails, not by the in-band floor.
- Spur levels at the chosen channel plans are [UNK] until the channel is fixed.

### 3.5 Level compatibility (RP2350 at IOVDD = 3.3 V ↔ AFE7071 at IOVDD = 3.3 V)
- **RP2350 → AFE7071:** RP2350 V_OH ≥ 2.62 V and V_OL ≤ 0.5 V [DS: RP2350 Table 1436, p. 1339–1340]. The AFE7071 needs V_IH ≥ 2.3 V and V_IL ≤ 1.0 V. Margins are 0.32 V high and 0.5 V low. Compatible.
- **AFE7071 → RP2350 (CLK_IO in dual-output mode, SDO):** AFE7071 V_OH ≥ 2.64 V, V_OL ≤ 0.73 V at 2 mA; ≤ 0.2 V at 100 µA. RP2350 needs V_IH ≥ 2.0 V and V_IL ≤ 0.8 V. The low-side margin is only 0.07 V at a 2 mA load, but an RP2350 input draws ≤ 1 µA, so the 100 µA figure applies. Compatible.
- **Alternative supply:** both parts also support 1.8 V I/O. RP2350 IOVDD is 1.8–3.3 V nominal [DS: §6.1.1, p. 441]. The AFE7071 IOVDD minimum is 1.71 V.
- A shared 1.8 V bank would lower edge rates and ground bounce on the 14-bit bus [EST]. It would need the CM5 GPIO_VREF at 1.8 V as well, or a level translator on the host link.
- **Timing budget, not levels, is the open item:** 1 ns setup / 1 ns hold on CLK_IO versus RP2350 PIO output skew [UNK]. It must be checked on hardware.

### 3.6 Still open for Q2
- Live stock at TI and distributors, and whether an AFE707xEVM can still be obtained.
- The DACCLK swing definition (peak or peak-to-peak) and input bias.
- LO match: how to present a differential push-pull output (LMX2572) to LO_P/N. Options are single-ended with the other side terminated, as on the EVM, or a balun. Also the harmonic LPF design at each band.
- CLK_IO timing closure: 1 ns setup / 1 ns hold against RP2350 PIO output skew at 32–64 MHz.

---

## Sources
All were accessed 2026-09-29 unless dated otherwise. Page and line numbers are the ones checked. Local copies are in `sources/`.

**Raspberry Pi and RP2350**
- RP2350 datasheet, build 2025-07-29 (local `sources/rp2350-datasheet.pdf`) — https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf. Sections cited: ch. 1 p. 13; §5.2.8.1 p. 375; §6.1.1 p. 441; §8.1 p. 519; §8.2 pp. 555–556; §8.6.3 p. 576; §11.1 p. 876; §11.2 p. 883; §11.5.6.3 p. 913; §12.3.4.4 p. 1050; §12.7 p. 1141; §12.11 p. 1202; Tables 1436/1439 pp. 1339–1341; RP2350-E9 p. 1366.
- RP1 peripherals, v1.1, build 2023-11-07 — https://datasheets.raspberrypi.com/rp1/rp1-peripherals.pdf (§2.5, §3.1 Table 4, §3.6, ch. 9)
- CM5 datasheet, Release 3 — https://datasheets.raspberrypi.com/cm5/cm5-datasheet.pdf (§2.9, §2.9.1 Table 1, §2.9.2 Table 2)
- Raspberry Pi SPI documentation — https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/raspberry-pi/spi-bus-on-raspberry-pi.adoc
- Raspberry Pi news, "piolib: a userspace library for PIO control" (2024-12-17) — https://www.raspberrypi.com/news/piolib-a-userspace-library-for-pio-control/

**Linux kernel and piolib source (branch rpi-6.18.y)**
- `drivers/spi/spi-dw-core.c` — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/spi/spi-dw-core.c
- `drivers/spi/spi-dw-dma.c` — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/spi/spi-dw-dma.c
- `drivers/spi/spidev.c` (L86–87) — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/spi/spidev.c
- `drivers/misc/rp1-pio.c` (L79–80, L1047–1052, L1252–1309) — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/misc/rp1-pio.c
- `arch/arm64/boot/dts/broadcom/rp1.dtsi` — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/arch/arm64/boot/dts/broadcom/rp1.dtsi
- `drivers/clk/clk-rp1.c` — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/clk/clk-rp1.c
- `drivers/gpu/drm/rp1/rp1-dpi/rp1_dpi.c` — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/gpu/drm/rp1/rp1-dpi/rp1_dpi.c
- Overlays README — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/arch/arm/boot/dts/overlays/README
- `smi-overlay.dts` — https://github.com/raspberrypi/linux/blob/rpi-6.18.y/arch/arm/boot/dts/overlays/smi-overlay.dts
- piolib README — https://github.com/raspberrypi/utils/blob/master/piolib/README.md
- piolib `pio_rp1.c` — https://github.com/raspberrypi/utils/blob/master/piolib/pio_rp1.c

**Issues, PRs and forum threads**
- raspberrypi/linux#6020 (SPI clock and SCLK duty, pelwell) — https://github.com/raspberrypi/linux/issues/6020
- raspberrypi/linux#5865 (SPI FIFO/DMA) — https://github.com/raspberrypi/linux/issues/5865
- raspberrypi/linux PR #6994 (PIO DMA performance) — https://github.com/raspberrypi/linux/pull/6994
- raspberrypi/linux PR #7132 (RP1 SPI target mode) — https://github.com/raspberrypi/linux/pull/7132
- raspberrypi/linux#7642 (PIO TX DMA hang) — https://github.com/raspberrypi/linux/issues/7642
- raspberrypi/linux#7540 (PIO RX corruption fix) — https://github.com/raspberrypi/linux/issues/7540
- raspberrypi/utils#116 (PIO DMA throughput: jepler, pelwell) — https://github.com/raspberrypi/utils/issues/116
- Forum t=395303 (PhilE, 2026-01-12, "2MB/s is well within…") — https://forums.raspberrypi.com/viewtopic.php?t=395303
- Forum t=360436 (PhilE, SPI clock limits) — https://forums.raspberrypi.com/viewtopic.php?t=360436
- Forum t=390556 (PIO blocking get / DAC rate) — https://forums.raspberrypi.com/viewtopic.php?t=390556
- Forum t=377694 (RP2350 PIO SPI master limit) — https://forums.raspberrypi.com/viewtopic.php?t=377694

**USB, Ethernet**
- USB 2.0 specification, Table 5-9 p. 54 — https://www.usb.org/document-library/usb-20-specification. The copy read was http://bitsavers.trailing-edge.com/components/usb/USB_2.0_2000.pdf.
- WIZnet W5500 datasheet v1.1.0 (SPI timing, PDF p. 61) — https://docs.wiznet.io/img/products/w5500/W5500_ds_v110e.pdf

**AFE7071 / AFE7070 (TI)**
- AFE7071 datasheet SLOS789C (local `sources/afe7071-rev-c.pdf`) — https://www.ti.com/lit/ds/symlink/afe7071.pdf. Pages cited: p. 4 absolute maximum; p. 5 DC, digital and DACCLK; p. 6 LO; p. 17 CONFIG0; pp. 25–28 clock modes.
- AFE7071 product page (status, price) — https://www.ti.com/product/AFE7071
- AFE7071IRGZT part details (purchase-limit note) — https://www.ti.com/product/AFE7071/part-details/AFE7071IRGZT
- AFE7070 product page — https://www.ti.com/product/AFE7070
- AFE707xEVM user guide SLOU337A — https://www.ti.com/lit/ug/slou337/slou337.pdf
- EVM tool pages (HTTP 404 on 2026-09-29) — https://www.ti.com/tool/AFE7070EVM and https://www.ti.com/tool/AFE7071EVM
- SLOA313, "AFE7070 Optimized Operation in the VHF Band" — https://www.ti.com/lit/an/sloa313/sloa313.pdf
- TI E2E 1107145 (LMX2572LP LO filter for AFE7071; search snippet only) — https://e2e.ti.com/support/rf-microwave-group/rf-microwave/f/rf-microwave-forum/1107145/afe7071-low-pass-filter-when-using-lmx2572lp-for-lo-with-afe7071
- Microwave Journal, 2012 launch prices — https://www.microwavejournal.com/articles/18779-tis-tiny-analog-front-ends-shrink-test-and-measurement-wireless-communications-optical-networking-systems
- DigiKey AFE7071IRGZT (not verified live; snippet only) — https://www.digikey.com/en/products/detail/texas-instruments/AFE7071IRGZT/3767572
- CDCM7005 product page — https://www.ti.com/product/CDCM7005

**Synthesizers**
TI parts: status and prices are from their ti.com/product pages.
- LMX2572, SNAS740B — https://www.ti.com/lit/ds/symlink/lmx2572.pdf
- LMX2572LP, SNAS764 — https://www.ti.com/lit/ds/symlink/lmx2572lp.pdf
- LMX2582, SNAS680E — https://www.ti.com/lit/ds/symlink/lmx2582.pdf
- LMX2594, SNAS696C — https://www.ti.com/lit/ds/symlink/lmx2594.pdf
- LMX2595, SNAS736C — https://www.ti.com/lit/ds/symlink/lmx2595.pdf
- LMX2491, SNAS711A — https://www.ti.com/lit/ds/symlink/lmx2491.pdf
- TRF3765, SLWS230E — https://www.ti.com/lit/ds/symlink/trf3765.pdf

ADI parts: analog.com timed out on a recheck on 2026-09-29. Datasheets were read from the analog.com data-sheet library, and status and prices come from search snippets of the product pages.
- ADF4351 Rev. A — https://www.analog.com/media/en/technical-documentation/data-sheets/ADF4351.pdf; product page https://www.analog.com/en/products/adf4351.html
- ADF4356 Rev. B — https://www.analog.com/media/en/technical-documentation/data-sheets/ADF4356.pdf
- ADF4371 Rev. A — https://www.analog.com/media/en/technical-documentation/data-sheets/adf4371.pdf
- ADF4368 Rev. 0 — https://www.analog.com/media/en/technical-documentation/data-sheets/adf4368.pdf

**Clock parts**
- CDCE6214, SNAS811A — https://www.ti.com/lit/ds/symlink/cdce6214.pdf
- LMK1C1104, SNAS791D — https://www.ti.com/lit/ds/symlink/lmk1c1104.pdf
- LMK1D1204, SNAS815C — https://www.ti.com/lit/ds/symlink/lmk1d1204.pdf
- CDCLVD1204, SCAS898C — https://www.ti.com/lit/ds/symlink/cdclvd1204.pdf
- LMK00304, SNAS577G — https://www.ti.com/lit/ds/symlink/lmk00304.pdf
- SiTime SiT9121 — https://www.sitime.com/datasheet/SiT9121
- SiTime SiT5356 — https://www.sitime.com/datasheet/SiT5356
- Skyworks Si510/511 Rev 1.4 — https://www.skyworksinc.com/-/media/Skyworks/SL/documents/public/data-sheets/si510-11.pdf

**Project files**
- `../backup/original-picotool-info.txt`: bench RP2350 is revision A2, QFN80.
- `../../IREC/Pigeon_Vision/DESIGN.md` §4: the 1.28 GHz design point and the AFE7071 comparison.
