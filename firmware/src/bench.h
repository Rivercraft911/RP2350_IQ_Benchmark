// Benchmarks: shaper kernel throughput, DVB-S2 encoder stages, DMA sniffer CRC modes.
// They borrow the transmitter's buffers (tx.h) while nothing streams.
#pragma once
#include <stdbool.h>

#include "iqgen.h"

void bench_kernel(const iq_kernel_info_t *k, int reps, bool tables_ok);
void bench_dvbs2(int code, bool pilots, int reps);
void bench_sniffer(void);
