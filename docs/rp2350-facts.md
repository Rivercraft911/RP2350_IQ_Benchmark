# RP2350 facts for the I/Q waveform generator (16 MS/s complex → PIO → AFE7071)

Sources: RP2350 datasheet, build-date 2025-07-29, build d126e9e (local `sources/rp2350.txt`). The online copy fetched 2026-09-29 is the same build and has the same errata (E1–E28). Citations are (§section, p.printed-page). Other sources: Arm Cortex-M33 TRM r1p0, doc 100230_0100_03_en ("TRM"); pico-sdk master @079c6f3 ("SDK"). "Derived" means my own arithmetic from the cited constraints. "UNVERIFIED" means neither source states it.

## Implications for this design

1. **clk_sys = 128 MHz** (12 MHz XOSC, REFDIV 1, FBDIV 128, VCO 1536 MHz, POSTDIV 6/2) is the only practical in-spec (≤150 MHz) PLL setting that gives whole numbers of PIO cycles at both word rates: 4 per word at 32 MW/s, 2 per word at 64 MW/s, and 8 CPU cycles per complex sample at 16 MS/s. 64 MHz also divides both rates evenly, but it leaves only 4 CPU cycles per complex sample. 96 MHz gives 3 per word at 32 MW/s only. At 144 or 150 MHz the ratio is 4.5 or 4.69 cycles per word, so either the fractional divider or an uneven program adds 1 clk_sys cycle (6.9 or 6.7 ns) of edge jitter. 160 and 192 MHz are overclocks and outside the spec (derived; §8.6.3 p576, §11.5.5 p911).
2. **PIO program:** one state machine (SM). OUT drives 15 contiguous pins (D13:0 + IQ_FLAG) and side-set drives CLK_IO. `out pins,16 side 0 [1]` / `nop side 1 [1]` takes 4 cycles per word; dropping the delays gives 2. Pack I|Q into one 32-bit word, use autopull threshold 32 and FJOIN_TX (8-deep FIFO). The sticky FDEBUG.TXSTALL flag detects any underrun (§11.5.1 p902, §11.5.4 p907–910, Table 984 p946).
3. **DMA load:** 16 M 32-bit writes/s into the TX FIFO is 1 write per 8 clk_sys cycles at 128 MHz, against a DMA/PIO limit of 1 word per clock. All three PIO FIFOs, the DMA control registers, USB and the HSTX FIFO share one FASTPERI crossbar port, so avoid CPU polling of PIO/DMA registers (§2.1 p24, §11.1 p877, §11.5.3 p906).
4. **CPU budget:** 8 cycles per complex sample on one core at 128 MHz (16 per sample per core if both cores work). Costs: LDR 1 cycle + 1 result delay, STR 1, UBFX/BFI with no penalty, taken branch ≥2 (so unroll). The SIO interpolator can act as phase accumulator plus LUT address generator with one POP per sample (§3.7.4.9 p138–142, §3.1.10 p45–52).
5. **Placement:** run hot code from SRAM, never XIP (SDK `.time_critical*` → RAM). Put the LUT in one striped half (SRAM4–7, 0x20040000+) and the DMA ring in the other (SRAM0–3). Stacks go in SRAM8/9 (SDK SCRATCH_X/Y). Each bank serves 1 access per cycle, and instruction fetch and data loads to the same bank collide (§4.2 p337–338, §3.7.4.9.12 p144, SDK).
6. **Streaming:** use a naturally aligned DMA ring (RING_SIZE ≤ 32 kB) with TRANS_COUNT.MODE = 0xF (endless), or 0x1 (trigger-self) to get periodic refill IRQs, or a CHAIN_TO ping-pong pair. Work around E5/E8 (§12.6.2.2.1 p1098, Table 1151 p1128).
7. **Bus priority:** because of E27, DMA-write priority at FASTPERI (the PIO FIFOs) is controlled by BUS_PRIORITY.PROC0, not DMA_W. Check the effect with the BUSCTRL counters FASTPERI_/SRAMn_ACCESS_CONTESTED (p1363–1364, §12.15.4 p1255–1258).
8. **Clock locking:** the PLL reference is XOSC/XIN only. To lock to the AFE7071 system, drive a common CMOS reference (≤50 MHz, ≥5 MHz after REFDIV) into XIN. GPIN pins (GPIO12/14/20/22) can clock clk_sys only directly (≤50 MHz, not through the PLL). GPOUT pins (≤50 MHz) can give 32 MHz = clk_sys/4, but 64 MHz exceeds the GPOUT spec, so generate 64 MHz with PIO side-set or the HSTX clock generator (§8.1.2.4 p517, §8.1 p514, Fig. 40 p575).
9. **GPIO setup:** SLEWFAST=1, drive 8/12 mA, IE/ISO handled by gpio_set_function(), IOVDD equal to the AFE7071 IOVDD (1.8 V needs VOLTAGE_SELECT=1). Worst-case clk→pad delay is ≤4.1 ns (3.3 V) or ≤5.4 ns (1.8 V), and Bank-0 skew is ≤2.1 ns (QMI tables), against a 15.6 ns period at 64 MW/s. There is no PIO-specific timing spec (Table 1291/1292 p1233–1234).
10. **Errata:** E9 (A2 only, fixed in A3) affects only inputs that rely on the internal pull-down (e.g. CLK_IO in, ALARM). E2: don't use SIO spinlocks, and note that writing MTIME_CTRL releases SPINLOCK9. E1: don't use interpolator OVERF. No PIO or clock/PLL errata exist. Benchmark with DWT_CYCCNT (check DWT_CTRL.NOCYCCNT==0 at runtime) and the 24-bit BUSCTRL counters, which saturate after ~131 ms of per-cycle events at 128 MHz (Appx E p1357–1376).

---

## 1. Clocks

- **Max frequencies:** clk_sys 150 MHz; clk_peri 12–150 MHz; clk_hstx 150 MHz; clk_usb/clk_adc 48 MHz (§1.1 p13–14, Table 541 p518–519, §8.6.1 p575). clk_ref max 25 MHz (§8.2.2 p556).
- **Core voltage:**
  - DVDD min/typ/max 1.05/1.10/1.16 V; transients ±100 mV (Table 1441 p1343). Abs max 1.21 V (Table 1433 p1338).
  - VREG default 1.10 V (VSEL 01011); capped at 1.30 V unless DISABLE_VOLTAGE_LIMIT is set (§6.3.2 p449, VREG reg p465).
  - "RP2350 might not operate reliably with … DVDD at a voltage other than 1.1 V" (§6.3.2 p449).
- **Overclocking:** the datasheet gives no spec above 150 MHz. Its maxima are worst-case PVT, and "most chips … can run significantly faster" (§8.1.2.2.5 p516). Operation above 150 MHz is UNVERIFIED.
- **PLL constraints** (§8.6.3 p576):
  - FOUT = (FREF/REFDIV)·FBDIV/(PD1·PD2).
  - FREF/REFDIV ≥ 5 MHz and ≤ VCO/16.
  - VCO 750–1600 MHz; FBDIV 16–320 (integer only); PD1, PD2 1–7.
  - REFDIV is 6 bits (1–63) (Fig. 40 p575, CS p584). FBDIV has no fractional mode (FBDIV_INT p584).
  - Put the larger post-divider in PD1 for lower power (p576). Higher VCO gives lower jitter (§8.6.3.1 p576).
  - With 12 MHz, FBDIV must be 63–133 (p576).
  - The CS register text says "VCO min 400 MHz, ref max 800 MHz" (p583). This conflicts with p576; use 750–1600.
- **PLL settings from 12 MHz, REFDIV=1** (derived; all satisfy p576):

  | clk_sys | Preferred (highest VCO) | Alternatives |
  |---|---|---|
  | 128 MHz | FBDIV 128, VCO 1536, PD 6/2 (or 4/3) | FB 96 / VCO 1152 / 3·3; FB 64 / VCO 768 / 6·1 |
  | 144 MHz | FBDIV 120, VCO 1440, PD 5/2 | FB 108 / 1296 / 3·3; FB 96 / 1152 / 4·2; FB 72 / 864 / 6·1 |
  | 150 MHz | FBDIV 125, VCO 1500, PD 5/2 (SDK default, p581) | FB 100 / 1200 / 4·2; FB 75 / 900 / 6·1 |
  | 96 MHz | FB 128 / 1536 / 4·4 | FB 120 / 1440 / 5·3 |
  | 160 MHz (overclock) | FB 120 / 1440 / 3·3 | — |
  | 192 MHz (overclock) | FB 128 / 1536 / 4·2 | — |

  SDK overrides: PLL_SYS_REFDIV / PLL_SYS_VCO_FREQ_HZ / PLL_SYS_POSTDIV1/2 (§8.6.3.2 p580).
- **PLL operation:** LOCK bit and LOCK_N interrupt (§8.6.2 p575, CS p583–584). Programming sequence on p582. Output is unusable during REFDIV/postdiv/bypass changes (§8.1.2.6 p518).
- **Clock generator divider:** fractional divide 1.0–2^16. It toggles between two integer divisors, so the output jitters (e.g. ÷2.4 = 3×÷2 + 2×÷3). DC50 corrects duty for odd divisors (§8.1.3.3–8.1.3.4 p520; CLK_SYS_DIV INT16.FRAC16, Table 560 p539).
- **External clock on XIN:**
  - CMOS square wave up to 50 MHz with XOUT floating. VIH ≥ 0.65·IOVDD, VIL ≤ 0.35·IOVDD (§8.2 p555, §8.2.2 p556, Table 1439 p1341–1342).
  - The PLLs can multiply XIN (§8.1.2.4 p517). PLL FREF is wired to XOSC/XIN (Fig. 40 caption p575; SDK comment p581).
  - The PLL needs ≥5 MHz (p555). clk_sys can also take XOSC directly (AUXSRC 0x3, Table 559 p538).
  - A non-12 MHz XIN breaks default USB BOOTSEL; configure OTP BOOTSEL_PLL_CFG/XOSC_CFG (§5.2.8.1 p375).
  - Examples (derived): 16 MHz ref → FBDIV 96; 32 MHz ref → FBDIV 48; both give VCO 1536 and PD 6/2 = 128 MHz.
- **GPIN:**
  - GPIN0 on GPIO12/20, GPIN1 on GPIO14/22 (Table 3 p18).
  - Can drive clk_sys (AUXSRC 0x4/0x5, Table 559 p538) and clk_ref (Table 556 p537), limited to 50 MHz. At worse than 1000 ppm, don't run generated clocks at max (§8.1.2.4 p517).
  - GPIN cannot be a PLL reference (p575), so no ×N lock via GPIN.
- **GPOUT:**
  - clk_gpout0 on GPIO13/21, gpout1 on GPIO15/23, gpout2 on GPIO24, gpout3 on GPIO25 (Table 3 p18–19). Max 50 MHz (§8.1 p514).
  - Sources: PLL_SYS, GPIN0/1, PLL_USB, ROSC, XOSC, LPOSC, CLK_SYS/USB/ADC/REF/PERI/HSTX (Table 544 p531–532).
  - Divider INT16.FRAC16 (Table 545 p532). PHASE delays the enable by 0–3 input cycles; NUDGE shifts phase by 1 input cycle (Table 544 p531).
  - Phase relationship between GPOUT and PIO output: UNVERIFIED.
- **Measurement:** the FC0 frequency counter measures internal and external clocks, with 1 µs–32 ms intervals (§8.1.4 p522).
- **Boot clock:** on A3+, boot clk_sys is ~48 MHz from the ROSC (Appx C p1355).

## 2. PIO

- **Blocks:** 3 PIO blocks × 4 SMs = 12 SMs. Each block has a 32-instruction memory with 4 read ports, shared by its SMs (§11.1 p876, p878; §11.2.8 p885).
- **Execution:**
  - 1 instruction per clk_sys cycle per SM. Every instruction takes 1 cycle unless it stalls. Up to 31 delay cycles (§11.2.2 p879; §11.4.1 p889).
  - PIO runs from clk_sys; the divider is a clock enable (§11.5.5 p911).
  - `.wrap` is a zero-cycle jump (§11.5.2 p904).
- **FIFOs:**
  - 4×32-bit TX + 4×32-bit RX per SM. FJOIN_TX/RX gives 8 deep in one direction. Changing FJOIN flushes (§11.2.4.4 p883, §11.5.3 p905–906, Table 997 p951).
  - "8 FIFO entries is sufficient for 1 word per clock through the RP2350 system DMA, provided the DMA is not slowed by contention" (p906). DMA interface sustains ≤1 word/clock (§11.1 p877).
- **Autopull:**
  - PULL_THRESH 1–32 (0 = 32) (Table 997 p951).
  - The OSR refills in the same cycle as the OUT that hits the threshold, if the FIFO has data, so there is no stall (§11.4.5.2 p894, §11.5.4.2 p910).
  - If the threshold is reached and TX is empty, the OUT stalls. The hardware "cannot fill an empty OSR and OUT it on the same cycle", so an underrun costs at least 1 extra cycle (p910).
  - With autopull, PULL is a no-op when OSR is full; MOV from OSR is undefined (p910).
- **Stall behaviour:**
  - Conditions listed in §11.2.5 p884. The PC holds, and delay cycles start after the stall clears (p884).
  - Side-set still happens on the first cycle of a stalled instruction (p884, p903).
- **Debug flags:** FDEBUG.TXSTALL (sticky, per SM) sets on a stall from an empty TX FIFO during blocking PULL or autopull OUT. TXOVER sets on system write-on-full (Table 984 p946–947). FLEVEL gives FIFO levels (p947).
- **Autopush:** single cycle unless the RX FIFO is full; must not be combined with FJOIN_RX_PUT/GET (§11.4.4 p893, §11.5.4.1 p909–910).
- **Side-set:**
  - ≤5 pins; SIDE_EN uses the MSB as an enable, leaving ≤4 data bits. Side-set bits come out of the 5-bit delay field (§11.5.1 p902–903, Table 996 p950, SIDESET_COUNT 0–5 Table 1000 p953).
  - Drives values or pindirs (SIDE_PINDIR). Beats OUT/SET on the same pin in the same SM (p903, p912).
- **Outputs per instruction:** OUT ≤32 pins (OUT_COUNT 0–32), SET ≤5. OUT/SET and side-set can happen in the same cycle (§11.5.6 p911–912, Table 1000 p953).
- **Pin mapping:**
  - OUT, SET, IN and side-set each use a contiguous base+count range. Ranges may overlap (§11.2.6 p884).
  - OUT LSB goes to OUT_BASE and continues for OUT_COUNT pins, wrapping after GPIO31 in PIO-relative numbering (§11.5.6 p912).
  - When several SMs write the same pin, the highest-numbered SM wins, per pin (§11.5.6.1 p912–913). OUT_STICKY and INLINE_OUT_EN are available (Table 996 p950).
- **GPIOBASE:**
  - Per block, values 0 or 16 only (bit 4). Each block sees 32 GPIOs at a time (§11.1.1 p877, Table 1017 p956).
  - Derived: with GPIOBASE=16, the window is GPIO16–47 and OUT wraps GPIO47→GPIO16.
  - §11.1 p877 and §11.2.6 p884 still say "30 GPIOs" (stale wording).
  - PIO0/1/2 are functions F6/F7/F8 on every Bank-0 GPIO. Inputs are always visible (Table 3 p18–20, Table 4 p21).
- **Clock divider:**
  - 16-bit INT + 8-bit FRAC (1/256 steps), range 1–65536. INT=0 means 65536; if INT=0, FRAC must be 0 (Table 995 p950).
  - Integer n → the SM runs 1 cycle in every n. The fractional part is first-order delta-sigma, stretching some periods from n to n+1 cycles. "For small n, the jitter … may be unacceptable" (§11.5.5 p911).
- **Synchronisation:**
  - CTRL.CLKDIV_RESTART puts dividers in lockstep. SM_ENABLE bits start several SMs in one write.
  - NEXTPREV_SM_ENABLE / NEXTPREV_CLKDIV_RESTART start or sync SMs in neighbouring PIO blocks simultaneously (Table 982 p944–946).
  - 8 IRQ flags per block; cross-PIO IRQs have no delay penalty (p878, §11.2.7 p884).
- **DREQ numbers:** PIO0 TX0–3 = 0–3, RX = 4–7; PIO1 TX = 8–11, RX = 12–15; PIO2 TX = 16–19, RX = 20–23 (Table 1146 p1101). DREQ latency is one cycle less than RP2040 (p878); the absolute value is UNVERIFIED.
- **Input synchroniser:**
  - A 2-flip-flop synchroniser on each GPIO input adds 2 cycles of latency. INPUT_SYNC_BYPASS bit n bypasses it per GPIO (§11.5.6.3 p913, Table 990 p948).
  - UNVERIFIED: residual latency when bypassed, and whether the bit index is GPIOBASE-relative.
- **System IRQs:** PIO0_IRQ_0/1 = 15/16, PIO1 = 17/18, PIO2 = 19/20 (Table 95 p82–83).
- **Throughput and timing figures:** DPI example sustains 360 Mb/s from a 48 MHz clk_sys (§11.1 p877). "Improved GPIO input/output delay and skew" vs RP2040 gives no numbers (p878).

## 3. GPIO electrical

- **IOVDD and thresholds:**
  - IOVDD 1.62–3.63 V (typ 1.8 or 3.3). One supply for all GPIOs (Table 1441 p1343, §6.1.1 p441).
  - For 1.8 V, set PADS VOLTAGE_SELECT=1. Default 0 is valid for 2.5–3.3 V. Running >1.8 V with the 1V8 setting may damage the chip (§9.6 p595, §6.1.1 p441, Table 852 p787).
  - Bank 0 and QSPI have separate registers; set both the same (p595).
- **Pad control (reset values):** DRIVE 2/4/8/12 mA (reset 4 mA), SLEWFAST (reset 0 = slow), SCHMITT (reset 1), PDE (reset 1), PUE, IE (reset 0), OD, ISO (reset 1) (Table 853 p787, §9.6 p595). PUE+PDE together = bus keeper (§9.6.1 p596).
- **DC levels** (Table 1436 p1339–1340):
  - VOH min: 1.24 V @1.8, 1.78 V @2.5, 2.62 V @3.3, at IOH = selected drive.
  - VOL max: 0.3 / 0.4 / 0.5 V.
  - VIH min: 0.65·IOVDD / 1.7 / 2.0 V. VIL max: 0.35·IOVDD / 0.7 / 0.8 V. Hysteresis 0.1·IOVDD / 0.2 V.
  - Pull-up 32–86 kΩ, pull-down 36–113 kΩ @3.3 V.
  - Total source current ≤100 mA; total sink current ≤100 mA.
  - FT pins tolerate 5.5 V with IOVDD = 3.3 V (Table 1433 p1339).
- **Max toggle frequency:** not specified (UNVERIFIED). Indirect evidence:
  - HSTX 150 MHz DDR = 300 Mb/s per pin on GPIO12–19 (§12.11 p1202–1203).
  - Trace clock 75 MHz DDR on GPIO1–5 (§3.5 p88–89, Table 3 p18).
  - GPOUT ≤50 MHz (§8.1 p514).
- **Output timing** (QMI AC tables, worst PVT):
  - clk_sys→GPIO output max: QFN-60 3.5 ns @3.3 V / 4.9 ns @1.8 V; QFN-80 4.1 / 5.4 ns (Table 1292 p1233–1234).
  - Bank-0 GPIO output skew (QMI context): QFN-60 typ 1080 / max 1725 ps; QFN-80 typ 1280 / max 2100 ps (Table 1291 p1233).
  - HSTX output delays are balanced within 300 ps (p1202).
  - No PIO-path delay/skew spec (UNVERIFIED for PIO).
- **Reset and isolation:**
  - After reset, Bank-0 pads are Hi-Z, input disabled, pulled low, with isolation latches latched (§9.3 p588–589).
  - Software must set IE=1 and ISO=0; gpio_set_function() does both (p589, §9.7 p596).
  - ISO bits return to 1 after switched-core power-up. A PADS block reset does not clear ISO (p596–597).
  - Code ported from RP2040 must clear ISO itself (p596).
- **Other paths:**
  - SIO GPIO (F5): GPIO_OUT covers 0–31; GPIO_HI_OUT covers 32–47, QSPI and USB.
  - DMA cannot access SIO; the recommended DMA→GPIO route is PIO (§9.8 p597).
  - The GPIO coprocessor does 64-bit GPIO read/write in one instruction (§9.9 p597, §3.6.1 p100–103).

## 4. Memory / bus

- **SRAM layout:**
  - 520 kB in 10 banks: SRAM0–7 are 64 kB each, SRAM8–9 are 4 kB each (§4.2 p337).
  - 0x20000000–0x2003FFFF is word-striped over SRAM0–3 (addr[3:2]); 0x20040000–0x2007FFFF over SRAM4–7.
  - SRAM8 at 0x20080000 and SRAM9 at 0x20081000 are not striped. There is no non-striped mirror (§2.2.3 p31, Table 434 p338).
- **SRAM access:** each bank has its own arbiter. Access is single-cycle unless another manager hits the same bank in the same cycle. Up to six 32-bit SRAM accesses per cycle, one per manager. SRAM8/9 are suggested for per-core stack and hot code (p337–338).
- **SDK (not datasheet):** SCRATCH_X = 0x20080000 (4 kB), SCRATCH_Y = 0x20081000 (4 kB). Core-0 stack sits at the end of SCRATCH_Y, core-1 stack in SCRATCH_X. `.time_critical*` links into `.data`, i.e. RAM (SDK `default_locations.ld`, `sections_stack.incl`, `section_default_data.incl`).
- **Other RAM:** XIP cache usable as SRAM (16 kB when pinned); USB DPRAM 4 kB at 0x50100000 if USB is unused, but it sits on the FASTPERI port (§4.2.1 p338).
- **Bus fabric:**
  - AHB5 crossbar: 6 managers (core0 I, core0 D, core1 I, core1 D, DMA R, DMA W), all 32-bit, up to 6 transfers per cycle (§2.1 p24).
  - ROM (1 port), XIP (2 ports), SRAM (10 ports) are symmetric to all managers.
  - FASTPERI (PIO0–2, USB, DMA CSRs, XIP DMA FIFOs, HSTX FIFO, trace FIFO) and APB are load/store + DMA only (p24).
  - SIO has a dedicated per-core path (p24–25). Peak 3.6 GB/s at 150 MHz (p25).
- **Arbitration:**
  - Two priority levels via BUS_PRIORITY (PROC0 bit 0, PROC1 bit 4, DMA_R bit 8, DMA_W bit 12), then round-robin (§2.1.1 p25, Table 1321 p1256–1257).
  - At zero-wait subordinates, high priority is never delayed by low; "low-priority managers may stall until there is a free cycle" (p25). BUS_PRIORITY_ACK (p1257).
  - E27: the priority bits are mis-wired at FASTPERI/APB (see §8).
- **Access costs:** AHB peripherals (incl. PIO FIFOs) take ≥1 cycle plus ≤1 wait state (§2.2.5 p33). APB reads ≥3 cycles, writes ≥4 (§2.1.4 p26). SIO is zero-wait (§2.2.6 p33).
- **BUSCTRL performance counters:**
  - 4 counters, 24-bit, saturating (BUSCTRL_BASE 0x40068000). Enable with PERFCTR_EN=1; write any value to PERFCTRx to clear (§2.1.7 p30, §12.15.4.2 p1255–1256).
  - Four events per downstream port:
    - ACCESS: an access completed.
    - ACCESS_CONTESTED: an access completed after being deferred by another manager's access.
    - STALL_UPSTREAM: cycles any manager stalled, including contention.
    - STALL_DOWNSTREAM: cycles the port itself was stalled.
  - Events don't distinguish reads/writes or which manager (p1255–1256).
  - PERFSEL codes: SRAMn_ACCESS_CONTESTED = 0x36−4n and SRAMn_ACCESS = 0x37−4n (n = 0–9); FASTPERI_{STALL_UP, STALL_DN, CONTESTED, ACCESS} = 0x0c–0x0f; APB 0x08–0x0b; SIOB_PROC0/1 0x04–0x07 / 0x00–0x03; XIP_MAIN0/1 0x3c–0x3f / 0x38–0x3b; ROM 0x40–0x43. PERFSEL reset = 0x1f (Table 1325 p1258–1260).
- **XIP cache:**
  - 16 kB, 2-way, 8-byte lines, 1-cycle hit, two 8 kB banks (§4.4 p341, §4.4.1 p341–342).
  - Windows: 0x10… cached, 0x14… uncached, 0x18… maintenance, 0x1c… uncached/untranslated (p341).
  - Lines can be pinned for cache-as-SRAM; eviction is random (§4.4.1.3 p344).
- **QMI (flash/PSRAM):**
  - SCK = clk_sys/(1–256). Data rate "capped at 4 bits per system clock cycle", i.e. ≤75 MB/s at 150 MHz (derived) (§12.14.1 p1227).
  - 2 chip selects × 16 MB. Cache misses are 64-bit transfers; sequential accesses can chain (p1227).
  - DMA reads from XIP stall the whole DMA; use the streaming FIFO (§4.4.3 p345).
  - Sustained PSRAM bandwidth: UNVERIFIED.

## 5. DMA

- **Channels and throughput:** 16 channels; one read + one write per cycle, each ≤32 bits (§12.6 p1094–1095). Round-robin between active channels; CTRL.HIGH_PRIORITY affects scheduling only, not bus priority (p1095, Table 1151 p1129). Sizes 8/16/32 with byte-lane replication; BSWAP (p1095, p1127).
- **TRANS_COUNT:** 28-bit count plus MODE[31:28] (§12.6.1 p1095–1096, §12.6.2.2.1 p1098):
  - 0x0: normal.
  - 0x1 TRIGGER_SELF: re-triggers on completion; still raises the IRQ and CHAIN_TO.
  - 0xF ENDLESS: no decrement.
- **Triggering:**
  - 4 register aliases, each with its own trigger register; CHAIN_TO; MULTI_CHAN_TRIGGER; a null (all-zero) trigger ends a chain; IRQ_QUIET (§12.6.3 p1098–1100, Table 1145 p1099).
  - CHAIN_TO resets to 0, so channels ≥1 chain to ch0 by default. Setting CHAIN_TO to the channel's own number disables chaining (Table 1151 p1128).
  - Ping-pong pairs pipeline configuration (p1100, p1109–1110).
- **Address modes:** RING_SIZE n wraps on a 2^n-byte boundary (2–32768 B) on read or write (RING_SEL). INCR_*_REV decrements, or steps by 2× when INCR=0 (Table 1151 p1128, p1096).
- **DREQ:**
  - Credit-based, with a 6-bit saturating counter per channel. Gives full 1 word/clock through an 8-deep FIFO with no over/underflow, absent contention (§12.6.4.2 p1101–1102).
  - One channel per DREQ. Don't touch a FIFO the DMA is servicing (p1102).
  - TREQ_SEL: 0x00–0x3a DREQ, 0x3b–0x3e TIMER0–3, 0x3f unpaced (p1127–1128).
- **Pacing timers:** 4, X/Y fractional, ≤1 request per clk_sys (§12.6.8.1 p1107, TIMERx p1135).
- **IRQs:** 4 lines, DMA_IRQ_0–3 = IRQ 10–13 (Table 95 p82). Masks INTE0–3 (§12.6.5 p1102). A bus error always raises the channel IRQ and suppresses CHAIN_TO (§12.6.7.1 p1105).
- **Other features:** CRC/sum sniffer runs at 32 bits/clock (§12.6.8.2 p1107). Abort procedure accounting for E5 (§12.6.8.3 p1108).
- **Limits:** the DMA cannot access SIO, so no interpolator access from DMA (§9.8 p597, §12.6.7 p1105). The DMA is capped at one HSTX FIFO write per clk_sys (§12.11 p1203).

## 6. SIO

- **Access:** each core has a dedicated port with zero-wait access (§3.1 p36, §2.2.6 p33).
- **Interpolators:**
  - 2 per core, INTERP0 and INTERP1, at SIO+0x080–0x0FC (§3.1.10 p44, register list p54–55).
  - Register read/write in 1 cycle; results ready the next cycle. POPx returns the result and writes the lane results back to the accumulators (p45).
  - Each lane: right-rotate by SHIFT (RP2350 changed shift to rotate, p70), mask LSB..MSB, optional sign-extend, add to BASE. CROSS_INPUT, CROSS_RESULT and ADD_RAW options (p45–47).
  - Blend mode on INTERP0 only (p48); clamp mode on INTERP1 only (p50).
  - The texture-mapping example turns LUT address generation into a "single cycle iteration" (§3.1.10.5 p52–53).
  - Secure/Non-secure assignment via PERI_NONSEC (p38). E1: OVERF is broken (p1373).
- **Mailboxes:** 2 FIFOs, 32-bit × 4 deep, one per direction. FIFO_ST has VLD/RDY/ROE/WOF. IRQ SIO_IRQ_FIFO = 25 (NS 27) (§3.1.5 p42).
- **Doorbells:** 8 each way, IRQ 26 (§3.1.6 p42–43).
- **Spinlocks:** 32 per S/NS bank (§3.1.4 p41). E2 aliasing (p42, p1373–1374); locks 5, 6, 7, 10, 11 and 18–31 are safe.
- **GPIO:** SIO GPIO registers are shared by both cores; for same-cycle writes, core 0 applies first (p40).
- **Other:** no memory-mapped divider (the M33 has hardware divide) (§3.1.7 p43). MTIME is a 64-bit timer (§3.1.8 p43).

## 7. Cortex-M33 (RP2350 build)

- **Configuration:** single-precision FPU; DSP extension; Security extension; coprocessor interface; 8+8 MPU regions; 8 SAU regions; 52 IRQs; 4 priority bits; DWT and ITM; ETM; no MTB (§3.7.2 p124–125). Double-precision coprocessor (DCP) (p36, §3.6.4 p123). UNCROSS_I_D: all fetch via the Code port, all load/store via the System port (§3.7.2.1.1 p125–126).
- **Arm TRM:**
  - The DSP option includes "Pack halfword", i.e. PKHBT/PKHTB (A1.3 p A1-20).
  - "Limited dual-issue of common 16-bit instruction pairs" (A1.4.1 p A1-23). ACTLR.DISFOLD disables dual issue (B2.2 p B2-50; RP2350 p177).
  - **The TRM has no instruction cycle table**: I checked the whole text, and the ST community thread "Cortex-M33 instruction cycle counts?" confirms Arm doesn't publish one.
- **RP2350 measured timings** (§3.7.4.9 p137–144). Conditions: one core running, no cache misses, no DMA (p138).
  - **Result-use penalty:** +1 cycle if the next instruction uses the result of LDR, the last register of LDM/POP, a multiply, or a "complex" shift/immediate op (shift other than LSL#0–3) (p138).
  - **ALU:** most ops 1 cycle (p138). SEL and complex ops incur the penalty (p138).
  - **Shifts and bitfields:** UBFX/SBFX/BFI have no penalty. LSL/LSR/ASR/ROR take 1 cycle with no penalty (p139).
  - **Multiply:** MUL/MLA 1 cycle + 1 delay. An MLA chain accumulating into the same register runs at 1/cycle (p139).
  - **Divide:** 2 to 4+n/4 cycles (p140).
  - **LDR/LDM:** 1 cycle per register, plus the result delay on the last register (p140).
  - **STR:** 1 cycle, including `[Rn,Rm,LSL#2]` (p140).
  - **Branches:** taken = base + 2 + L + U − (K&F); not taken = base + 1 − F. A folded BNE in a loop costs 1–2 cycles (p141–142).
  - **Folding:** IT and NOP (0xBF00) fold with a 16-bit neighbour (p142–143).
  - **FPU:** VADD/VSUB/VMUL 1 cycle + 1 delay; VMLA/VFMA 3 cycles; VDIV/VSQRT 14 (p143).
  - **Coprocessor:** MCR/MCRR 1 cycle; MRC/MRRC 1 cycle + 1 delay (p144).
  - **Instruction fetch:** 32 bits/cycle per bus. Fetch and data collide on the same SRAM bank; e.g. `LDR R8,[PC,#32]` can cost +1 (§3.7.4.9.12 p144).
  - **Not covered (UNVERIFIED):**
    - LDRD: the "1 cycle/register" rule suggests 2 cycles.
    - STM: per-register cost not stated.
    - PKHBT/PKHTB: not listed. By the p138 rule, the LSL#16/ASR#16 operand is "complex", so expect +1 penalty (inference).
- **SRAM vs XIP:**
  - The published timings assume no XIP misses (p138). A miss goes to QMI at ≤4 bits per clk_sys (p1227).
  - Keep the generator loop in SRAM: SDK `__time_critical_func` → `.time_critical` in RAM, or a copy_to_ram / no_flash binary (SDK; §5.1 p359).

## 8. Errata (Appx E p1357–1376; steppings in Appx C p1354–1356)

- **Steppings:** A2 = CHIP_ID.REVISION 0x2; A3 = 0x3; A4 = 0x8. A4 has no hardware changes vs A3, only bootrom changes (p1354–1355).
- **E9 GPIO leakage** (Bank-0 pads with IE=1, OE=0 and the pin between VIL and VIH): **A2 only; fixed in A3 hardware** (p1354, p1366–1368).
  - ~120 µA source current holds the pad at ~2.2 V (3.3 V IOVDD), or ~30 µA at 1.8 V. The internal pull-down cannot overcome it.
  - Workarounds: keep IE=0 when relying on the pull-down, or fit an external pull-down ≤8.2 kΩ. PIO can't toggle pad controls.
  - QSPI and USB pads are unaffected. Pads right after reset are fine because IE=0 then.
- **E27 bus-priority mis-wiring** at the FASTPERI and APB arbiters. Affects A2/A3/A4; fix is documentation only (p1363–1364).
  - Mapping: PROC0 bit → DMA-write priority; PROC1 → core-0 load/store; DMA_R → core-1 load/store; DMA_W → DMA-read.
  - Correct at the SRAM/ROM/XIP arbiters.
- **E5 DMA ABORT + CHAIN_TO** can re-trigger; clear EN on the channel and its chain targets first. Affects all steppings (p1365, p1108).
- **E8** CHAIN_TO may not fire from a zero-length transfer. Affects all steppings (p1365).
- **E2** SIO writes to 0x180–0x1FC alias spinlocks 0x100–0x17C (doorbells, PERI_NONSEC, MTIME*, TMDS). Affects all steppings (p1373–1374). Interpolators (0x080–0x0FC) are unaffected.
- **E1** interpolator OVERF broken by the rotate. Affects all steppings (p1373).
- **E11** XIP clean-by-set/way corrupts tags; the SDK `xip_cache_clean_all()` works around it. Affects all steppings (p1374–1375).
- **E12** USB status sync: needs clk_sys ≥ 1.1 × clk_usb while USB is used. Affects all steppings; mitigated on A3 (p1375–1376).
- **E3** QFN-60 NSMASK wrong pads. A2 only; matters only with Non-secure code (p1357).
- **No PIO, DMA-throughput, clock/PLL or GPIO-output errata** are listed (Appx E TOC p11–12).

## 9. Timers / counters for benchmarking

- **DWT_CYCCNT:**
  - 32-bit; counts core clocks when DEMCR.TRCENA=1 and DWT_CTRL.CYCCNTENA=1; wraps (Table 144 p165). At 128 MHz it wraps every 33.5 s (derived).
  - Also present: CPICNT, LSUCNT, FOLDCNT, EXCCNT, SLEEPCNT (§3.7.4.11 p145, TRM C3.1 p C3-100). CYCDISS can disable counting in Secure state (p165).
  - **Inconsistency:** Table 143 (p164) lists reset NOCYCCNT=1, NOPRFCNT=1 and NUMCOMP=7, which contradicts "full DWT, 4 comparators, CYCCNT present" (p145). Read DWT_CTRL at runtime to confirm.
- **SysTick:** 24-bit per core. CLKSOURCE=1 uses the processor clock; 0 uses the external reference = PROC0/PROC1 tick generator from clk_ref (Table 189 p177, §8.5 p570, §12.8.1.2 p1183).
- **TIMER0/TIMER1:**
  - 64-bit, with a 1 µs tick by default. SOURCE=1 counts clk_sys cycles (§12.8 p1182–1183, SOURCE p1189).
  - TIMERAWH/L don't latch and are safe from both cores; the TIMELR→TIMEHR latch is not (p1184). 4 alarms each (p1183).
- **SIO MTIME:** 64-bit, shared, Secure SIO only. MTIME_CTRL.FULLSPEED=1 counts clk_sys (§3.1.8 p43–44, p38, MTIME_CTRL p77). Because of E2, a write to MTIME_CTRL (0x1a4) releases SPINLOCK9 (derived from p1373).
- **Other:** 12 PIO SMs, 12 PWM counters and 4 DMA pacing timers can also count (p1183). FC0 frequency counter (p522). BUSCTRL counters (§4). PIO FDEBUG.TXSTALL catches underruns (p946).

## 10. HSTX

- **Pins and rate:**
  - GPIO12–19 only (8 pins), output only (§12.11 p1202; F0 in Table 3 p18).
  - clk_hstx ≤150 MHz; DDR gives ≤300 Mb/s per pin and ≤16 bits per HSTX cycle (p1202–1203).
  - Output delays balanced within 300 ps (p1202).
  - 8×32-bit async FIFO on FASTPERI; DMA ≤1 write per clk_sys (§12.11.1 p1203).
- **Clock generator:**
  - Single generator; integer period of 1–16 HSTX cycles; phase set in half-cycles; can drive any HSTX pin via BITx.CLK (§12.11.4 p1205–1206, CSR Table 1254 p1209).
  - A centre-aligned clock limits data to 1 bit per HSTX cycle per pin (p1206).
  - clk_hstx sources: clk_sys, pll_sys, pll_usb, GPIN0… (Table 565 p540–541).
- **PIO-coupled mode:** PIO outputs 12–19 of one PIO feed the HSTX crossbar bits 31:24. Requires clk_hstx = clk_sys selected directly. Gives DDR / half-clk_sys edge placement for PIO signals, but adds +1 clk_sys delay vs the direct PIO→pad path. Always PIO bits 19:12, independent of GPIOBASE (§12.11.6 p1208).
- **Why HSTX can't carry the bus:** the AFE7071 bus needs D13:0 + IQ_FLAG + CLK_IO = 16 pins, and HSTX has 8. The AFE7071 takes 14 parallel bits per edge, so a folded 8-pin DDR format doesn't apply.
  - HSTX could supply only CLK_IO: e.g. 64 MHz = CLKDIV 2 at clk_hstx 128 MHz (derived), or a PIO-coupled DDR clock.
  - The data would then leave via a different path from the clock. Clock-to-data skew between HSTX and PIO paths is UNVERIFIED.

## Gaps (UNVERIFIED)

- GPIO max toggle rate.
- PIO-path output delay/skew.
- Absolute DREQ latency.
- Latency with INPUT_SYNC_BYPASS set.
- LDRD / STM / PKHBT / PKHTB cycle counts.
- Whether DWT CYCCNT is present (register table contradiction).
- Operation above 150 MHz or above 1.1 V.
- Sustained PSRAM bandwidth.
- GPOUT-to-PIO phase relationship.
