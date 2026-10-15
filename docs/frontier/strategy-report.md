# RP2350 DVB-S2 baseband: prior art, novelty, new strategies

Strategy/novelty review, 2026-10-15. Read-only; no firmware, board or Pico was touched.
Repo evidence is from `experiment/yapogee-limits` at `34195ce`. Web sources were opened on
2026-10-15 and are listed at the end. **F** = fact from a cited source or repo record,
**E** = my estimate (arithmetic shown), **U** = unknown.

## Bottom line

1. **The MCU is no longer the throughput bottleneck for Yapogee's adopted N4 profile.**
   - QPSK at 16 Msym/s × 4 SPS needs 64 MS/s per axis. That is 98.5% of the AFE7071's
     65 MSPS maximum (F, SLOS789C p.1).
   - The occupied width, 19.2 MHz, sits just under the AFE's 20 MHz maximum RF bandwidth.
   - The repo already sustains this rate digitally: 25.844 Mb/s BB capacity with QPSK 5/6.
   - Higher QPSK throughput at N4 is capped by the DAC, not by the RP2350. That is a
     clean, honest headline: *the MCU saturates the DAC*.
2. **The closest prior art is three weeks old.** ESP32-DATV (repo created 2026-10-03, F)
   does RRC LUT shaping on an ESP32-C3 at ≤1 MS/s, but its FEC and framing run on a PC.
   - Other low-cost DATV transmitters do one of three things: put FEC on a PC; use an FPGA
     (PlutoDVB2 uses the ORI FPGA modulator at 20 kS–4 MS/s); or filter analog 1-bit I/Q
     (Portsdown-era boards, ≤ about 2 MS/s).
   - I found no published MCU that does on-chip BCH/LDPC plus shaping at multi-Msym/s.
   - Novelty therefore rests on the **complete on-chip chain at 16 Msym/s**, not on any
     single technique.
3. **The individual techniques are known.** LUT modulators date to 1985 (US4843613), and
   component-summed LUTs are the same idea as the repo's signed planes. PicoDVI already
   uses interpolators to generate LUT addresses. DMA control blocks are a stock SDK example.
   A DMA "adder" via the sniffer already exists (Cornell DMA machine).
   - A paper must claim the system and its measured limits, not the tricks.
4. **Without RF measurement and an independent receiver decode, there is no paper.**
   - Both Yapogee carriers (1280 and 2400 MHz) fall inside the BATC MiniTiouner's
     143–2450 MHz, 66 kS–30 MS/s range, and it reports MER (F).
   - The cheapest closing experiment is therefore direct reception with no downconverter.
5. **Best new ideas, ranked in §3:**
   - (P1) Take the DDR clock from the writers' existing side-set. It is already a
     phase-correct 64 MHz clock on GPIO14 and costs zero extra state machines.
   - (P2) Build one 128 KiB, 4-level full-history L7 table. It would give hardware-only
     QPSK/8PSK/VCM shaping at 8 Msym/s **on the adopted N4 + DDR32 profile**.
   - (P3) Run a full-coverage DMA-sniffer CRC of the address stream. This answers the
     "sparse capture" critique at zero bandwidth cost.

## 1. Prior art

| Work | What it does | Rate | FEC location | Source |
|---|---|---|---|---|
| ESP32-DATV (SP8ESA; fork americo8888) | ESP32-C3 writes RRC-shaped I/Q (α 0.35, 8-symbol LUT span per fork README) to its Wi-Fi DAC; 2.3–2.45 GHz direct RF | QPSK/8PSK/16APSK ≤1000 kS/s; 32APSK ≤250 kS/s; 8 MS/s DAC | PC: BBFRAME, BCH, LDPC, interleave, PLFRAME; symbols sent over USB | [1][2] |
| Portsdown / DigiLite-class boards | Raspberry Pi GPIO 1-bit I/Q → analog Nyquist filter → AD8345/AD8349 | 250 kS and 2000 kS (2083 "works"); filter-mod v2 settings 125–500 kS | Pi CPU (DVB-S) | [3][4] |
| PlutoDVB2 (F5OEO) | Zynq SDR firmware; "DVB-S/S2 modulator in FPGA (Open Research Institute)" | 20 kS–4 MS | FPGA | [5] |
| DATV-Express (2014) | PC prepares I/Q stream over USB2; board FPGA + modulator | 1–5 MSym/s; DVB-S2 via GNU Radio up to 10 MSym/s (BladeRF) | PC | [6] |
| gr-dvbs2rx / leandvb | Open receivers; gr-dvbs2rx supports QPSK/8PSK CCM only (no VCM/APSK); leandvb supports MODCODs 1–28 | PC | — | [7][8] |
| CPU/DSP class | AFF3CT/StreamPU DSEL (multicore SIMD x86/ARM DVB-S2 transceiver); RC64 64-core DSP with LDPC accelerator, >2 Gb/s TX | Gb/s-class | Desktop/DSP + accelerators | [9][10] |
| LUT shaping | US4843613 (DEC, priority 1985): LUT addressed by 3 consecutive symbols, 12 samples/symbol; components summed to shrink memory | — | — | [11] |
| RP2040 peripherals | PicoDVI: interpolator generates TMDS-LUT addresses; DMA control blocks; 3 PIO SMs, 6 DMA channels. SDK `control_blocks`: control DMA writes data channel's alias-3 `READ_ADDR_TRIG`. Cornell DMA machine: sniffer as adder, ~8 M DMA blocks/s | — | — | [12][13][14] |

I found no Cortex‑M DVB-S2 LDPC/BCH throughput figures in two searches. Published software
encoders target SIMD desktop CPUs or many-core DSPs [9][10]. Treat this as "not found", not
as "does not exist". A literature search on IEEE Xplore and Google Scholar is still required
before claiming firsts.

## 2. What appears new, and what a reviewer will attack

These claims are supported by repo evidence (F), and each is narrower than "first":

- **Complete DVB-S2 baseband on one ~128 MHz dual-M33 MCU at 16 Msym/s.** The chain is
  SPI → GCS/TS BBFRAME → BCH/LDPC → PL framing → RRC N4 → 128 Mword/s 14-bit bus.
  - 60 s runs: QPSK 5/6 + pilots, 25.844 Mb/s BB capacity (`results/waveforms/2026-10-15`,
    `pulse-design/.../pico-matched-soak-1`).
  - This is 16× ESP32-DATV's symbol rate, with FEC on-chip rather than on a PC.
- **CPU-free shaping datapath.**
  - PIO SMs turn raw packed QPSK bits into LUT row addresses.
  - Control DMA feeds them to sample DMA, which feeds staggered PIO writers.
  - Measured 3.38% core-0 supervision at 16 Msym/s with a cyclic PRBS source
    (`quad-pointers/2026-10-15/poll128-soak-1`). This is not yet integrated with live FEC.
- **Causal SRAM-bank placement result.** A one-section A/B/A rebuild gives about 31% fewer
  BCH-stage cycles and rescues the complete 8PSK path (`memory-placement/2026-10-15`).
  This is a nice "systems lesson" figure.
- **Continuous QPSK/8PSK VCM.** One shared π/8-rotated two-plane engine preserves filter
  history across MODCOD changes (`vcm/2026-10-15`).
- **Verification discipline:** independent references, a gr-dtv cross-check at five stages,
  CRC chains, failed controls kept, and pinned binaries.

What a reviewer will attack first, in order:

1. No AFE, no spectrum and no MER/EVM measurement.
2. No independent receiver decode. Self-referential models are not interoperability, and
   gr-dvbs2rx cannot decode VCM [7].
3. The recent rates use an on-chip SPI master. 5/6 at 16 Msym/s needs an SPI clock of at
   least 26.8 MHz, while the external CM5 was proven only at 20/25 MHz.
4. Sparse 4096-word captures over 60 s runs, on one A2 unit at room temperature.
5. 150 MHz results sit at the stock limit and outside Yapogee's SYS128 plan.
6. No power or cost comparison against the FPGA it claims to replace.
7. The DDR clock is not yet implemented or measured.

## 3. New strategies

### P1. DDR clock = the writers' own IQ_FLAG side-set (data-derived clock)

- **Observation (F, `dual_dma.c`):** in paired and quad modes, every I writer does
  `OUT PINS side 1` and every Q writer `OUT PINS side 0` on GPIO14. All other writer
  instructions use no side-set (`.side_set 1 opt`).
  - With four staggered writers at divider 1, exactly one OUT executes per SYS cycle.
  - GPIO14 is therefore already a 50%-duty 64 MHz square wave whose rising edge launches
    with every I word: exactly the AFE DDR clock (I on rising edge, Q on falling).
- **Timing:** identical to the "aligned-source PWM" screen in the Yapogee notes. With the
  SN65 at 1.8–2.9 ns delay, setup is ≥1.8 ns against 0 ns required and hold is ≥4.91 ns
  against 2 ns required (SLOS789C DDR table) (E, same equal-path assumptions).
- **Two ways to use it:**
  - Firmware only: move `sideset_pins` from 14 to 28 on PIO2 (GPIOBASE 0 reaches GP28).
    IQ_FLAG is ignored in DDR.
  - Board only: add a DNP 0 Ω link from the GP14/IQ_FLAG net to SN65 1A.
- **Cost:** 0 SMs and 0 instructions; no atomic cross-PIO start is needed, because I/Q
  parity holds by construction across every restart.
- **Risks:**
  - The clock stops whenever the writers stall. This is the "enabled clock halt → oversized
    AC-coupled restart edge" case in clocks.md, so fault handling must disable SN65 first
    or keep writers stall-free.
  - It is unknown whether the AFE tolerates a non-continuous DDR clock (U; ask TI, or
    measure).
  - There is no clock pre-roll before the first word.
- **Smallest experiment:** run the existing quad writers and capture pins 0–31 instead of
  0–15. Check that bit 28 (or bit 14) toggles every word and equals the I/Q slot identity
  in every sample.
  - Even for the parent's separate GP28 clock SM, **"GP28 == GP14 in every captured
    sample"** is the cheapest online proof of DDR parity. I recommend adding it to the
    capture validator either way.

### P2. One 4-level full-history table: hardware-only QPSK/8PSK/VCM at N4

- **Math (F/E):** with the repo's global π/8 rotation, every symbol class maps onto the same
  four per-axis levels {±0.924, ±0.383}. That covers 8PSK, rotated QPSK (3π/8 + kπ/2) and
  π/2-BPSK headers and pilots.
  - A per-axis window of L symbols × 2 bits indexes a full-history table shared by I and Q.
- **Size:** L7, one-symbol rows, N4 gives 4^7 × 4 samples × 2 B = **128 KiB**. L8 would
  need 512 KiB, which does not fit.
- **Pulse quality (F):** `qpsk_l7_n4_hold_m010_far2` already passes the EN 302 307-1
  Annex A mask in the DAC-hold model (+0.10 dB upper, +2.10 dB far, 1.47% ISI). It fails
  digital-only, so it depends on the hold.
  - The PSD of a linear modulation depends only on the pulse, so the mask result carries
    over to 8PSK. A 1.5% RMS ISI floor is small against 8PSK 5/6 needs (E).
- **Bandwidth (E):** 3 transfers (1 control + 2 data) per axis per symbol. At 8 Msym/s that
  is 48 M transfers/s, or 37.5% of the DMA slots at 128 MHz.
  - The one-symbol-row mode has already run at 8 Msym/s with CPU descriptors.
  - 16 Msym/s would need 96 M/s and 32 M handoffs/s, which is not recommended.
- **Addresses:** either adapt the QPSK PIO pointer program to 2-bit steps and a 14-bit
  window, or use interpolator descriptors (≈2–3 cycles/symbol/axis E).
  - The packed 8PSK mapper already produces the two sign planes per axis, so it only
    needs bit-interleaving.
- **Payoff (E):**
  - 8PSK 5/6 at 8 Msym/s gives 19.38 Mb/s in 9.6 MHz, on the *same* SYS128 / DDR32 / N4
    profile as IREC.
  - That is +88% over QPSK 2/3 at 10.33 Mb/s, with N4 images instead of N2's
    roughly −18 dBc.
  - Freed core 0 can take BCH, balancing FEC near half of each core.
  - It needs several dB more Es/N0. That is a link-budget choice, not free capacity.
- **Smallest experiment:**
  1. Generate the table from the frozen L7 taps.
  2. Native bit-exact test against the scalar FIR on all 4^7 rows.
  3. A 60 s cyclic-PRBS 8PSK trial at 8 Msym/s with the existing capture validator.
  4. Then live FEC and a VCM boundary capture.

### P3. Full-coverage verification with the DMA CRC sniffer

- The sniffer can be enabled on one channel at zero bus cost.
- Put it on an axis's control DMA. It then CRC-32s **every LUT row address** emitted
  during the 60 s run: about 2.4×10⁸ at 16 Msym/s with quad rows (E).
- Compare with the host's native model of the same deterministic source. Run alternate
  runs on I and Q.
- Together with the static table CRC, this proves the whole row sequence. Sparse pin
  captures continue to cover the pins and timing.
- At 8 Msym/s a full-rate pin capture into a sniffed DMA also fits (E):
  - Output is about 36 M transfers/s; capture adds 32 M/s; together 68 of 128 M/s.
  - This gives a CRC of literally every bus word.
  - At 16 Msym/s it does not fit (72 + 64 > 128 M/s).
- Mind that the sniffer is a single shared resource; the FEC self-check uses CRC at
  startup.

### P4. Frame-parallel FEC once shaping is in hardware (QPSK 16 Msym/s)

- A QPSK 5/6 + pilots frame lasts 33,282/16e6 = 2.08 ms, which is 266 k cycles per core
  at 128 MHz.
- The measured complete 2/3 frame costs about 289 k cycles (`dual-dma/2026-10-15`). 5/6 has
  more BCH input (about 101 k cycles at the measured 1.88 cycles/bit) and less LDPC parity
  (U, not measured here).
- With PIO pointers, odd and even frames on separate cores give each core about 532 k
  cycles per frame, i.e. **~55–60% load** including 3.4% supervision (E). Today it is
  86% / 76%.
- No intra-frame handoff is needed: two FE frame slots plus in-order publication.
- This is the cleanest route to "the MCU saturates the DAC with margin". The repo's
  next-experiments #1 already plans the pointer integration; this is the scheduling choice
  that should accompany it.

### P5. Pre-equalize the AFE in the LUT (zero runtime cost), after measuring it

- **In-band droop at 16 Msym/s (F/E):**
  - The AFE baseband filter at filter_tune 0 is 1 dB down at 10 MHz (typical), with
    0.5 dB ripple below 0.9 × cutoff and 2° RMS phase deviation (SLOS789C p.6).
  - The DAC hold adds sinc(9.6/64) = −0.32 dB at the 9.6 MHz band edge.
  - Total in-band droop is therefore roughly 1.3 dB at the edge, plus group-delay ripple.
- **The fix:** any real per-axis FIR equalizer can be folded into the precomputed rows,
  limited by table headroom and the mask certificate.
- **Do not** fold IQ imbalance correction into the tables. The AFE7071 already has
  gain/phase/offset QMC (CONFIG5–15), and phase correction couples I into Q, which
  per-axis tables cannot express.
- **Experiment:** measure the AFE's response first, because the corner tolerance is U.
  Then compare MiniTiouner MER at 16 Msym/s with and without the EQ table.

### Considered and rejected (brief reasons)

| Idea | Why not |
|---|---|
| DMA-sniffer adder for multi-plane (8PSK/APSK) shaping | Prior art [14]. Needs ≥3 serial chained transfers per output word; ~8 M DMA blocks/s class [14] is far below 32–64 M words/s |
| DMA XOR-alias LDPC, PIO BCH | PIO has no XOR; DMA XOR via register aliases ~3 transfers per word-XOR saturates DMA at 16 Msym/s (E) |
| Rewrite BCH/LDPC algebraically | Already at the method floor. BCH slicing-by-4 is load-bound at 20 table loads per 32 bits, measured 1.88 cycles/bit. LDPC already uses the QC 360-bit rotate-XOR form (`dvbs2.c:356`). Expect ≤~20% (E); not paper material |
| HSTX or a second MCU for parallelism | The DAC caps QPSK N4 at 16.25 Msym/s; HSTX is limited to GPIO12–19 (RP2350 datasheet, not re-checked here) |
| 3 SPS or DVB-S2X α=0.05 | Exceeds the 20 MHz AFE bandwidth, or needs filter spans far beyond LUT feasibility |
| 8PSK at N2, higher rates | Image at ~11 MHz, about −18 dBc modeled; incompatible with adopted N4 without an external filter |
| Lower SYS (e.g. 64 MHz for IREC) to save power | Feasible on paper (DDR32 = SYS/2), but RP2350 savings are small next to the AFE's 334 mW typ (F); measure before bothering |

## 4. Candidate paper

- **Title:** *Retiring the FPGA: A Complete DVB-S2 Transmitter Baseband on a Dual-Core
  Microcontroller at 16 Msym/s*.
- **Claim:** a ~$1-class RP2350 (two M33 @128 MHz + PIO + DMA) produces standard DVB-S2
  QPSK 5/6 at 64 MS/s per axis. That saturates a 65 MSPS IQ DAC, with BCH/LDPC on-chip,
  verified against independent references and **received by a commercial-chipset
  demodulator**. The receiver part is still missing.
- **Contributions:**
  1. A peripheral-only shaping datapath (PIO address generation → control/sample DMA →
     staggered PIO writers), with a cost model in DMA transfers per sample against table
     size 2^(b(L+R−1))·R·N·2 B.
  2. A Cortex-M33 FEC with per-stage cycles, and a bank-placement causal study.
  3. Modulation-agnostic shaping and VCM (P2), plus a structural-parity DDR clock (P1).
  4. Measured RF: mask, MER, image levels, LO leakage, and decode at 1.28 GHz and 2.4 GHz.
  5. Power and BOM against an FPGA reference (e.g. PlutoDVB2 at the same MODCOD).
- **Missing experiments:**
  - AFE spectrum vs. Annex A mask.
  - MER/EVM on MiniTiouner/Ryde, plus decoded TS CRC vs. the SPI source.
  - External-master SPI at ≥27 MHz.
  - Full-coverage verification (P3).
  - Multi-hour soak and temperature.
  - Power per mode.
  - At least two boards or units.
- **Threats to validity:** single silicon revision; instrumented CPU percentages are not
  whole-system load; an on-chip source shares clocks with the sink; the Pico Plus 2 is not
  the Yapogee board; jitter/SN65 behaviour is unmeasured; the AFE rate limit means "16
  Msym/s" is DAC-bound.
- **Venue:**
  - First an amateur/DATV or SDR community venue (TAPR/ARRL DCC, AMSAT Symposium, GNU
    Radio Conference, BATC CQ-TV). Those audiences value exactly this and accept
    engineering papers once RF and decode exist.
  - Then a short archival paper: IEEE Embedded Systems Letters-style letter, IEEE
    Aerospace Conference, or SmallSat if tied to the SATS mission.
  - Not a top-tier embedded-systems venue unless the datapath is generalized beyond
    DVB-S2 with a broader cost model.
  - This is my judgement; I did not check venue scopes or deadlines.

## 5. Ranked next steps

1. **Close RF + independent decode** on the first AFE hardware (Yapogee Rev A, or an AFE707x
   EVM if obtainable). Transmit at 1.28 GHz into a MiniTiouner (in range, reports MER), log
   MER and TS CRC against the SPI source, and measure spectrum against the mask.
   Everything else is secondary to this for a paper.
2. **DDR clock A/B on pins now:** the separate GP28 SM (being implemented) vs. P1. Add the
   "GP28 == GP14 every sample" capture check.
3. **Integrate PIO pointers with live QPSK FEC, using frame-parallel FEC (P4)** at
   16 Msym/s, with **P3 sniffer CRC** for full coverage.
4. **External SPI ≥27 MHz** from the CM5 (or a 2-lane link) for 16 Msym/s 5/6.
5. **P2** 4-level L7 table at 8 Msym/s N4 (8PSK/QPSK/VCM on the IREC clock profile).
6. **P5** AFE pre-EQ once the response is measured; power per mode.
7. **Deprioritize:** APSK/N2 work, FEC micro-optimizations, 150 MHz modes and any DMA/PIO
   "compute" tricks.

## Sources (opened 2026-10-15)

1. SP8ESA/ESP32-DATV README and GitHub API metadata (created 2026-10-03): https://github.com/SP8ESA/ESP32-DATV
2. americo8888/ESP32-DATV (fork) README: https://github.com/americo8888/ESP32-DATV
3. PG540 wiki, SETUP-DATV DVB-S print for Raspberry Pi: https://www.pg540.org/wiki/index.php/SETUP-DATV_DVB-S_Print_for_Raspberry_PI_in_the_English_language
4. BATC wiki, Filter Modulator ver2: https://wiki.batc.org.uk/Filter_Modulator_ver2
5. HB9DUG, "PlutoDVB2 Firmware", HAMRADIO 2024 DATV forum slides: https://wiki.batc.org.uk/images/e/e7/Hamradio-2024-datv-forum-plutodvbs2.pdf
6. W6HHC, "SDR-Based DATV-Express", TAPR DCC 2014: https://web.tapr.org/meetings/DCC_2014/DCC2014-SDR-Based-DATV-Express-W6HHC.pdf
7. gr-dvbs2rx `docs/support.md` (via GitHub API): https://github.com/igorauad/gr-dvbs2rx
8. PABR, leandvb: http://www.pabr.org/radio/leandvb/leandvb.en.html
9. Cassagne et al., "A DSEL for High Throughput and Low Latency SDR on Multicore CPUs", arXiv:2206.06147 (abstract only): https://arxiv.org/abs/2206.06147
10. "DVB-S2 Software Defined Radio Modem on the RC64 Manycore DSP", ESA indico: https://indico.esa.int/event/102/contributions/49/attachments/28/35/4_4_DVBS2_Software_Defined_Radio_Modem_on_RC64_20160613.pdf
11. US4843613A, "Digitally implemented modulators": https://patents.google.com/patent/US4843613
12. PicoDVI README (via GitHub API): https://github.com/Wren6991/PicoDVI
13. pico-examples `dma/control_blocks/control_blocks.c` (via GitHub API): https://github.com/raspberrypi/pico-examples
14. B. Land, Cornell ECE4760 "DMA machine": https://people.ece.cornell.edu/land/courses/ece4760/RP2040/C_SDK_DMA_machine/DMA_machine_rp2040.html
15. BATC wiki, MiniTioune (143–2450 MHz, 66 kS–30 MS, QPSK…32APSK): https://wiki.batc.org.uk/MiniTioune ; Ryde Receiver (MER banner): https://wiki.batc.org.uk/Ryde_Receiver
16. TI AFE7071 datasheet SLOS789C, local copy `docs/sources/afe7071.txt` (65 MSPS, 20 MHz RF BW, QMC, DDR tSU 0 / tH 2 ns, baseband filter table).

Side notes, out of scope:
- `results/waveforms/2026-10-15/pulse-design.md` links to `/tmp/yapogee-short-pulse-*`
  paths that do not exist in the repo.
- The waveforms README names the deleted branch `experiment/yapogee-next-waveforms`.
