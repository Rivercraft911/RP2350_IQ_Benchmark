### T-val. CPU model vs measured runs (work fraction of each core)

| Run | Core 0 pred / meas | Core 1 pred / meas |
|---|---:|---:|
| QPSK 5/6 16M GCS pack1 bank1 (calibration run) | 82.3% / 82.3% | 82.0% / 82.0% |
| QPSK 5/6 16M TS pack1 bank1 | 82.3% / 82.6% | 93.6% / 93.7% |
| QPSK 5/6 16M GCS pack0 bank0 | 86.2% / 85.6% | 87.9% / 88.3% |
| 8PSK 5/6 8M (no split, pack0) | 75.5% / 75.5% | 93.3% / 92.1% |
| 8PSK 5/6 9.375M @150 | 75.5% / 75.6% | 93.3% / 92.1% |
| 16APSK 5/6 split 4.6875M @150 | 87.5% / 88.1% | 81.3% / 80.1% |
| 32APSK 5/6 split 3.125M @150 | 89.5% / 89.8% | 89.0% / 87.7% |
| 16APSK 5/6 no-split 4.6875M @150 (FAILED, underrun) | 64.9% / - | 103.8% / 99.8% |
| 16APSK 5/6 no-split 4.1667M @150 | 57.7% / - | 92.3% / 92.1% |

### T-cyc. Two-core cycles per on-air symbol at SYS-independent grid steps

Available (2 cores x u_demo 0.9628 x 2N d): N4 d=1 (16 Msym/s at SYS128) = 15.4; N4 d=2 = 30.8; N2 d=2 (16 Msym/s) = 15.4; N2 d=3 = 23.1; N2 d=4 = 30.8.

| MODCOD | FEC variant | ingress+BCH+LDPC+map | of which map | SW shaping (+sup.) | total, SW shaping | total, HW shaping |
|---|---|---:|---:|---:|---:|---:|
| QPSK 5/6 | meas | 10.18 | 2.17 | 2.97 (qpsk_desc_n4) | 13.15 | 10.45 |
| QPSK 5/6 | bchT | 10.18 | 2.17 | 2.97 (qpsk_desc_n4) | 13.15 | 10.45 |
| QPSK 9/10 | bchT | 9.53 | 2.17 | 2.97 (qpsk_desc_n4) | 12.50 | 9.80 |
| 8PSK 5/6 | meas | 14.22 | 2.21 | 12.08 (psk2plane_n2) | 26.30 | 14.49 |
| 8PSK 5/6 | bchT | 14.22 | 2.21 | 12.08 (psk2plane_n2) | 26.30 | 14.49 |
| 8PSK 9/10 | bchT | 13.25 | 2.21 | 12.08 (psk2plane_n2) | 25.33 | 13.52 |
| 16APSK 5/6 | meas | 33.23 | 17.25 | 20.78 (apsk16_sw_n2) | 54.01 | - |
| 16APSK 5/6 | bchT | 33.23 | 17.25 | 20.78 (apsk16_sw_n2) | 54.01 | - |
| 16APSK 9/10 | bchT | 31.94 | 17.25 | 20.78 (apsk16_sw_n2) | 52.72 | - |
| 32APSK 5/6 | meas | 51.74 | 31.76 | 33.94 (apsk32_sw_n2) | 85.68 | - |
| 32APSK 5/6 | bchT | 51.74 | 31.76 | 33.94 (apsk32_sw_n2) | 85.68 | - |
| 32APSK 9/10 | bchT | 50.13 | 31.76 | 33.94 (apsk32_sw_n2) | 84.07 | - |

### T-front. Frontier by scenario, SYS128, GCS input, u_demo (Rs Msym/s / BB Mb/s / binding)

| MODCOD | N | S0 today | S1 +A1 | S2 +B1 | S3 +C1 | S4 beyond |
|---|---|---|---|---|---|---|
| QPSK 2/3 | N4 | 16.00 / 20.7 / AFE | 16.00 / 20.7 / AFE | 16.00 / 20.7 / AFE | 16.00 / 20.7 / AFE | 16.00 / 20.7 / AFE |
| QPSK 2/3 | N2 | 8.00 / 10.3 / CPU | 10.67 / 13.8 / CPU | 10.67 / 13.8 / CPU | 10.67 / 13.8 / CPU | 16.00 / 20.7 / AFE* |
| QPSK 3/4 | N4 | 8.00 / 11.6 / CPU | 16.00 / 23.2 / AFE | 16.00 / 23.2 / AFE | 16.00 / 23.2 / AFE | 16.00 / 23.2 / AFE |
| QPSK 3/4 | N2 | 8.00 / 11.6 / CPU | 10.67 / 15.5 / CPU | 10.67 / 15.5 / CPU | 10.67 / 15.5 / CPU | 16.00 / 23.2 / AFE* |
| QPSK 5/6 | N4 | 16.00 / 25.8 / AFE | 16.00 / 25.8 / AFE | 16.00 / 25.8 / AFE | 16.00 / 25.8 / AFE | 16.00 / 25.8 / AFE |
| QPSK 5/6 | N2 | 8.00 / 12.9 / CPU | 10.67 / 17.2 / CPU | 10.67 / 17.2 / CPU | 10.67 / 17.2 / CPU | 16.00 / 25.8 / AFE* |
| QPSK 9/10 | N4 | 8.00 / 14.0 / CPU | 16.00 / 27.9 / AFE | 16.00 / 27.9 / AFE | 16.00 / 27.9 / AFE | 16.00 / 27.9 / AFE |
| QPSK 9/10 | N2 | 8.00 / 14.0 / CPU | 8.00 / 14.0 / CPU | 8.00 / 14.0 / CPU | 10.67 / 18.6 / CPU | 16.00 / 27.9 / AFE* |
| 8PSK 2/3 | N4 | no engine | no engine | 8.00 / 15.5 / DMA hand-off* | 8.00 / 15.5 / DMA hand-off* | 16.00 / 31.0 / ingress* |
| 8PSK 2/3 | N2 | 8.00 / 15.5 / CPU | 8.00 / 15.5 / CPU | 8.00 / 15.5 / CPU | 8.00 / 15.5 / CPU | 16.00 / 31.0 / ingress* |
| 8PSK 3/4 | N4 | no engine | no engine | 8.00 / 17.4 / DMA hand-off* | 8.00 / 17.4 / DMA hand-off* | 16.00 / 34.8 / ingress* |
| 8PSK 3/4 | N2 | 6.40 / 13.9 / CPU | 8.00 / 17.4 / CPU | 8.00 / 17.4 / CPU | 8.00 / 17.4 / CPU | 16.00 / 34.8 / ingress* |
| 8PSK 5/6 | N4 | no engine | no engine | 8.00 / 19.4 / DMA hand-off* | 8.00 / 19.4 / DMA hand-off* | 16.00 / 38.8 / ingress* |
| 8PSK 5/6 | N2 | 8.00 / 19.4 / CPU | 8.00 / 19.4 / CPU | 8.00 / 19.4 / CPU | 8.00 / 19.4 / CPU | 16.00 / 38.8 / ingress* |
| 8PSK 9/10 | N4 | no engine | no engine | 8.00 / 20.9 / DMA hand-off* | 8.00 / 20.9 / DMA hand-off* | 16.00 / 41.9 / ingress* |
| 8PSK 9/10 | N2 | 6.40 / 16.8 / CPU | 8.00 / 20.9 / CPU | 8.00 / 20.9 / CPU | 8.00 / 20.9 / CPU | 16.00 / 41.9 / ingress* |
| 16APSK 2/3 | N4 | no engine | no engine | no engine | no engine | SRAM (272 KiB) |
| 16APSK 2/3 | N2 | 4.57 / 11.8 / CPU | 4.57 / 11.8 / CPU | 4.57 / 11.8 / CPU | 4.57 / 11.8 / CPU | 4.57 / 11.8 / CPU |
| 16APSK 3/4 | N4 | no engine | no engine | no engine | no engine | SRAM (272 KiB) |
| 16APSK 3/4 | N2 | 3.20 / 9.3 / CPU | 3.20 / 9.3 / CPU | 3.20 / 9.3 / CPU | 4.00 / 11.6 / CPU | 4.00 / 11.6 / CPU |
| 16APSK 5/6 | N4 | no engine | no engine | no engine | no engine | SRAM (272 KiB) |
| 16APSK 5/6 | N2 | 4.00 / 12.9 / CPU | 4.00 / 12.9 / CPU | 4.00 / 12.9 / CPU | 4.00 / 12.9 / CPU | 4.00 / 12.9 / CPU |
| 16APSK 9/10 | N4 | no engine | no engine | no engine | no engine | SRAM (272 KiB) |
| 16APSK 9/10 | N2 | 3.20 / 11.1 / CPU | 3.20 / 11.1 / CPU | 3.20 / 11.1 / CPU | 4.00 / 13.9 / CPU | 4.57 / 15.9 / CPU |
| 32APSK 3/4 | N4 | no engine | no engine | no engine | no engine | SRAM (230 KiB) |
| 32APSK 3/4 | N2 | 2.29 / 8.3 / CPU | 2.67 / 9.7 / CPU | 2.67 / 9.7 / CPU | 2.67 / 9.7 / CPU | 2.67 / 9.7 / CPU |
| 32APSK 5/6 | N4 | no engine | no engine | no engine | no engine | SRAM (230 KiB) |
| 32APSK 5/6 | N2 | 2.67 / 10.7 / CPU | 2.67 / 10.7 / CPU | 2.67 / 10.7 / CPU | 2.67 / 10.7 / CPU | 2.67 / 10.7 / CPU |
| 32APSK 9/10 | N4 | no engine | no engine | no engine | no engine | SRAM (230 KiB) |
| 32APSK 9/10 | N2 | 2.13 / 9.3 / CPU | 2.67 / 11.6 / CPU | 2.67 / 11.6 / CPU | 2.91 / 12.7 / CPU | 2.91 / 12.7 / CPU |

`*` = engine not measured (derived or hypothetical). Ingress-bound rows give capacity; deliverable data is the ingress cap (30.87 Mb/s on-chip 32 MHz; 24.13 Mb/s external 25 MHz).

### T-bounds. Separate bounds (Msym/s) for the S4 point of each 5/6 and 9/10 MODCOD, SYS128

| MODCOD | N | engine / partition | (a) FEC | (b) shaping | (a+b) CPU | (c) DMA bw / hand-off | (d) table / budget KiB | (e) AFE | (f) ingress 32 / 25 MHz | Rs grid | BB Mb/s | max core load | binding | flags |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| QPSK 5/6 | N4 | qpsk_ptr_n4 / fp | 24.2 | 41.8 | 23.8 | 28.1 / 32.0 | 64 / 134 | 16.25 | 19.1 / 14.9 | 16.000 | 25.84 | 65% | AFE |  |
| QPSK 5/6 | N2 | qpsk_ptr_n2 / fp | 24.2 | 41.8 | 23.8 | 50.0 / 32.0 | 32 / 134 | 16.67 | 19.1 / 14.9 | 16.000 | 25.84 | 65% | AFE | N2 image at 22.4 MHz (AFE tune0 ~22 dB), unqualified; hypothetical |
| QPSK 9/10 | N4 | qpsk_ptr_n4 / fp | 25.9 | 41.8 | 25.4 | 28.1 / 32.0 | 64 / 134 | 16.25 | 17.7 / 13.8 | 16.000 | 27.94 | 61% | AFE |  |
| QPSK 9/10 | N2 | qpsk_ptr_n2 / fp | 25.9 | 41.8 | 25.4 | 50.0 / 32.0 | 32 / 134 | 16.67 | 17.7 / 13.8 | 16.000 | 27.94 | 61% | AFE | N2 image at 22.4 MHz (AFE tune0 ~22 dB), unqualified; hypothetical |
| 8PSK 5/6 | N4 | hw4_L7R1_n4_2ch / fp | 17.3 | 64.0 | 17.0 | 20.9 / 16.0 | 128 / 134 | 16.25 | 12.7 / 10.0 | 16.000 | 38.76 | 91% | ingress | DMA 77% > demonstrated 57%; core load 91% > planning 90%; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| 8PSK 5/6 | N2 | hw4_L7R1_n2_2ch / fp | 17.3 | 64.0 | 17.0 | 31.0 / 16.0 | 64 / 134 | 16.67 | 12.7 / 10.0 | 16.000 | 38.76 | 91% | ingress | core load 91% > planning 90%; N2 image at 22.4 MHz (AFE tune0 ~22 dB), unqualified; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| 8PSK 9/10 | N4 | hw4_L7R1_n4_2ch / fp | 18.6 | 64.0 | 18.3 | 20.9 / 16.0 | 128 / 134 | 16.25 | 11.8 / 9.2 | 16.000 | 41.89 | 84% | ingress | DMA 77% > demonstrated 57%; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| 8PSK 9/10 | N2 | hw4_L7R1_n2_2ch / fp | 18.6 | 64.0 | 18.3 | 31.0 / 16.0 | 64 / 134 | 16.67 | 11.8 / 9.2 | 16.000 | 41.89 | 84% | ingress | N2 image at 22.4 MHz (AFE tune0 ~22 dB), unqualified; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| 8PSK 2/3 | N4 | hw4_L7R1_n4_2ch / fp | 19.4 | 64.0 | 19.0 | 20.9 / 16.0 | 128 / 134 | 16.25 | 15.9 / 12.5 | 16.000 | 30.97 | 81% | ingress | DMA 77% > demonstrated 57%; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| 8PSK 2/3 | N2 | hw4_L7R1_n2_2ch / fp | 19.4 | 64.0 | 19.0 | 31.0 / 16.0 | 64 / 134 | 16.67 | 15.9 / 12.5 | 16.000 | 30.97 | 81% | ingress | N2 image at 22.4 MHz (AFE tune0 ~22 dB), unqualified; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| 16APSK 5/6 | N4 | apsk16_sw_n4 / pipe0 | 3.7 | 3.3 | 3.3 | 32.0 / inf | 272 / 181 | 16.25 | 9.6 / 7.5 | 0.000 | 0.00 | - | SRAM | table 272 KiB > budget 181 KiB |
| 16APSK 5/6 | N2 | apsk16_sw_n2 / bal | 7.4 | 11.9 | 4.6 | 64.0 / inf | 136 / 173 | 16.67 | 9.6 / 7.5 | 4.000 | 12.89 | 84% | CPU | N2 image at 5.6 MHz (AFE tune0 ~0 dB), unqualified |
| 16APSK 9/10 | N4 | apsk16_sw_n4 / pipe0 | 3.9 | 3.3 | 3.3 | 32.0 / inf | 272 / 181 | 16.25 | 8.9 / 6.9 | 0.000 | 0.00 | - | SRAM | table 272 KiB > budget 181 KiB |
| 16APSK 9/10 | N2 | apsk16_sw_n2 / bal | 7.7 | 11.9 | 4.7 | 64.0 / inf | 136 / 173 | 16.67 | 8.9 / 6.9 | 4.571 | 15.92 | 94% | CPU | core load 94% > planning 90%; N2 image at 6.4 MHz (AFE tune0 ~0 dB), unqualified |
| 32APSK 5/6 | N4 | apsk32_sw_n4 / pipe0 | 2.4 | 2.5 | 2.4 | 32.0 / inf | 230 / 181 | 16.25 | 7.7 / 6.0 | 0.000 | 0.00 | - | SRAM | table 230 KiB > budget 181 KiB |
| 32APSK 5/6 | N2 | apsk32_sw_n2 / fp | 4.8 | 7.3 | 2.9 | 64.0 / inf | 115 / 134 | 16.67 | 7.7 / 6.0 | 2.667 | 10.75 | 89% | CPU | N2 image at 3.7 MHz (AFE tune0 ~0 dB), unqualified |
| 32APSK 9/10 | N4 | apsk32_sw_n4 / pipe0 | 2.5 | 2.5 | 2.5 | 32.0 / inf | 230 / 181 | 16.25 | 7.1 / 5.5 | 0.000 | 0.00 | - | SRAM | table 230 KiB > budget 181 KiB |
| 32APSK 9/10 | N2 | apsk32_sw_n2 / fp | 4.9 | 7.3 | 2.9 | 64.0 / inf | 115 / 134 | 16.67 | 7.1 / 5.5 | 2.909 | 12.67 | 96% | CPU | core load 96% > planning 90%; N2 image at 4.1 MHz (AFE tune0 ~0 dB), unqualified |

### T-fec. BB capacity allowed by ingress+FEC+map alone on two cores (frame-parallel, SYS128, u_demo)

| MODCOD | variant | cycles/symbol | cycles/BB bit | Rs cap | BB cap Mb/s |
|---|---|---:|---:|---:|---:|
| QPSK 2/3 | meas | 9.17 | 7.11 | 26.87 | 34.7 |
| QPSK 2/3 | bchT | 9.17 | 7.11 | 26.87 | 34.7 |
| QPSK 5/6 | meas | 10.18 | 6.30 | 24.22 | 39.1 |
| QPSK 5/6 | bchT | 10.18 | 6.30 | 24.22 | 39.1 |
| QPSK 9/10 | meas | 11.58 | 6.63 | 21.28 | 37.2 |
| QPSK 9/10 | bchT | 9.53 | 5.46 | 25.86 | 45.2 |
| 8PSK 2/3 | meas | 12.71 | 6.57 | 19.39 | 37.5 |
| 8PSK 2/3 | bchT | 12.71 | 6.57 | 19.39 | 37.5 |
| 8PSK 5/6 | meas | 14.22 | 5.87 | 17.34 | 42.0 |
| 8PSK 5/6 | bchT | 14.22 | 5.87 | 17.34 | 42.0 |
| 8PSK 9/10 | meas | 16.33 | 6.24 | 15.10 | 39.5 |
| 8PSK 9/10 | bchT | 13.25 | 5.06 | 18.60 | 48.7 |
| 16APSK 2/3 | meas | 31.23 | 12.13 | 7.89 | 20.3 |
| 16APSK 2/3 | bchT | 31.23 | 12.13 | 7.89 | 20.3 |
| 16APSK 5/6 | meas | 33.23 | 10.31 | 7.42 | 23.9 |
| 16APSK 5/6 | bchT | 33.23 | 10.31 | 7.42 | 23.9 |
| 16APSK 9/10 | meas | 36.03 | 10.35 | 6.84 | 23.8 |
| 16APSK 9/10 | bchT | 31.94 | 9.17 | 7.72 | 26.9 |
| 32APSK 5/6 | meas | 51.74 | 12.84 | 4.76 | 19.2 |
| 32APSK 5/6 | bchT | 51.74 | 12.84 | 4.76 | 19.2 |
| 32APSK 9/10 | meas | 55.25 | 12.68 | 4.46 | 19.4 |
| 32APSK 9/10 | bchT | 50.13 | 11.51 | 4.92 | 21.4 |

### T-lvl. Per-axis amplitude levels (data + unit-radius header/pilot + Gold quarter turns)

| Alphabet | levels |
|---|---:|
| QPSK | 2 |
| 8PSK (native) | 5 |
| 8PSK/QPSK/headers, pi/8 rotated | 4 |
| 16APSK 2/3 (R2=1) | 8 |
| 16APSK 3/4 (R2=1) | 8 |
| 16APSK 4/5 (R2=1) | 8 |
| 16APSK 5/6 (R2=1) | 8 |
| 16APSK 8/9 (R2=1) | 8 |
| 16APSK 9/10 (R2=1) | 8 |
| 16APSK 5/6 (E=1, unit headers) | 10 |
| 32APSK 3/4 (R3=1) | 17 |
| 32APSK 4/5 (R3=1) | 17 |
| 32APSK 5/6 (R3=1) | 17 |
| 32APSK 8/9 (R3=1) | 17 |
| 32APSK 9/10 (R3=1) | 17 |

### T-tab. Full-history table size (one row per axis history, R=1, int16 samples), KiB

| Alphabet | N | L5 | L6 | L7 | L8 | L9 |
|---|---|---:|---:|---:|---:|---:|
| 2-level (QPSK) | N2 | 0 | 0 | 0 | 1 | 2 |
| 2-level (QPSK) | N4 | 0 | 0 | 1 | 2 | 4 |
| 4-level (QPSK/8PSK pi/8) | N2 | 4 | 16 | 64 | 256 | 1,024 |
| 4-level (QPSK/8PSK pi/8) | N4 | 8 | 32 | 128 | 512 | 2,048 |
| 8-level (16APSK) | N2 | 128 | 1,024 | 8,192 | 65,536 | 524,288 |
| 8-level (16APSK) | N4 | 256 | 2,048 | 16,384 | 131,072 | 1,048,576 |
| 17-level (32APSK) | N2 | 5,546 (131,072 PIO-keyed) | 94,287 (4,194,304 PIO-keyed) | 1,602,885 (134,217,728 PIO-keyed) | 27,249,053 (4,294,967,296 PIO-keyed) | 463,233,893 (137,438,953,472 PIO-keyed) |
| 17-level (32APSK) | N4 | 11,093 (262,144 PIO-keyed) | 188,575 (8,388,608 PIO-keyed) | 3,205,771 (268,435,456 PIO-keyed) | 54,498,105 (8,589,934,592 PIO-keyed) | 926,467,785 (274,877,906,944 PIO-keyed) |

### T-part. Split-span (partial) tables: memory vs. required summation

| Scheme | KiB | adds per output word | output words per symbol | source |
|---|---:|---:|---:|---|
| 16APSK measured (3+4+4) | 136.0 | 2 | 2 | results/apsk-dual/2026-10-15/README.md 139,264 B |
| 16APSK L7 R1 N2 (4+3) | 18.0 | 1 | 2 | this note |
| 16APSK L7 R1 N4 (4+3) | 36.0 | 1 | 4 | this note |
| 16APSK L8 R4 N4 (3+4+4) | 272.0 | 2 | 4 | this note |
| 32APSK measured (3+3+3) | 115.1 | 2 | 2 | results/apsk-frontier/2026-10-15/README.md 117,912 B |
| 32APSK L7 R1 N2 (3+2+2) | 21.4 | 2 | 2 | this note |
| 32APSK L8 R2 N4 (3+3+3) | 230.3 | 2 | 4 | this note |

### T-snif. DMA sniffer SUM as an adder: transfer-count bound only (ignores chain latency)

| terms S | N | transfers per summed 32-bit word | Rs bound at SYS128 (Msym/s) |
|---:|---:|---:|---:|
| 2 | 2 | 6 | 10.7 |
| 2 | 4 | 6 | 5.3 |
| 3 | 2 | 8 | 8.0 |
| 3 | 4 | 8 | 4.0 |

### T-lev. Lever results (vs measured QPSK 5/6 N4 16 Msym/s, 25.84 Mb/s)

| Group | Lever | BB Mb/s | change | deliverable 32 / 25 MHz SPI | Rs | binding | core loads | flags |
|---|---|---:|---:|---:|---:|---|---|---|
| ref | QPSK 5/6 N4 16 Msym/s, measured (desc engine, split FEC) | 25.84 | +0.0% | 25.84 / 24.13 | 16.000 | AFE | 82% / 82% |  |
| i | QPSK 5/6 16 Msym/s with TS input (today) | 25.84 | +0.0% | 25.84 / 24.13 | 16.000 | AFE | 82% / 94% | core load 94% > planning 90% |
| i | QPSK 5/6 16 Msym/s, TS input, A1 (pointers + frame-parallel) | 25.84 | +0.0% | 25.84 / 24.13 | 16.000 | AFE | 71% / 71% |  |
| i | QPSK 9/10, today's code (no t=8 frontend: BCH on core 1) | 13.97 | -46.0% | 13.97 / 13.97 | 8.000 | CPU | 25% / 72% |  |
| i | QPSK 9/10, t=8 BCH frontend kernel, today's descriptor engine | 27.94 | +8.1% | 27.94 / 24.13 | 16.000 | AFE | 78% / 79% |  |
| i | QPSK 9/10, PIO pointers + generic BCH sliced on core 0 (no frame-parallel, no faster kernel) | 27.94 | +8.1% | 27.94 / 24.13 | 16.000 | AFE | 69% / 79% |  |
| i | QPSK 9/10 via A1 alone (frame-parallel, existing BCH) | 27.94 | +8.1% | 27.94 / 24.13 | 16.000 | AFE | 74% / 74% |  |
| i | QPSK 9/10 at SYS130 (16.25 Msym/s, 65.0 MS/s) | 28.37 | +9.8% | 28.37 / 24.13 | 16.250 | AFE | 61% / 61% |  |
| ii | 8PSK 2/3 N4 16 Msym/s: 4-level L7 R1, 2 chains/axis, frame-parallel FEC | 30.97 | +19.8% | 30.87 / 24.13 | 16.000 | ingress | 81% / 81% | DMA 77% > demonstrated 57%; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| ii | 8PSK 3/4 N4 16 Msym/s: 4-level L7 R1, 2 chains/axis, frame-parallel FEC | 34.84 | +34.8% | 30.87 / 24.13 | 16.000 | ingress | 93% / 93% | DMA 77% > demonstrated 57%; core load 93% > planning 90%; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| ii | 8PSK 5/6 N4 16 Msym/s: 4-level L7 R1, 2 chains/axis, frame-parallel FEC | 38.76 | +50.0% | 30.87 / 24.13 | 16.000 | ingress | 90% / 90% | DMA 77% > demonstrated 57%; core load 91% > planning 90%; needs 11 SMs / 17 DMA ch (limit 12/16); hypothetical |
| ii | 8PSK 5/6 N4 via B1 as specified (1 chain/axis) | 19.38 | -25.0% | 19.38 / 19.38 | 8.000 | DMA hand-off | 46% / 46% | hypothetical |
| ii | 8PSK 5/6 N2 today (2-plane software) | 19.38 | -25.0% | 19.38 / 19.38 | 8.000 | CPU | 76% / 89% | N2 image at 11.2 MHz (AFE tune0 ~4 dB), unqualified |
| ii | 8PSK 5/6 N2 today at SYS150 | 22.71 | -12.1% | 22.71 / 22.71 | 9.375 | CPU | 76% / 89% | N2 image at 13.1 MHz (AFE tune0 ~8 dB), unqualified |
| ii | 16APSK 5/6 N2 today (split), SYS128 | 12.89 | -50.1% | 12.89 / 12.89 | 4.000 | CPU | 88% / 81% | N2 image at 5.6 MHz (AFE tune0 ~0 dB), unqualified |
| ii | 16APSK 5/6 N2 today (split), SYS150 | 15.10 | -41.6% | 15.10 / 15.10 | 4.688 | CPU | 88% / 81% | N2 image at 6.6 MHz (AFE tune0 ~0 dB), unqualified |
| ii | 16APSK 5/6 N2 PL-job balance, SYS128 | 12.89 | -50.1% | 12.89 / 12.89 | 4.000 | CPU | 84% / 84% | N2 image at 5.6 MHz (AFE tune0 ~0 dB), unqualified |
| ii | 16APSK 5/6 N2 PL-job balance, SYS150 | 15.10 | -41.6% | 15.10 / 15.10 | 4.688 | CPU | 84% / 84% | N2 image at 6.6 MHz (AFE tune0 ~0 dB), unqualified |
| ii | 32APSK 5/6 N2 today (split), SYS128 | 10.75 | -58.4% | 10.75 / 10.75 | 2.667 | CPU | 90% / 89% | N2 image at 3.7 MHz (AFE tune0 ~0 dB), unqualified |
| ii | 32APSK 5/6 N2 today (split), SYS150 | 12.60 | -51.3% | 12.60 / 12.60 | 3.125 | CPU | 90% / 89% | N2 image at 4.4 MHz (AFE tune0 ~0 dB), unqualified |
| ii | 32APSK 5/6 N2 PL-job balance, SYS128 | 10.75 | -58.4% | 10.75 / 10.75 | 2.667 | CPU | 89% / 89% | N2 image at 3.7 MHz (AFE tune0 ~0 dB), unqualified |
| ii | 32APSK 5/6 N2 PL-job balance, SYS150 | 12.60 | -51.3% | 12.60 / 12.60 | 3.125 | CPU | 89% / 89% | N2 image at 4.4 MHz (AFE tune0 ~0 dB), unqualified |

### T-map. APSK PL-mapper sensitivity (PL-job balance, 5/6, N2, SYS128)

| Mod | map cycles/symbol | continuous Rs | grid Rs | BB Mb/s |
|---|---:|---:|---:|---:|
| 16APSK | 17.3 | 4.56 | 4.000 | 12.89 |
| 16APSK | 12.0 | 5.06 | 4.571 | 14.73 |
| 16APSK | 8.0 | 5.51 | 5.333 | 17.18 |
| 16APSK | 4.0 | 6.05 | 5.333 | 17.18 |
| 16APSK | 2.2 | 6.33 | 5.333 | 17.18 |
| 32APSK | 31.8 | 2.88 | 2.667 | 10.75 |
| 32APSK | 12.0 | 3.74 | 3.556 | 14.33 |
| 32APSK | 8.0 | 3.98 | 3.556 | 14.33 |
| 32APSK | 4.0 | 4.26 | 4.000 | 16.12 |
| 32APSK | 2.2 | 4.39 | 4.000 | 16.12 |

SRAM shaping-table budgets (KiB): lab image 69.3; flight single-profile 181.4; flight with frame-parallel FEC 133.9.

### T-sck. PV-SPI SCK needed per lane (1332-byte messages, 10 us CS gap)

| Point | BB Mb/s | 1 lane | 2 lanes |
|---|---:|---:|---:|
| QPSK 5/6 @16 | 25.84 | 26.8 MHz | 13.2 MHz |
| QPSK 9/10 @16 | 27.94 | 29.0 MHz | 14.3 MHz |
| QPSK 9/10 @16.25 | 28.37 | 29.5 MHz | 14.6 MHz |
| 8PSK 2/3 @16 | 30.97 | 32.3 MHz | 15.9 MHz |
| 8PSK 5/6 @16 | 38.76 | 40.7 MHz | 20.0 MHz |
| 8PSK 9/10 @16 | 41.89 | 44.2 MHz | 21.6 MHz |
