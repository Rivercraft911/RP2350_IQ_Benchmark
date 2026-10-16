# Four-level L7/N4 table: hardware-only 8PSK shaping at 8 Msym/s

**Result:** with shaping done entirely by PIO address generation and DMA, cyclic-PRBS 8PSK
ran 60 s at 8 Msym/s, 4 samples/symbol, 32 MHz GPIO28 DDR clock, SYS128: every one of
4096 sparse GPIO snapshots matched the independent scalar-FIR reference, GPIO28 equalled
IQ_FLAG in every snapshot, and CPU supervision was 1.8% of core 0 (core 1 idle). The
inverted-clock control failed all 4096 parity checks with correct data. This is the IREC
clock profile; live FEC and VCM are not integrated yet.

Image `daa294ffc0` built with `-DIQ4HIST_EXPERIMENT=ON` (branch `rd/b1-l7-4level`, UF2
sha256 `8b8f711d…d703`, `flash.txt`). Runner `host/iq4hist_trial.py` (`control-3s/`,
`target-60s/`, each with manifest, captures and runs). Pico Plus 2, RP2350B A2, USB only.

| Run | Symbol rate / DDR clock | Duration | Bus / clock parity | Supervision | Result |
|---|---|---:|---|---:|---|
| control | 4 Msym/s / 16 MHz | 3 s | 0 mismatches / 4096 of 4096 | 1.79% | clean |
| control, inverted clock | 4 Msym/s / 16 MHz | 3 s | 0 / 0 of 4096 | 1.79% | detected |
| target | 8 Msym/s / 32 MHz | 60 s | 0 mismatches / 4096 of 4096 | 1.80% | clean |
| target, inverted clock | 8 Msym/s / 32 MHz | 60 s | 0 / 0 of 4096 | 1.81% | detected |

Table CRC on device 431963165 equals the native generator. Maximum poll gap 131 µs.

What this establishes and what it does not: the 128 KiB four-level full-history table, PIO
key generation and DMA chain sustain the 8 Msym/s N4 bus without CPU sample arithmetic.
REPORT.md derives why 16 Msym/s is out of reach for this design (about 130 M shared
fast-peripheral accesses/s needed against 128 M/s available) and why the pulse passes the
spectral mask only with the DAC hold modelled. Live 8PSK FEC, VCM boundaries, AFE output,
spectrum and EVM remain to be measured.
