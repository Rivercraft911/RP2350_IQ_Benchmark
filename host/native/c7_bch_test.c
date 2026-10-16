// Exercise C7's public preparation and per-worker APIs beside the production C
// fallback. Python supplies an independent polynomial and transmitter reference.
#include <string.h>

#include "dvbs2.c"
#include "dvbs2_codes.h"

unsigned c7_setup(unsigned ci, uint32_t *table, uint32_t *generator) {
    if (ci >= N_DVBS2_CODES) return 0;
    dvbs2_init(&DVBS2_CODES[ci], false);
    if (!dvbs2_bch_set_kernel(S2_BCH_T8_ARM)) return 0;
    if (table) memcpy(table, bch_tab, sizeof bch_tab);
    if (generator) memcpy(generator, bch_g, sizeof bch_g);
    return bch_deg;
}

int c7_leaf(unsigned variant, const uint32_t *input, uint32_t *parity,
            const void *table, uint32_t words) {
    dvbs2_bch_leaf_t leaf = dvbs2_bch_copy(NULL, 0, variant);
    if (!leaf) return 0;
    leaf(input, parity, table, words);
    return 1;
}

void c7_serial(const uint32_t *input, uint32_t *parity) {
    dvbs2_bch_serial(input, parity);
}

int c7_transpose(uint32_t *matrix) {
    dvbs2_transpose_fn transpose = dvbs2_transpose_copy(NULL, 0);
    if (!transpose) return 0;
    transpose(matrix);
    return 1;
}

// Retain the actual bounded frontend dispatch, final-byte handling, and append.
// UINT32_MAX chooses its ordinary dispatch; the other variants attach a caller's
// copied entry and table only after admission, exactly as the C3 worker does.
int c7_frontend(const uint32_t *input, uint32_t *frame, uint32_t *parity,
                unsigned options, unsigned variant, const void *table,
                uint32_t chunk, unsigned prepared) {
    dvbs2_frontend_t state;
    if (prepared) {
        const uint32_t count = (C->kbch + 31u) / 32u;
        memset(frame, 0, DVBS2_FE_WORDS * sizeof frame[0]);
        for (uint32_t k = 0; k < count; k++) frame[k] = input[k] ^ bb_prbs[k];
        if (C->kbch % 32u) frame[count - 1] &= ~(~0u >> (C->kbch % 32u));
        if (!dvbs2_frontend_begin_prepared(frame, &state, options)) return 0;
    } else if (!dvbs2_frontend_begin_reentrant_options(input, frame, &state, options)) return 0;
    if (((options & S2_FE_LDM_BCH) ? !state.leaf : !!state.leaf) ||
        state.table || state.cursor || state.done) return 0;
    if (variant != UINT32_MAX) {
        state.leaf = dvbs2_bch_copy(NULL, 0, variant);
        if (!state.leaf) return 0;
        state.table = table;
    }
    if (dvbs2_frontend_step(&state, 0) || state.cursor) return 0;
    unsigned calls = 0;
    while (!state.done) {
        const uint32_t before = state.cursor;
        const bool done = dvbs2_frontend_step(&state, chunk);
        if (done != state.done || state.cursor < before || state.cursor - before > chunk) return 0;
        if (++calls > DVBS2_FE_WORDS) return 0;
    }
    if (state.cursor != C->kbch / 32u) return 0;
    uint32_t complete[DVBS2_FE_WORDS];
    memcpy(complete, frame, sizeof complete);
    if (!dvbs2_frontend_step(&state, 0) || !dvbs2_frontend_step(&state, chunk) ||
        memcmp(complete, frame, sizeof complete)) return 0;
    memcpy(parity, state.parity, BCH_W * sizeof parity[0]);
    return 1;
}

// Keep two different source frames, function overrides, tables, and remainders
// live at once. Alternating unequal slices exposes accidental shared state.
int c7_frontend_pair(const uint32_t *input_a, const uint32_t *input_b,
                     uint32_t *frame_a, uint32_t *frame_b, const void *table3) {
    dvbs2_frontend_t state[2];
    if (!dvbs2_frontend_begin_reentrant_options(input_a, frame_a, &state[0], S2_FE_LDM_BCH) ||
        !dvbs2_frontend_begin_reentrant_options(input_b, frame_b, &state[1], 0)) return 0;
    state[0].leaf = dvbs2_bch_copy(NULL, 0, 1);
    state[1].leaf = dvbs2_bch_copy(NULL, 0, 2);
    state[0].table = bch_tab;
    state[1].table = table3;
    if (!state[0].leaf || !state[1].leaf) return 0;
    unsigned calls = 0;
    while (!state[0].done || !state[1].done) {
        for (unsigned i = 0; i < 2; i++) {
            const uint32_t before = state[i].cursor, chunk = i ? 128u : 7u;
            const bool done = dvbs2_frontend_step(&state[i], chunk);
            if (done != state[i].done || state[i].cursor < before ||
                state[i].cursor - before > chunk) return 0;
        }
        if (++calls > DVBS2_FE_WORDS) return 0;
    }
    return state[0].table == bch_tab && state[1].table == table3 &&
           state[0].cursor == C->kbch / 32u && state[1].cursor == C->kbch / 32u;
}

// Failed admission must leave both caller objects byte-identical. This covers
// unknown options and the unsupported short-frame degree168 configuration.
int c7_rejects_without_writes(unsigned options) {
    uint32_t frame[DVBS2_FE_WORDS], before[DVBS2_FE_WORDS];
    dvbs2_frontend_t state, saved;
    memset(frame, 0xA5, sizeof frame);
    memcpy(before, frame, sizeof before);
    memset(&state, 0x5A, sizeof state);
    memcpy(&saved, &state, sizeof saved);
    if (dvbs2_frontend_begin_reentrant_options(frame, frame, &state, options)) return 0;
    if (memcmp(before, frame, sizeof before) || memcmp(&saved, &state, sizeof saved)) return 0;
    if (dvbs2_frontend_begin_prepared(frame, &state, options)) return 0;
    return !memcmp(before, frame, sizeof before) && !memcmp(&saved, &state, sizeof saved);
}
