#include "iq32mapkeys.h"

typedef struct { uint32_t coordinate, times17, times289; } point_t;
#define COORD(i,q) ((uint32_t)(i) | (uint32_t)(q)<<16)
#define P(i,q) {COORD(i,q), 17u*COORD(i,q), 289u*COORD(i,q)}
static const point_t points[132] = {
    // Quarter turn 0.
    P(1,1), P(9,1), P(9,9), P(1,9), P(4,2), P(3,3), P(2,4), P(10,4),
    P(11,3), P(12,2), P(12,10), P(11,11), P(10,12), P(2,12), P(3,11), P(4,10),
    P(8,0), P(7,5), P(6,6), P(5,7), P(0,8), P(13,7), P(14,6), P(15,5),
    P(16,0), P(15,13), P(14,14), P(13,15), P(0,16), P(5,15), P(6,14), P(7,13),
    // Quarter turn 1.
    P(9,1), P(9,9), P(1,9), P(1,1), P(10,4), P(11,3), P(12,2), P(12,10),
    P(11,11), P(10,12), P(2,12), P(3,11), P(4,10), P(4,2), P(3,3), P(2,4),
    P(0,8), P(13,7), P(14,6), P(15,5), P(16,0), P(15,13), P(14,14), P(13,15),
    P(0,16), P(5,15), P(6,14), P(7,13), P(8,0), P(7,5), P(6,6), P(5,7),
    // Quarter turn 2.
    P(9,9), P(1,9), P(1,1), P(9,1), P(12,10), P(11,11), P(10,12), P(2,12),
    P(3,11), P(4,10), P(4,2), P(3,3), P(2,4), P(10,4), P(11,3), P(12,2),
    P(16,0), P(15,13), P(14,14), P(13,15), P(0,16), P(5,15), P(6,14), P(7,13),
    P(8,0), P(7,5), P(6,6), P(5,7), P(0,8), P(13,7), P(14,6), P(15,5),
    // Quarter turn 3.
    P(1,9), P(1,1), P(9,1), P(9,9), P(2,12), P(3,11), P(4,10), P(4,2),
    P(3,3), P(2,4), P(10,4), P(11,3), P(12,2), P(12,10), P(11,11), P(10,12),
    P(0,16), P(5,15), P(6,14), P(7,13), P(8,0), P(7,5), P(6,6), P(5,7),
    P(0,8), P(13,7), P(14,6), P(15,5), P(16,0), P(15,13), P(14,14), P(13,15),
    P(6,6), // Unit pilot, quarter turn0.
    P(14,6), // Unit pilot, quarter turn1.
    P(14,14), // Unit pilot, quarter turn2.
    P(6,14), // Unit pilot, quarter turn3.
};
#undef P
#undef COORD

uint32_t iq32mapkeys_coordinate(uint8_t token) {
    return points[token].coordinate;
}

void iq32mapkeys_init(iq32mapkeys_state_t *state, const uint8_t previous[2]) {
    const uint32_t pilot = 6u | 6u<<16;
    state->last = previous ? points[previous[1]].coordinate : pilot;
    const uint32_t older = previous ? points[previous[0]].coordinate : pilot;
    state->pair = older + 17u*state->last;
}

void iq32mapkeys_encode(uint32_t *out, const uint8_t *tokens, uint32_t symbols,
                        iq32mapkeys_state_t *state) {
    if (!symbols) return;
    uint32_t last = state->last, pair = state->pair;
    do {
        const point_t *point = &points[*tokens++];
        *out++ = pair + point->times289;
        pair = last + point->times17;
        last = point->coordinate;
    } while (--symbols);
    state->last = last;
    state->pair = pair;
}

void iq32mapkeys_history(uint32_t out[5], const uint8_t previous[7]) {
    if (!previous) {
        for (unsigned i = 0; i < 5; ++i) out[i] = 1842u | 1842u<<16;
        return;
    }
    iq32mapkeys_state_t state;
    iq32mapkeys_init(&state, previous);
    iq32mapkeys_encode(out, previous+2, 5, &state);
}
