// PRBS-only L8 PIO-pointer -> FIFO-control/sample DMA -> paired output trial.
#pragma once

// cpw1/2,100..60000ms. fault0 runs normally; fault1 stops source grants at ms/4.
// Core1 and transmitter/SPI must be idle. Uses existing public backend cleanup;
// its output-DMA abort wait is inherited and has no separate timeout here.
void bench_quadlive(int cpw, int ms, int fault);
