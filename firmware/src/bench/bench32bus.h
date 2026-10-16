#pragma once

// Exclusive diagnostic borrow of BUSCTRL counters: prior counts are discarded.
// No DMA/output; mode selects the core1 load, group selects four bus events.
void bench_32bus(int mode, int group);
