# Yapogee short-pulse research — frozen candidates, 15 October 2026

The first hardware candidate is **`qpsk_l8_n4_both_m010_far2`**. It uses a 64 KiB four-symbol QPSK table, half the current L9 table. Its quantized linear-FIR response passes both the digital-only and ideal DAC-hold mask models. Hold-aware ideal-receiver ISI improves from **1.531020% to 1.420130%** (7.24% relative). Digital-only ISI changes from 0.970057% to 1.038788%; that trade is explicit. No Pico or RF performance is claimed by this report.

![Modeled spectral and memory/ISI tradeoffs](/tmp/yapogee-short-pulse-frontier.png)

## Frozen profiles

Exact taps, model definitions, mask knots, headroom, table CRCs, certificate slack and baselines are in [/tmp/yapogee-short-pulse-selected.json](/tmp/yapogee-short-pulse-selected.json). The file SHA256 is `a800c69ac3ccda72bbe70816915a373a68402f3df9a50b3dbaea5b4d68bf3d49`. IDs identify different pulse designs; `far2` and `far8` are different reserve choices.

| Profile ID | Table memory | Role |
|---|---:|---|
| `qpsk_l8_n4_both_m010_far2` | 64 KiB | First hardware candidate; both digital and held-pulse mask models pass. |
| `qpsk_l8_n4_hold_m010_far2` | 64 KiB | Co-designed DAC-hold variant; digital-only mask fails. |
| `psk8_l8_n2_hold_m010_far2` | 64 KiB | 8PSK balanced variant; less far-stopband reserve than the existing L9. |
| `psk8_l8_n2_hold_m010_far8` | 64 KiB | 8PSK reserve variant; more far rejection at higher ISI than the balanced variant. |
| `qpsk_l7_n4_hold_m010_far2` | 32 KiB | Research-only compact variant; no demonstrated 32KiB PIO addressing implementation. |

## Both response models, after Q8 coefficient quantization

Each margin is measured inward from the relevant Annex A amplitude boundary. Negative margins fail. ISI is RMS ideal-RRC receive sidelobes divided by the main sample, with constant delay removed. The independent alias-sum integral includes all sidelobes rather than truncating them at a finite symbol lag.

| Profile | Model | Upper margin dB | Lower margin dB | Far margin dB | ISI % | Continuous mask |
|---|---|---:|---:|---:|---:|---|
| `qpsk_l8_n4_both_m010_far2` | Digital | +0.099843 | +0.220437 | +2.099337 | 1.038788 | pass |
| `qpsk_l8_n4_both_m010_far2` | With hold | +0.219306 | +0.101461 | +2.755436 | 1.420130 | pass |
| `qpsk_l8_n4_hold_m010_far2` | Digital | -1.542975 | +0.250000 | -1.542975 | 0.788445 | fail |
| `qpsk_l8_n4_hold_m010_far2` | With hold | +0.099947 | +0.250000 | +2.099497 | 0.808980 | pass |
| `psk8_l8_n2_hold_m010_far2` | Digital | -1.975101 | +0.250000 | -1.330419 | 2.817321 | fail |
| `psk8_l8_n2_hold_m010_far2` | With hold | +0.099985 | +0.219053 | +2.099929 | 1.165031 | pass |
| `psk8_l8_n2_hold_m010_far8` | Digital | -2.011296 | +0.250000 | +4.652511 | 2.700906 | fail |
| `psk8_l8_n2_hold_m010_far8` | With hold | +0.100000 | +0.157213 | +8.099732 | 1.590251 | pass |
| `qpsk_l7_n4_hold_m010_far2` | Digital | -1.037880 | +0.250000 | -1.037880 | 1.224443 | fail |
| `qpsk_l7_n4_hold_m010_far2` | With hold | +0.099816 | +0.216938 | +2.099661 | 1.469839 | pass |
| `qpsk_l9_n4_kaiser_b080` | Digital | +0.192339 | +0.232941 | +1.130657 | 0.970057 | pass |
| `qpsk_l9_n4_kaiser_b080` | With hold | +0.250000 | +0.226679 | +1.820467 | 1.531020 | pass |
| `qpsk_l12_n4_kaiser_b100` | Digital | +0.186553 | +0.250000 | +6.849947 | 0.441708 | pass |
| `qpsk_l12_n4_kaiser_b100` | With hold | +0.250000 | +0.250000 | +7.567623 | 1.161069 | pass |
| `psk8_l9_n2_current_preeq` | Digital | -0.261900 | +0.250000 | +5.937705 | 2.169306 | fail |
| `psk8_l9_n2_current_preeq` | With hold | +0.232594 | +0.230908 | +8.795387 | 1.674082 | pass |

**Scope of every margin:** `f_n = f / (R_sym / 2)`. N4 upper-mask comparisons cover `f_n ∈ [0, 6]`; N2 comparisons cover `[0, 2]`. The lower boundary exists over `[0, 1.11]`. Far rejection covers `[1.7, 6]` for N4 and `[1.7, 2]` for N2. The N2 far interval is narrower and does not qualify its first DAC image. These intervals must accompany any chart or claim.

The hold-only N4 variant reaches **0.808980%** ISI and roughly 2.10 dB far reserve, but fails the digital-only mask by 1.543 dB. It is a distinct DAC/pulse co-design experiment. The N2 balanced variant reaches **1.165031%** ISI at 2.10 dB far reserve. The N2 reserve variant gives **1.590251%** ISI at 8.10 dB reserve; the actual current L9 preEQ baseline gives 1.674082% at 8.795 dB reserve. The different reserves explain the earlier 1.165% versus 1.590% numbers.

## Why this is possible

A short pulse does not have to be a windowed sample of an infinite RRC. With a fixed ideal RRC receiver, many finite transmit pulses occupy the permitted spectral envelope. A symmetric FIR has a real zero-phase cosine response, so upper and lower mask inequalities are linear in its tap coefficients when the DC response is positive. Fixing the desired main receive sample to one makes the RMS sidelobe objective a convex Euclidean norm. The solver searches the permitted pulse shapes directly rather than sweeping one Kaiser window parameter.

For even tap count `M = N*L`, the centered response is `H(f_n) = 2 Σ x[j] cos(π f_n (j-(M-1)/2)/N)`. The ideal hold multiplies amplitude by `sinc(f_n/(2N))`; digital and hold delays are `(M-1)/(2N)` and `M/(2N)` symbols. Optimization uses 4,001 uniform frequency points plus exact mask knots, signed upper bounds, positive lower bounds and 80 receiver sidelobes per side. Supporting hyperplanes converge to the second-order-cone ISI objective; the selected LP bound gaps are below 2×10⁻⁸ before quantization. This proves an accurately solved gridded model, not a globally optimal continuous-frequency filter.

Independent verification evaluates more than 1,048,000 frequency points including **every exact mask knot**. A global derivative bound for the FIR, hold and interpolated envelope subtracts a worst-case between-grid uncertainty from the minimum linear-amplitude slack. Every listed pass has positive continuous-frequency slack over its stated interval. A separate alias-sum Parseval calculation gives infinite-symbol ISI; explicit continuous-time held-pulse integration into analytic ideal RRC converges to the same values. The initial FFT-only estimates missed a few exact-knot extrema by up to 0.0014 dB; the frozen table uses the corrected independent values.

## Standard interpretation and limits

The source is [ETSI EN 302 307-1 V1.4.1, Annex A and Table A.1](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf), α = 0.20. The model follows the existing reference implementation by interpolating dB linearly between the table knots. Annex A describes ideal pulse inputs and calls for sinc compensation when rectangular inputs are used. Designing the FIR together with a known DAC hold is consistent with that distinction, but it changes which digital-only waveform passes the template. No complete inverse-sinc or zero-ISI restoration is claimed. Finite pulse length, spectral limits and quantization still trade against receive ISI.

The certificate is for the Q8 **linear pulse** with or without an ideal DAC hold. The final LUT rounds sample sums to integer DAC words; its exact nonlinear rounding is a separate independent audit. Physical AFE magnitude/phase, DAC aperture details, clock jitter, I/Q skew, RF power amplifier behavior and transmitter compliance are outside this model. No external measurement has validated either the profile or its first/subsequent DAC images.

## Exact quantization and table truth

The coefficient array is signed int32 Q8 in DAC sample units. Each binary-axis history uses `floor((Σ symbol_sign*coefficient_q8 + 128)/256)`. Exhausting all histories gives peak +7300 and −7300 for every selected binary pulse: **1.000281 dB** headroom below 8191. Four-symbol rows were checked sample by sample against a separate scalar FIR: zero mismatches. The 8PSK two-plane Q30 coefficient scaling is also frozen; the combined axis peak is 6744, leaving **1.688387 dB** headroom.

First hardware profile Q8 taps, in chronological sample order (symmetric):

```text
18026, -9152, -32743, -43578, -16902, 44626, 101009, 97555
8822, -128261, -220622, -167118, 78231, 466713, 865736, 1117887
1117887, 865736, 466713, 78231, -167118, -220622, -128261, 8822
97555, 101009, 44626, -16902, -43578, -32743, -9152, 18026
```

For that profile, the 2,048 canonical rows occupy 65,536 bytes. Canonical packed-u14 table CRC32 is **629259652**; the proposed BSWAP-PIO row-order CRC32 is **3433922394**. These are distinct layouts. CRCs and coefficient SHA256 values for every profile are in the frozen JSON.

## Final rounded-table audit

An independent exact integer Walsh decomposition now includes LUT sample rounding. The checksum-bound [/tmp/yapogee-short-pulse-selected-audit.json](/tmp/yapogee-short-pulse-selected-audit.json) has SHA256 `a896517ce100446c49867711fa47433ecab65a431527d4c9ea659d4e15165b6e` and matches every frozen coefficient/table checksum. All five held variants pass the continuous-frequency certificate for their **zero-mean iid-coordinate ensemble PSD**; the first N4 variant also passes digitally. Deterministic phase-mean spectral lines are reported separately, and receiver error includes DC bias. This is not a universal spectrum claim for deterministic PLHEADERs, pilots or coded payloads.

| Profile | Actual rounded engine | Upper / lower / far margin dB | Ideal-RRC ISI + distortion % |
|---|---|---:|---:|
| `qpsk_l8_n4_both_m010_far2` | Binary LUT + hold | +0.219368 / +0.101419 / +2.758689 | 1.419996 |
| `qpsk_l8_n4_hold_m010_far2` | Binary LUT + hold | +0.098673 / +0.250000 / +2.089991 | 0.808922 |
| `psk8_l8_n2_hold_m010_far2` | Q30 A/B planes + hold | +0.097314 / +0.219213 / +2.088691 | 1.164447 |
| `psk8_l8_n2_hold_m010_far8` | Q30 A/B planes + hold | +0.098510 / +0.157131 / +8.094530 | 1.590245 |
| `qpsk_l7_n4_hold_m010_far2` | Binary LUT + hold | +0.098800 / +0.216951 / +2.092919 | 1.469776 |

The first profile's actual digital-only rounded table also passes: upper/lower/far margins +0.099908/+0.220338/+2.089863 dB, with error 1.038773%. The actual existing rounded L9/preEQ 8PSK planes give 1.674304% under the identical audit.

Full independent derivation: [/tmp/yapogee-short-pulse-independent-review.md](/tmp/yapogee-short-pulse-independent-review.md).

## PIO/address experiment needed for L8

The old L9 PIO history register reverses nibble groups. Merely changing `IN X,12` to `IN X,11` removes the wrong history bit. The proposed L8 program extracts the correct 11 bits and uses the DMA controller’s 32-bit byte swap for the emitted pointer. It requires a 64 KiB-aligned table and a different row permutation.

[/tmp/yapogee_l8_pio_pointer_model.py](/tmp/yapogee_l8_pio_pointer_model.py) models the actual 13 instruction encodings. It checks 36,972 rolling addresses plus all 2,048 keys across three table bases, with zero mismatches. It executes 49 non-stalled instructions per 16-symbol raw word: **3.0625 PIO cycles/symbol**, compared with the old 2.3125. The DAC sample count and DMA payload traffic do not decrease. This is a memory win; it does not by itself establish higher symbol rate or lower CPU usage. FIFO/DMA timing, pointer ownership and capture on the actual Pico remain required.

L7 demonstrates a possible 32 KiB pulse table, but a correct 32 KiB PIO address construction has not been demonstrated. It is not the first integration choice. Within the specified symmetric FIR, hold/no-hold models and reserve constraints, L7/N4 both-model with 2 dB extra far reserve and N2/L8 both-model are infeasible; this is not a proof that every architecture or waveform is impossible.

## Reproduction

All new scripts and results are outside the repository. No production source, shared build, firmware or USB state was changed for this research. The scripts read the repository’s reference implementation and SciPy/NumPy.

```sh
python3 /tmp/yapogee_short_pulse_opt.py --case 4,8,1,.1,2 --out /tmp/reproduce-hold-only.json
python3 /tmp/yapogee_short_pulse_verify.py
python3 /tmp/yapogee_short_pulse_export.py
python3 /tmp/yapogee_l8_pio_pointer_model.py
python3 /tmp/yapogee_short_pulse_plot.py
```

To reproduce the first both-model pulse in Python, call `optimize(4, 8, True, .1, 2, both=True)` from `/tmp/yapogee_short_pulse_opt.py`; its exported exact Q8 coefficients are authoritative for integration. Solver floating-point re-runs may choose a neighboring coefficient rounding near a quantizer boundary. Independent review code is `/tmp/yapogee_short_pulse_review.py`; its separate JSON records the audit rather than modifying the frozen manifest.
