#include "iq32mapkeys_pair.h"
#include <stdbool.h>

static const uint8_t canonical[132] = {
    0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,
    16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,
    1,2,3,0,7,8,9,10,11,12,13,14,15,4,5,6,
    20,21,22,23,24,25,26,27,28,29,30,31,16,17,18,19,
    2,3,0,1,10,11,12,13,14,15,4,5,6,7,8,9,
    24,25,26,27,28,29,30,31,16,17,18,19,20,21,22,23,
    3,0,1,2,13,14,15,4,5,6,7,8,9,10,11,12,
    28,29,30,31,16,17,18,19,20,21,22,23,24,25,26,27,
    18,22,26,30,
};

uint8_t iq32mapkeys_canonical(uint8_t token) { return canonical[token]; }

void iq32mapkeys_pair_build(uint32_t table[IQ32MAPKEYS_PAIR_WORDS]) {
    for (unsigned first = 0; first < 32; ++first) {
        const uint32_t c = iq32mapkeys_coordinate(first);
        for (unsigned second = 0; second < 32; ++second) {
            const uint32_t d = iq32mapkeys_coordinate(second);
            uint32_t *row = table + 4*(32*first + second);
            row[0] = 289u*c;
            row[1] = 17u*c + 289u*d;
            row[2] = c + 17u*d;
            row[3] = d;
        }
    }
}

static inline void encode(uint32_t *out, const uint8_t *tokens, uint32_t symbols,
                           iq32mapkeys_state_t *state, const uint32_t *table,
                           bool canonical_input) {
    if (!symbols) return;
    uint32_t last = state->last, pair = state->pair;
    uint32_t pairs = symbols / 2;
    while (pairs--) {
        const unsigned first = canonical_input ? tokens[0] : canonical[tokens[0]];
        const unsigned second = canonical_input ? tokens[1] : canonical[tokens[1]];
        const uint32_t *row = table + 4*(32*first + second);
        out[0] = pair + row[0];
        out[1] = last + row[1];
        pair = row[2];
        last = row[3];
        out += 2;
        tokens += 2;
    }
    state->last = last;
    state->pair = pair;
    if (symbols & 1u) iq32mapkeys_encode(out, tokens, 1, state);
}

void iq32mapkeys_pair_encode(uint32_t *out, const uint8_t *tokens,
                             uint32_t symbols, iq32mapkeys_state_t *state,
                             const uint32_t *table) {
    encode(out, tokens, symbols, state, table, false);
}

void iq32mapkeys_pair_canonical(uint32_t *out, const uint8_t *points,
                                uint32_t symbols, iq32mapkeys_state_t *state,
                                const uint32_t *table) {
    encode(out, points, symbols, state, table, true);
}
