// Benchmarks: shaper kernel throughput, DVB-S2 encoder stages, DMA sniffer CRC modes.
// They borrow the transmitter's buffers (tx.h) while nothing streams.
#pragma once
#include <stdbool.h>

#include "iqgen.h"

void bench_kernel(const iq_kernel_info_t *k, int reps, bool tables_ok);
void bench_dvbs2(int code, bool pilots, int reps);
void bench_sniffer(void);
void bench_pv(int reps);
void bench_pvstress(int ms, int irq_mask_us);
void bench_8psk(int reps);
void bench_8quad(int ms, int reps);
void bench_8map(int code, bool pilots, int reps);
void bench_16chunk(int reps);
void bench_16native8(int reps);
void bench_32native8(int reps);
void bench_hstx_ddr(int ms);
void bench_descriptors(int reps);
bool bench_dual_set_quad_profile(unsigned profile);
void bench_dual_pointer(int cpw, int ms, int source, int fault, int code_index);
void bench_dual_pointer4(int cpw, int ms, int source, int fault, int code_index);
void bench_dual_stream(int cpw, int ms, int kernel, int source, int cap_words, int fault,
                       int schedule, int divider256, int row_symbols, int code_index);
