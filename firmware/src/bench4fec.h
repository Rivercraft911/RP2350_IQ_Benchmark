// Opt-in live SPI/GCS 8PSK 5/6 FEC through the four-level L7/N4 hardware shaper.
#pragma once
// prio: dual_dma bus-priority mask0..15 (PROC0, PROC1, DMA_R, DMA_W register fields).
void bench4fec(int cpw, int ms, int invert, int prio, int bits);
