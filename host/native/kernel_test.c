// Host build of the firmware kernels: prints one JSON line per (kernel, sps, L) with the CRC of
// its output for a PRBS input, split into blocks to exercise the filter-history hand-off.
#include <stdio.h>
#include <stdlib.h>

#include "coeffs.h"
#include "iqgen.h"

enum { NWORDS = 2048, BLOCK = 64 };

int main(int argc, char **argv) {
    uint32_t seed = argc > 1 ? (uint32_t)strtoul(argv[1], 0, 0) : 0x1234ABCDu;
    static uint32_t in[NWORDS], out[NWORDS * 16 * 4], ti[1 << 13], tq[1 << 13];
    uint32_t st = seed;
    prbs_fill(in, NWORDS, &st);
    for (int s = 0; s < N_COEF_SETS; s++) {
        const coef_set_t *cs = &COEF_SETS[s];
        iq_cfg_t cfg = {cs->sps, cs->L, cs->c, ti, tq};
        iq_build_tables(&cfg);
        size_t tw = ((size_t)1 << cs->L) * cs->sps / 2 * 4;
        int tables_ok = crc32_update(0, ti, tw) == cs->crc_i && crc32_update(0, tq, tw) == cs->crc_q;
        for (int k = 0; k < IQ_N_KERNELS; k++) {
            const iq_kernel_info_t *ki = &IQ_KERNELS[k];
            if (ki->sps && (ki->sps != cs->sps || ki->L != cs->L)) continue;
            for (int b = 0; b < NWORDS; b += BLOCK)
                ki->fn(out + (size_t)b * 16 * cs->sps, in + b, BLOCK, b ? in[b - 1] : 0, &cfg);
            printf("{\"kernel\":\"%s\",\"layout\":%d,\"sps\":%d,\"L\":%d,\"seed\":%u,"
                   "\"nwords\":%d,\"tables_ok\":%d,\"crc\":%u}\n", ki->name, ki->layout, cs->sps, cs->L, seed, NWORDS,
                   tables_ok, crc32_update(0, out, (size_t)NWORDS * 16 * cs->sps * 4));
        }
    }
    return 0;
}
