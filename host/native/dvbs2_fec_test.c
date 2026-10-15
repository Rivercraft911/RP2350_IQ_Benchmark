// Include the production translation unit so its portable tiled transpose can be checked
// directly without adding a test-only firmware API. Device assembly still needs device CRCs.
#include "dvbs2.c"
#include "dvbs2_codes.h"
#include <stdio.h>

static uint32_t rng = 0xA13F6B29u;
static uint32_t random_word(void) {
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
    return rng;
}

static int matrix_tests(void) {
    for (unsigned pattern = 0; pattern < 1024 + 128; pattern++) {
        uint32_t a[32] = {0}, expected[32] = {0};
        if (pattern < 1024) a[pattern / 32] = 1u << (pattern % 32);
        else for (unsigned row = 0; row < 32; row++) a[row] = random_word();
        for (unsigned row = 0; row < 32; row++)
            for (unsigned col = 0; col < 32; col++)
                expected[col] |= ((a[row] >> (31u - col)) & 1u) << (31u - row);
        transpose32(a);
        if (memcmp(a, expected, sizeof a)) return 0;
    }
    return 1;
}

static uint32_t mapped_reference(uint32_t word, uint32_t swap, uint32_t flip) {
    uint32_t mapped = 0;
    for (unsigned j = 0; j < 16; j++) {
        uint32_t i = (word >> (31u - 2u * j)) & 1u, q = (word >> (30u - 2u * j)) & 1u;
        if ((swap >> (2u * j)) & 1u) { const uint32_t t = i; i = q; q = t; }
        i ^= (flip >> (2u * j)) & 1u;
        q ^= (flip >> (2u * j + 1u)) & 1u;
        mapped |= i << j | q << (16u + j);
    }
    return mapped;
}

static int mapping_tests(void) {
    enum { WORDS = 37, RING = 32, CANARY = 0x35C96A17 };
    uint32_t input[WORDS], swap[WORDS], flip[WORDS];
    struct { uint32_t before, words[RING], after; } got, want;
    dvbs2_init(&DVBS2_CODES[S2_N2_3], true);
    for (unsigned k = 0; k < WORDS; k++) {
        input[k] = random_word(); swap[k] = random_word() & 0x55555555u; flip[k] = random_word();
        if (map_scramble16(input[k], swap[k], flip[k]) != mapped_reference(input[k], swap[k], flip[k]))
            return 0;
    }
    for (unsigned fill = 0; fill < 16; fill++) {
        memset(got.words, 0xA5, sizeof got.words);
        got.before = got.after = CANARY;
        want = got;
        const uint32_t mask = (1u << fill) - 1u;
        symstream_t s = {.out = got.words, .mask = RING - 1, .n = RING - 3,
                         .fill = fill, .acc_i = random_word() & mask, .acc_q = random_word() & mask};
        uint32_t n = s.n, ai = s.acc_i, aq = s.acc_q, pos = fill;
        map_words(input, swap, flip, 0, &s, r0);
        if (s.n != n || s.fill != pos || s.acc_i != ai || s.acc_q != aq ||
            memcmp(got.words, want.words, sizeof got.words)) return 0;
        map_words(input, swap, flip, WORDS, &s, r0);
        for (unsigned k = 0; k < WORDS; k++) {
            const uint32_t mapped = mapped_reference(input[k], swap[k], flip[k]);
            for (unsigned j = 0; j < 16; j++) {
                ai |= ((mapped >> j) & 1u) << pos;
                aq |= ((mapped >> (16u + j)) & 1u) << pos;
                if (++pos == 16) {
                    want.words[n++ & (RING - 1)] = ai | aq << 16;
                    ai = aq = pos = 0;
                }
            }
        }
        if (s.n != n || s.fill != pos || s.acc_i != ai || s.acc_q != aq ||
            memcmp(got.words, want.words, sizeof got.words) ||
            got.before != CANARY || got.after != CANARY) return 0;
    }
    return 1;
}

static int parity_tests(void) {
    enum { PAR_WORDS = 21600 / 32 + 1 };
    const dvbs2_code_t *code = &DVBS2_CODES[S2_N2_3];
    uint32_t input[DVBS2_FE_WORDS], expected[PAR_WORDS];
    struct { uint32_t before, words[PAR_WORDS], after; } output;
    for (unsigned pattern = 0; pattern < 8; pattern++) {
        for (unsigned k = 0; k < DVBS2_FE_WORDS; k++)
            input[k] = pattern == 0 ? 0 : pattern == 1 ? ~0u :
                       pattern == 2 ? 0xAAAAAAAAu : pattern == 3 ? 0x55555555u : random_word();
        // Make stale P rows/scratch nonzero before reconfiguring to the q60 path.
        dvbs2_init(&DVBS2_CODES[S2_N1_4], false);
        uint32_t old_parity[DVBS2_FE_WORDS];
        dvbs2_ldpc(input, old_parity);
        dvbs2_init(code, true);
        memset(output.words, 0xA5, sizeof output.words);
        output.before = output.after = 0xCA85319Du;
        dvbs2_ldpc(input, output.words);
        dvbs2_ldpc_serial(input, expected);
        if (memcmp(output.words, expected, sizeof expected) ||
            output.before != 0xCA85319Du || output.after != 0xCA85319Du) return 0;
    }
    return 1;
}

int main(void) {
    const int matrix = matrix_tests(), parity = parity_tests(), mapping = mapping_tests();
    printf("FEC native: transpose1152 matrices=%s; q60 parity8 patterns/minimum buffer=%s; "
           "mapping16 partial fills/ring wrap=%s\n",
           matrix ? "PASS" : "FAIL", parity ? "PASS" : "FAIL", mapping ? "PASS" : "FAIL");
    return !(matrix && parity && mapping);
}
