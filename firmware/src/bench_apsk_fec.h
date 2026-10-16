// Complete two-core normal5/6 APSK pipeline, fed by on-chip PV-SPI GCS traffic.
#pragma once
// modulation16/32, PIO clocks/bus word3..32, duration100..60000ms. split1 moves BB scrambling
// and BCH to core 0 (bounded slices between shaped blocks); split0 is the original serial core 1.
// options (A3, 0 = original): 1 table-driven PL mapper copied to SRAM, 2 multiply-form
// 32APSK native-to-offset conversion from SCRATCH_Y (32APSK only), 4 run the fast
// mapper's leaves from cached flash instead (diagnostic; needs 1).
void bench_apsk_fec(int modulation, int cpw, int ms, int split, int options);
