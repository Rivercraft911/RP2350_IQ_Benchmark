// Runtime label mapping, shaping, and digital output; no FEC/RF.
#pragma once
// schedule0: core0 maps/shapes;1: core1 maps,core0 shapes;2: alternate blocks.
// mapper0: weighted32/packed16;1: pair32;2: canonical pair32;3: byte offsets.
// cpw3..8,100..60000ms. The16 mapper requires mapper0.
// placement0: cached flash;1: per-core SRAM, only mapper3/modulation32.
void bench_apsk_live(int cpw, int ms, int schedule, int mapper, int modulation, int placement);
