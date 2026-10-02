# RP2350 transmitter hardware: requirements draft

Status: **proposal**, still preliminary. No schematic, nothing bought, no RF hardware measured. Parts
are candidates from desk research on 2026-10-01; the notes behind it are in
`../notes/potential-transmitter/` (outside git, to seed the hardware repo; paths outside the repo are
relative to the repo root). Tags are in
[`docs/sources/SOURCES.md`](../../docs/sources/SOURCES.md); [EST] marks estimates, [SIM] results
from the repo's Python model.

The design is a set of blocks with fixed interfaces, so IREC and SATS boards can each take the
parts they need: a **shared RF core** (DAC/modulator, DAC clock, LO) behind the RP2350, and a
**mission module** (PA, filters, power domain) per band.

## Decisions so far

1. **First spin: an RF daughterboard on the Pico Plus 2**, laid out as the reusable core, with the PA
   on a separate SMA-connected card. Mission boards with their own RP2350B come second.
2. **AFE7071** stays the DAC/modulator. Buy the prototype quantity now: DigiKey showed about 80
   units and an 18–26 week lead time.
3. **LMX2572** is the core LO: one part covers 23 cm and S-band. It runs from its own 12 MHz TCXO;
   only the DAC clock has to lock to the RP2350.
4. **DAC clock from a CDCE6214** used as a jitter cleaner, referenced to the RP2350 crystal via
   GPOUT0 (GP21) or to CLK_IO.
5. **IREC PA: GRF2011 → pad → GRF5613** (EVB184 tune), rated 0.5 W average, 0.75 W ceiling. Fly the
   lowest power that closes the link: 0.25 W at 10k ft.
6. **Mute is three independent layers**: LO off, PA rail off, AFE in sleep or reset.
7. **Firmware**: the shaper must change to pass the DVB-S2 spectrum mask (see Firmware).

## Gates before layout

| # | gate | status |
|---|---|---|
| G1 | Kernel ≤ 75 % of one core at 8 Msym/s, N = 4, bit-exact | **passed**: 10.80 cyc/sym, 67.8 % streaming |
| G2 | Continuous DMA/PIO output, 0 underruns, pin capture exact | **passed**: 60 s, capture exact |
| G3 | Concurrent host input with G2 still clean | **passed**: real CM5 over PV-SPI at 20 MHz, 210 s |
| G4 | DVB-S2 encode on-chip at the target rate, bit-exact vs reference | **passed**: 8 Msym/s, encoder 80 %, shaper 72 % |
| G5 | Scope at 64 MW/s: setup/hold, edges, ground bounce, CLK_IO jitter with the kernel running | pending: on the daughterboard's test points (probing the Pico header kept shorting the probe ground) |
| G6 | AFE7071 bring-up: spectrum, mask, images, LO leakage, DACCLK swing sweep | pending (needs the daughterboard) |

## Interfaces (the reuse boundary)

```
RP2350 ──I1 bus──► AFE7071 ──I5 RF──► mission module (PA, filters) ──► antenna
   │ I2 control       ▲ I3 DACCLK  ▲ I4 LO               ▲ I6 PA power domain
   └──────────► CDCE6214      LMX2572 + LPF + pad
```

| | interface | requirement |
|---|---|---|
| I1 | data bus | D13:0 + IQ_FLAG, SYNC_SLEEP, CLK_IO 64 MHz SDR; 3.3 V CMOS; setup and hold ≥ 1 ns at the AFE pins [AFE7071 p.5] |
| I2 | control | SPI for AFE and LO; I2C for the CDCE6214 and a GPIO expander (LO CE, AFE RESETB); PA_EN on its own GPIO; AFE ALARM and LO lock read over SPI |
| I3 | DAC clock | 64 MHz differential (2 f_s), AC-coupled with a bias footprint, locked to CLK_IO; target 0.45 V peak, the only level that meets both readings of the 0.4–1 V spec; jitter ≤ 13 ps rms |
| I4 | LO port | 50 Ω into LO_P single-ended, LO_N terminated; +4 dBm ± 0.5 dB; H3 ≤ −50 dBc |
| I5 | core RF out | 50 Ω SMA; average −3.3 dBm at 1.28 GHz [SIM], PAPR 5.0 dB at 10⁻⁴ [SIM] |
| I6 | PA domain | switched 5 V, PA_EN default off, current and temperature telemetry |

## Shared core

| block | choice | key numbers | alternates |
|---|---|---|---|
| DAC/modulator | AFE7071, dual-input clock mode | full-scale Pout 0.3 dBm (850 MHz), −1.5 dBm (2.1 GHz); OIP3 17–19 dBm; LO leakage and sideband 36–45 dBc uncalibrated [AFE7071 pp.5–7], which costs < 0.01 dB for QPSK 2/3 [EST] | AD9117 + ADL5375: 0.4–6 GHz, needs a DDR PIO program [AD9117, ADL5375] |
| DAC clock | CDCE6214 | 12 MHz ÷ 3 × 640 = 2560 MHz VCO, ÷ 4 ÷ 10 = 64.000 MHz exact [CDCE6214]; jitter that reaches the AFE noise floor: 40–81 ps rms [EST] | LMK1D1204 buffering CLK_IO (bench fallback) [LMK1D1204] |
| LO | LMX2572 (LMX2572LP as an IREC-only cost-down; it stops at 2 GHz) | PFD 20 MHz (12 MHz × 5 ÷ 3) keeps 5 MHz-grid channels integer-N; LO phase error 1.6–2.2 mrad, 1 kHz–4 MHz [EST] | ADF4351 |
| LO network | LFCN-1500+ (23 cm) or LFCN-2500+ (S-band), 2 dB pad | about 40 dB at 3 × 1.28 GHz, so H3 about −50 dBc [LFCN-1500]; LFCN-2500+ gives only about 26 dB at 3 × 2.4 GHz, so S-band may need a second stage | |
| reference | 12 MHz TCXO, ≤ 1 ppb/g specified at purchase | at 1 ppb/g and 10 grms, DVB-S2 pilots leave 8 mrad [EST] | SiT7201 (0.009 ppb/g, 80 mA) |
| power | one LDO per analog rail from 5 V | Pico 3V3 (600 mA max) for logic only [PPP2-PAGE] | |
| test | SMA at RF out and LO; test points on DACCLK, CLK_IO, SYNC, D0 and IQ_FLAG, each with its own GND pad beside it for a probe ground spring; logic-analyzer header | optional AD8318 detector + RP2350 ADC for field QMC calibration [EST] | |

SLOA313: sideband suppression moves 15–30 dB per 1 dB of LO drive, and QMC drifts with temperature,
so the LO level matters more than LO phase noise.

## IREC module (23 cm)

**Band.** The 2026 band plan (Rev D) allows student video in 1240–1300 MHz, ≤ 5 W, ≤ 40 MHz wide,
with a ham licence and call-sign ID [IREC-BP]. The 2027 plan is not out; ask ESRA whether the entry
stays and whether 5 W is conducted or EIRP. Part 97 has no numeric spurious limit above 225 MHz
[CFR97 97.307], and 23 cm amateurs must not harm RNSS [CFR97 97.303(o)].

**Channel.** No 9.6 MHz slot avoids every GNSS signal. Keep the LO tunable and take two options to
the coordinator:
- 1254.0 MHz: in the 9.15 MHz gap between GLONASS L2 and Galileo E6 / BeiDou B3, overlapping their
  edges by 0.5–1 MHz; integer-N with a 24 MHz PFD. ITU-R M.2164 is strictest here for broadband
  amateur use: below 5° elevation it allows only about 7 mW in total [M2164].
- A centre in 1263–1295 MHz at ≤ −17 dBW/MHz EIRP (M.2164), on top of Galileo E6 and QZSS L6.
  The densest 1 MHz holds 1/8 of the power, so that is about 0.16 W EIRP: enough for 10k ft,
  not for 30k ft.

M.2164 is ITU guidance, not FCC law, but a coordinator may apply it.

Avoid 1260–1270 MHz (amateur-satellite uplink).

**Link.** The budget in `../IREC/Pigeon_Vision/DESIGN.md` reproduces. With a 10 dB reserve, the
conducted power needed is:

| apogee | notebook (NF 8 dB, C/N 8 dB) | ETSI 3.10 dB + 1.5 dB loss [EN302307 Table 13] |
|---|---|---|
| 10k ft | 65 mW | 25 mW |
| 30k ft | 0.58 W | 0.22 W |

**Level budget at 0.5 W** (average / peak dBm, PAPR 5.0 dB):

| stage | avg | peak | note |
|---|---|---|---|
| AFE7071 | −3.3 | +1.7 | [SIM]; full scale about 0 dBm at 1.28 GHz [EST] |
| post-DAC filter | −4.8 | +0.2 | −1.5 dB [EST] |
| GRF2011 driver | +10.4 | +15.4 | +15.2 dB at 900 MHz; measure at 1.25 GHz [IREC-PA] |
| pad, about 7 dB | +3.2 | +8.2 | sets PA drive |
| GRF5613 | +27.0 | +32.0 | OP1dB 33.3–33.9, Psat 33.7–34.4 dBm [GRF5613-EVB184] |
| LPF + coupler | +26.0 | +31.0 | 0.4 W at the antenna port |

Simulated ACPR at 0.5 W is about −42 dBc, the waveform's own floor; about −40 dBc at 1 W [SIM, Rapp
model, no AM/PM].

**Heat and power.** The PA dissipates about 2.5 W, nearly constant with RF level (1.55 W is bias).
Assume 30–60 min of transmit on the pad, so steady state: package base ≤ 85 C [GRF5613], 9–12
filled 0.2 mm vias, a spreader and a tie to the airframe. From 5 V: about 1.0 A average, 1.25 A
peak; soft-start load switch, input sized for ≥ 1.5 A.

**Filters and GPS.** No DAC image filter: the image clears the −13 dBm/MHz far-out target by
14.5–17.5 dB [EST]. After the PA, a low-pass with ≥ 25 dB at 1575 MHz and ≥ 50 dB at 2.5–3.8 GHz,
an early filter before the driver, and a footprint for a 2–3 pole band-pass. CBP-1280C+ gives only
about 7 dB at L1 [CBP-1280C]. Protecting the rocket's own GPS needs about 66 dB of rejection of AFE
wideband noise at L1 [EST], plus a GPS preselector and antenna isolation: a system requirement.

## SATS module

**Band.** Two paths, both slow; decide before layout.
- 2200–2290 MHz under Part 5 or Part 25 with NTIA coordination [CFR2.106 US96]. Plan for over 6
  months. The 2026 Part 25 grant [DA26-706] is a narrow precedent: 1.16 MHz of S-band telemetry,
  to stations outside the US only, with payload data on X-band.
- 2400–2450 MHz amateur-satellite [CFR97 97.207(c)] with IARU coordination. The mission must be
  educational and non-commercial; a 1 Mb/s S-band downlink was coordinated in Sept 2026, while
  commercial and Earth-exploration requests were declined [IARU-2609].

AWS Ground Station takes S-band only in 2200–2290 MHz, as raw IF [AWS-GS]. 1400–1427 MHz is a
passive band where all emissions are prohibited [RR 5.340].

**Link.** At 500–550 km with a 5 dBi patch and 6 dB margin, 1 Msym/s needs 0.04–0.21 W into a 3.5 m
dish or 0.28–1.64 W into a 1.2 m dish [EST]. For a fixed dish the band does not change this.

**Waveform.** 1 Msym/s at N = 8 (f_DAC 8 MS/s, CLK_IO = DACCLK = 16 MHz), filter_tune 8: image
−58 dBc at about 16 % of a core [SIM]. Only filter_tune 0, 4 and 8 are characterized by TI.

**PA.** A 5 V HBT part (GRF5526 has a 2.2–2.5 GHz tune but no published data for it [GRF5526]) fed
by a buck from the 18 V bus, in its own switched domain.

**Above 2.7 GHz** (5.8 GHz amateur-satellite, X-band): the AFE7071 cannot go there. Swap the core
for AD9117 + ADL5375 (to 6 GHz) or add an up-converter; the RP2350 side and I1 stay.

## Board options

| | (a) daughterboard on the Pico Plus 2 | (b) mission board with its own RP2350B |
|---|---|---|
| for | fastest path to G6; the digital side is proven; the RF section becomes the reusable block | short matched bus, all 48 GPIO, power domains and flight connectors |
| against | its own crystal, so DACCLK must come from CLK_IO or GPOUT; 64 MHz bus through 2.54 mm headers; not for flight | larger first spin; digital and RF mistakes compound |

Do (a) first. Go straight to (b) only if the schedule allows one spin.

**Daughterboard pins.** The Pico Plus 2 reaches GP0–22 and GP26–28 on the headers and GP32–36 on
SP/CE [PPP2-SCH]:

| GPIO | use |
|---|---|
| 0–13, 14, 16 | D0–D13, IQ_FLAG, CLK_IO |
| 15 | SYNC_SLEEP, driven from the bus so it meets setup/hold (the datasheet requires an external sync in dual-input mode [AFE7071 p.25]) |
| 17–20, 22 | PV-SPI, unchanged |
| 21 | GPOUT0 = XOSC 12 MHz → CDCE6214 reference (the 4-lane link test cannot run with it) |
| 26–28 | SPI1 to AFE and LO |
| 32–33 | I2C0: CDCE6214 and a GPIO expander for LO CE and AFE RESETB, pulled down so both power up off |
| 34–36 | CS_AFE, CS_LO, PA_EN |

Fit 22 Ω series-resistor footprints at the daughterboard end of every bus line, a GND via at every
header GND pin and a solid plane under the bus. Ground bounce from up to 15 bits switching at once is
the main signal-integrity risk, more than rise time [EST].

## Default-off and mute

- Mute with LO CE low, the PA load switch off, and AFE sleep or RESETB. Never use TXENABLE alone: it
  leaves a carrier at RF_OUT, and the power-up default is transmit enabled [AFE7071 CONFIG3].
- GRF5613 and GRF5526 VSHDN is active-high shutdown, so a floating pin enables the PA. Pull it up
  from the switched PA rail [GRF5613].
- Pull-downs ≤ 4.7 kΩ on every enable that must default low (RP2350-E9 needs ≤ 8.2 kΩ on A2
  silicon).

## Firmware the hardware depends on

1. **Spectrum mask.** The shaper (L = 10, no window) exceeds the ETSI α = 0.20 mask by 1.3 dB at
   6.8 MHz (point S, −40 dB) [SIM with ZOH, EN302307 Annex A]. Kaiser β = 2–3 at L = 10 fixes that but droops
   0.89 f_N below its −1.1 dB limit. **L = 12 with β ≈ 1 passes every point**, with the far
   sidelobe at −47.6 dB. Measure `lut_asm_p` at L = 12 on the board.
2. Set CONFIG1 `twos` = 1 before enabling the LO or PA: the power-on default is offset binary, so
   code 0 is full-scale DC, a carrier [AFE7071 CONFIG1].
3. SYNC_SLEEP on GP15 from bit 15 of chosen DMA words; call-sign ID in the TS.

## Open

- DACCLK swing: peak or peak-to-peak, and the input bias. Needs the AFE707xEVM design files or TI E2E.
- G5: CLK_IO jitter and spurs at 64 MHz with the kernel running.
- PA on an EVB184: ACPR with the real waveform, GRF2011 gain at 1.25 GHz, TJ vs Pout and via pattern
  from Guerrilla.
- Real E200 + gr-dvbs2rx sensitivity: it decides whether 0.5 W closes 30k ft.
- Channel, the 2027 band plan and conducted vs EIRP: ask ESRA.
- SATS: band, ground station (DVB-S2 at 1 Msym/s) and the filing start date.
