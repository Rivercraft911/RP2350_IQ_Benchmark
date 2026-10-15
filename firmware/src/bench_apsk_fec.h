// Complete two-core normal5/6 APSK pipeline, fed by on-chip PV-SPI GCS traffic.
#pragma once
// modulation16/32, PIO clocks/bus word3..32, duration100..60000ms. split1 moves BB scrambling
// and BCH to core 0 (bounded slices between shaped blocks); split0 is the original serial core 1.
void bench_apsk_fec(int modulation, int cpw, int ms, int split);
