// Binary output for the independent Python 8PSK shaper test; no device/hardware access.
#include "iq8.h"
#include "coeffs.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { GROUPS = 137, WORDS = GROUPS * 16 * IQ8_SPS };
int main(int argc, char **argv) {
    uint32_t seed = argc > 1 ? (uint32_t)strtoul(argv[1], 0, 0) : 0x1234abcdu;
    unsigned block = argc > 2 ? (unsigned)strtoul(argv[2], 0, 0) : GROUPS;
    if (!block || block > GROUPS) return 2;
    static uint32_t ta[IQ8_TABLE_WORDS], tb[IQ8_TABLE_WORDS], out[WORDS];
    static iq8_input_t in[GROUPS];
    iq8_tables_t tables = {ta, tb};
    const int32_t *coef = NULL;
    for (unsigned k = 0; k < N_COEF_SETS; ++k)
        if (COEF_SETS[k].sps == IQ8_SPS && COEF_SETS[k].L == IQ8_L) coef = COEF_SETS[k].c;
    if (!coef) return 3;
    iq8_build_tables(coef, &tables);
    iq8_fill(in, GROUPS, &seed);
    for (unsigned pos = 0; pos < GROUPS; pos += block) {
        unsigned n = GROUPS - pos;
        if (n > block) n = block;
        iq8_kernel_c(out + pos * 16 * IQ8_SPS, in + pos, n, pos ? in + pos - 1 : NULL, &tables);
    }
    return fwrite(out, sizeof out[0], WORDS, stdout) == WORDS ? 0 : 4;
}
