// Complete two-core normal5/6 APSK pipeline, fed by on-chip PV-SPI GCS traffic.
#pragma once
// modulation16/32, PIO clocks/bus word3..32, duration100..60000ms.
void bench_apsk_fec(int modulation, int cpw, int ms);
