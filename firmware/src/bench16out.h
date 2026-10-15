// Continuous 16APSK component output; borrows the idle transmitter's arenas.
#pragma once

// L8/N2, gamma2.70, cyclic PRBS labels, RAW_PAIRS GPIO output and pin capture.
// cpw4/6/8; kernels0..3 are C, native burst, sparse burst, paired sparse burst.
// Core1 is idle.
// This is a shaper/DMA/PIO experiment, without FEC, SPI input, AFE or RF claims.
void bench_16out(int cpw, int ms, int kernel);
