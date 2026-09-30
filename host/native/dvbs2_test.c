// Host build of firmware/src/dvbs2.c. For each code and pilot setting: check the fast BCH and
// LDPC against the bit-serial forms, then print the PLFRAME in shaper-format words for comparison
// with reference/dvbs2/dvbs2.py. Input: xorshift32 words (seed per code) as the unscrambled
// BBFRAME, truncated to Kbch bits.
#include <stdio.h>
#include <string.h>

#include "dvbs2_codes.h"
#include "iqgen.h"

int main(int argc, char **argv) {
    static uint32_t bb[64800 / 32 + 2], fe[64800 / 32 + 2], p1[64800 / 32 + 2], p2[64800 / 32 + 2];
    static uint32_t out[33282 / 16 + 2];
    const char *only = argc > 1 ? argv[1] : 0;
    for (int ci = 0; ci < N_DVBS2_CODES; ci++) {
        const dvbs2_code_t *c = &DVBS2_CODES[ci];
        if (only && strcmp(only, c->name)) continue;
        for (int pil = 0; pil < 2; pil++) {
            dvbs2_init(c, pil);
            uint32_t st = 0x9E3779B9u ^ (uint32_t)ci, nw = (c->kbch + 31) / 32;
            memset(bb, 0, sizeof bb);
            prbs_fill(bb, nw, &st);
            if (c->kbch % 32) bb[nw - 1] &= ~(~0u >> (c->kbch % 32));
            // BCH and LDPC: fast vs serial on the scrambled frame
            memcpy(fe, bb, sizeof fe);
            dvbs2_bbscramble(fe);
            dvbs2_bch(fe, p1), dvbs2_bch_serial(fe, p2);
            const int bch_ok = !memcmp(p1, p2, 24);
            for (uint32_t k = 0; k < 64800 / 32 + 2; k++) fe[k] = st = st * 1664525u + 1013904223u;
            dvbs2_ldpc(fe, p1), dvbs2_ldpc_serial(fe, p2);
            const uint32_t m = c->nldpc - c->kldpc;
            int ldpc_ok = !memcmp(p1, p2, m / 32 * 4);
            if (m % 32) ldpc_ok &= ((p1[m / 32] ^ p2[m / 32]) & ~(~0u >> (m % 32))) == 0;
            symstream_t s = {.out = out};
            dvbs2_frame(bb, &s);
            symstream_flush(&s);
            printf("{\"code\":\"%s\",\"pilots\":%d,\"seed\":%u,\"bch_ok\":%d,\"ldpc_ok\":%d,"
                   "\"syms\":%u,\"words\":[", c->name, pil, 0x9E3779B9u ^ (uint32_t)ci, bch_ok,
                   ldpc_ok, dvbs2_plframe_symbols());
            for (uint32_t k = 0; k < s.n; k++) printf("%s%u", k ? "," : "", out[k]);
            printf("]}\n");
        }
    }
    return 0;
}
