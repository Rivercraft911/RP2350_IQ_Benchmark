// Isolated 8PSK shaper experiment. This is not a DVB-S2 8PSK encoder or streaming profile.
#pragma once
#include <stdint.h>

enum { IQ8_L = 12, IQ8_SPS = 4, IQ8_TABLE_WORDS = (1 << IQ8_L) * IQ8_SPS / 2 };

// Sixteen symbols/group: a contains I-plane-A bits [15:0], Q-plane-A [31:16]; b likewise B.
// Bit 0 means +1, bit 1 means -1. Each coordinate = A*sign_a + B*sign_b, with
// A=(cos(pi/8)+sin(pi/8))/2, B=(cos(pi/8)-sin(pi/8))/2. Only 8 of 16 joint states are used.
typedef struct { uint32_t a, b; } iq8_input_t;
typedef struct { uint32_t *a, *b; } iq8_tables_t;

// Each caller-owned table occupies 32 KiB; table samples are SIGNED 16-bit partial sums,
// with neither 14-bit wrapping nor IQ_FLAG. Round each plane separately, then sum and pack.
void iq8_build_tables(const int32_t *qpsk_coef, const iq8_tables_t *tables);
void iq8_fill(iq8_input_t *in, uint32_t groups, uint32_t *seed);

// Output PAIRS layout, groups*16*4 uint32_t words. prev carries the preceding group;
// NULL means repeated angle pi/8 before the input. Groups can be partitioned arbitrarily.
void iq8_kernel_c(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                  const iq8_input_t *prev, const iq8_tables_t *tables);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
void iq8_kernel_asm(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                    const iq8_input_t *prev, const iq8_tables_t *tables);
#endif
