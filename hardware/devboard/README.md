# RP2350 transmitter hardware: requirements draft

Status: **proposal**. No schematic, nothing bought, no RF hardware measured. Parts are candidates
from desk research (2026-10-01). Tags are in [`docs/sources/SOURCES.md`](../../docs/sources/SOURCES.md);
[EST] marks estimates, [SIM] results from the repo's Python model.

The design is blocks with fixed interfaces: a **shared RF core** (DAC/modulator, DAC clock, LO)
behind the RP2350, and a **mission module** (PA, filters, power domain) per band, so IREC and SATS
boards each take what they need.

## Decisions so far

1. **First spin: an RF daughterboard on the Pico Plus 2**, laid out as the reusable core, with the PA
   on a separate SMA-connected card. Mission boards with their own RP2350B come second.
2. **AFE7071** DAC/modulator. Buy the prototype quantity now: DigiKey showed about 80 units and an
   18–26 week lead time.
3. **LMX2572** LO for both 23 cm and S-band, on its own 12 MHz TCXO. Only the DAC clock locks to the
   RP2350.
4. **CDCE6214** makes the DAC clock as a jitter cleaner, referenced to the RP2350 crystal (GPOUT0 on
   GP21) or to CLK_IO.
5. **IREC PA: GRF2011 → pad → GRF5613** (EVB184 tune), rated 0.5 W, 0.75 W ceiling. Fly the lowest
   power that closes the link: 0.25 W at 10k ft.
6. **Mute in three independent layers**: LO off, PA rail off, AFE in sleep or reset.

## Gates

| # | gate | status |
|---|---|---|
| G1 | Kernel ≤ 75 % of a core at 8 Msym/s, N = 4, bit-exact | **passed**: 10.80 cyc/sym, 67.8 % streaming |
| G2 | Continuous DMA/PIO output, 0 underruns, pin capture exact | **passed**: 60 s |
| G3 | Host input with G2 still clean | **passed**: real CM5 over PV-SPI, 20 and 25 MHz |
| G4 | DVB-S2 encode on-chip at rate, bit-exact | **passed**: encoder 79 %, shaper 76 % (L = 12) |
| G5 | Scope at 64 MW/s: setup/hold, edges, ground bounce, CLK_IO jitter | pending: on the daughterboard test points |
| G6 | AFE7071 bring-up: spectrum, mask, images, LO leakage, DACCLK swing | pending: daughterboard |

## Interfaces (the reuse boundary)

```
RP2350 ──I1 bus──► AFE7071 ──I5 RF──► mission module (PA, filters) ──► antenna
   │ I2 control       ▲ I3 DACCLK  ▲ I4 LO               ▲ I6 PA power domain
   └──────────► CDCE6214      LMX2572 + LPF + pad
```

| | interface | requirement |
|---|---|---|
| I1 | data bus | D13:0, IQ_FLAG, SYNC_SLEEP, CLK_IO 64 MHz SDR; 3.3 V CMOS; setup and hold ≥ 1 ns at the AFE [AFE7071 p.5] |
| I2 | control | SPI to AFE and LO; I2C to CDCE6214 and a GPIO expander (LO CE, AFE RESETB); PA_EN on its own GPIO |
| I3 | DAC clock | 64 MHz differential, AC-coupled with a bias footprint, locked to CLK_IO; 0.45 V peak (meets both readings of the 0.4–1 V spec); ≤ 13 ps rms |
| I4 | LO port | 50 Ω into LO_P, LO_N terminated; +4 dBm ± 0.5 dB; H3 ≤ −50 dBc |
| I5 | core RF out | 50 Ω SMA; −3.3 dBm average at 1.28 GHz, PAPR 5.0 dB at 10⁻⁴ [SIM] |
| I6 | PA domain | switched 5 V, PA_EN default off, current and temperature telemetry |

## Shared core

| block | choice | key numbers | alternates |
|---|---|---|---|
| DAC/modulator | AFE7071, dual-input clock mode | full-scale 0.3 dBm (850 MHz), −1.5 dBm (2.1 GHz); OIP3 17–19 dBm; LO leakage and sideband 36–45 dBc uncalibrated [AFE7071 pp.5–7], < 0.01 dB loss for QPSK 2/3 [EST] | AD9117 + ADL5375 (0.4–6 GHz; needs a DDR PIO program) [AD9117, ADL5375] |
| DAC clock | CDCE6214 | 12 MHz ÷ 3 × 640 ÷ 4 ÷ 10 = 64.000 MHz exact [CDCE6214] | LMK1D1204 buffering CLK_IO [LMK1D1204] |
| LO | LMX2572 (LMX2572LP for IREC only; it stops at 2 GHz) | 20 MHz PFD keeps 5 MHz-grid channels integer-N; 1.6–2.2 mrad [EST]. LO level matters more than phase noise: sideband suppression moves 15–30 dB per dB [SLOA313] | ADF4351 |
| LO network | LFCN-1500+ (23 cm) / LFCN-2500+ (S-band), 2 dB pad | H3 about −50 dBc at 1.28 GHz [LFCN-1500]; S-band may need a second stage (about 26 dB at 3×) | |
| reference | 12 MHz TCXO, ≤ 1 ppb/g | DVB-S2 pilots track vibration: 8 mrad at 10 grms [EST] | SiT7201 |
| power | one LDO per analog rail from 5 V | Pico 3V3 (600 mA) for logic only [PPP2-PAGE] | |
| test | SMA at RF out and LO; test points on DACCLK, CLK_IO, SYNC, D0, IQ_FLAG, each with a GND pad for a ground spring; logic-analyzer header | optional AD8318 detector for field QMC calibration | |

## IREC module (23 cm)

**Band and channel.** The 2026 band plan allows student video in 1240–1300 MHz, ≤ 5 W, ≤ 40 MHz,
with ham ID [IREC-BP]; ask ESRA about the 2027 plan and whether 5 W is conducted or EIRP. 23 cm
amateurs must not harm RNSS [CFR97 97.303(o)], and no 9.6 MHz slot avoids every GNSS signal. Keep
the LO tunable, avoid 1260–1270 MHz (satellite uplink), and offer the coordinator two options:
- 1254.0 MHz, in the gap between GLONASS L2 and Galileo E6 / BeiDou B3; but ITU-R M.2164 allows
  only about 7 mW there at low elevation [M2164].
- 1263–1295 MHz at ≤ −17 dBW/MHz (M.2164), about 0.16 W EIRP: enough for 10k ft, not 30k ft.

M.2164 is ITU guidance, not FCC law.

**Link.** The `../IREC/Pigeon_Vision/DESIGN.md` budget reproduces. With a 10 dB reserve the conducted
power needed is 25–65 mW at 10k ft and 0.22–0.58 W at 30k ft (ETSI threshold [EN302307 Table 13]
vs the notebook's conservative one).

**Level budget at 0.5 W** (average / peak dBm, PAPR 5.0 dB):

| stage | avg | peak | note |
|---|---|---|---|
| AFE7071 | −3.3 | +1.7 | [SIM] |
| post-DAC filter | −4.8 | +0.2 | [EST] |
| GRF2011 | +10.4 | +15.4 | +15.2 dB at 900 MHz; measure at 1.25 GHz [IREC-PA] |
| pad, about 7 dB | +3.2 | +8.2 | |
| GRF5613 | +27.0 | +32.0 | OP1dB 33.3–33.9 dBm [GRF5613-EVB184]; ACPR about −42 dBc [SIM] |
| LPF + coupler | +26.0 | +31.0 | 0.4 W at the antenna port |

**Heat, power, filters.** The PA dissipates about 2.5 W whatever the RF level; assume 30–60 min on the
pad, so design for steady state: base ≤ 85 C [GRF5613], 9–12 filled vias, a spreader, a tie to the
airframe. 5 V draw about 1.0 A (1.25 A peak) through a soft-start switch. No image filter is needed
[EST]; after the PA, a low-pass with ≥ 25 dB at 1575 MHz and ≥ 50 dB at 2.5–3.8 GHz, plus a
band-pass footprint. The rocket's GPS needs a preselector and antenna isolation as well.

## SATS module

**Band**, decide early, both are slow:
- 2200–2290 MHz, Part 5 or 25 with NTIA coordination [CFR2.106 US96], over 6 months. The one recent
  grant [DA26-706] allows only S-band telemetry to stations outside the US.
- 2400–2450 MHz amateur-satellite [CFR97 97.207(c)], IARU coordination, educational missions only
  [IARU-2609].

AWS Ground Station takes only 2200–2290 MHz, as raw IF [AWS-GS]. 1400–1427 MHz is a passive band
[RR 5.340].

**Link and waveform.** 1 Msym/s from 500–550 km needs 0.04–0.21 W into a 3.5 m dish, 0.28–1.64 W
into 1.2 m [EST]. Firmware: N = 8 (16 MHz DAC clock), filter_tune 8: image −58 dBc at 16 % of a
core [SIM]. PA: a 5 V HBT part (GRF5526 [GRF5526]) on a buck from the 18 V bus, in its own domain.
Above 2.7 GHz the core needs AD9117 + ADL5375 or an up-converter; the RP2350 side stays.

## Daughterboard

The Pico Plus 2 has its own crystal, so the DAC clock comes from GPOUT or CLK_IO, and its 2.54 mm
headers make it a test vehicle, not flight hardware. A mission board with its own RP2350B fixes both;
do it second, reusing this RF block. Pins reachable: GP0–22 and GP26–28 on the headers, GP32–36 on
SP/CE [PPP2-SCH]:

| GPIO | use |
|---|---|
| 0–14, 16 | D0–D13, IQ_FLAG, CLK_IO |
| 15 | SYNC_SLEEP, clocked with the bus (dual-input mode needs an external sync [AFE7071 p.25]) |
| 17–20, 22 | PV-SPI, unchanged |
| 21 | GPOUT0 = 12 MHz XOSC → CDCE6214 reference |
| 26–28 | SPI1 to AFE and LO |
| 32–33 | I2C0: CDCE6214 and the GPIO expander (LO CE, AFE RESETB, pulled down: off at power-up) |
| 34–36 | CS_AFE, CS_LO, PA_EN |

Layout: 22 Ω series footprints at the daughterboard end of every bus line, a GND via at every header
GND pin, a solid plane under the bus. Ground bounce from 15 lines switching together is the main
signal-integrity risk [EST].

## Default-off and mute

- Mute with LO CE low, the PA switch off, and AFE sleep or RESETB. TXENABLE alone leaves a carrier,
  and the power-up default is transmit enabled [AFE7071 CONFIG3].
- GRF5613/GRF5526 VSHDN is active-high shutdown: a floating pin enables the PA. Pull it up from the
  switched PA rail [GRF5613].
- Pull-downs ≤ 4.7 kΩ on every enable that must default low (RP2350-E9 needs ≤ 8.2 kΩ on A2).

## Firmware the hardware depends on

- Spectrum mask: done. `pvtx` uses L = 12 with a Kaiser window (β = 1), 7.6 dB under the
  EN 302 307-1 mask; L = 10 unwindowed failed by 1.3 dB [SIM, EN302307 Annex A].
- Write CONFIG1 `twos` = 1 before enabling the LO or PA: the power-on default makes code 0 a
  full-scale carrier [AFE7071 CONFIG1].
- SYNC_SLEEP from bit 15 of chosen DMA words; call-sign ID in the TS.

## Open

- DACCLK swing (peak or peak-to-peak) and input bias: AFE707xEVM design files or TI E2E.
- G5 and G6 on the daughterboard.
- GRF5613 on an EVB184 with the real waveform: ACPR, GRF2011 gain at 1.25 GHz, thermal data.
- Real E200 + gr-dvbs2rx sensitivity: decides whether 0.5 W closes 30k ft.
- ESRA: channel, 2027 band plan, conducted vs EIRP. SATS: band, ground station, filing date.
