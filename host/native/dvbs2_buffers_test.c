// Interleave three different BBFRAMEs through two FE buffers. The second frame remains
// live while the original FE is reused for frame three; the symbol stream is never flushed
// between frames. Python checks every resulting word against its independent transmitter.
#include <stdio.h>
#include <string.h>

#include "dvbs2_codes.h"
#include "iqgen.h"

enum { FRAMES = 3, STREAM_WORDS = (FRAMES * 33282 + 15) / 16, GUARD = 0x3C91A527 };
static uint32_t bb[FRAMES][DVBS2_FE_WORDS], saved_bb[FRAMES][DVBS2_FE_WORDS];
static struct { uint32_t before, words[DVBS2_FE_WORDS], after; } other;
static struct { uint32_t before, words[STREAM_WORDS], after; } output;
static uint32_t sequential[STREAM_WORDS], first_kernel[STREAM_WORDS];

static uint32_t hash_words(const uint32_t *p, uint32_t n) {
    uint32_t h = 2166136261u;
    for (uint32_t k = 0; k < n; k++) h = (h ^ p[k]) * 16777619u;
    return h;
}

static void make_bb(const dvbs2_code_t *c, unsigned ci) {
    for (unsigned f = 0; f < FRAMES; f++) {
        uint32_t seed = 0xD1B54A35u ^ ci ^ (f * 0x9E3779B9u);
        memset(bb[f], 0xA5, sizeof bb[f]);
        prbs_fill(bb[f], (c->kbch + 31u) / 32, &seed);
        // Include the proposed runtime phase marker and keep unused final bits nonzero:
        // begin_into must mask the BB tail rather than relying on a padded caller.
        bb[f][0] ^= (f & 1u) << 31;
    }
    memcpy(saved_bb, bb, sizeof bb);
}

static int check_chunks(const dvbs2_code_t *c, uint32_t chunk) {
    static uint32_t expected[DVBS2_FE_WORDS], serial[7], scrambled[DVBS2_FE_WORDS];
    uint32_t *frame = other.words;
    int ok = 1;
    for (unsigned f = 0; f < FRAMES; f++) {
        dvbs2_frame_begin_into(bb[f], expected);
        memcpy(scrambled, bb[f], sizeof scrambled);
        dvbs2_bbscramble(scrambled);
        dvbs2_bch_serial(scrambled, serial);
        memset(frame, 0xA5, sizeof other.words);
        dvbs2_frontend_t state, untouched;
        memset(&state, 0x5A, sizeof state);
        memcpy(&untouched, &state, sizeof state);
        other.before = other.after = GUARD;
        const int supported = (c->t * (c->is_short ? 14u : 16u) == 160u ||
                               c->t * (c->is_short ? 14u : 16u) == 128u) &&
                              dvbs2_bch_get_kernel() != S2_BCH_BASELINE;
        const uint32_t before = hash_words(frame, DVBS2_FE_WORDS);
        const bool began = dvbs2_frontend_begin(bb[f], frame, &state);
        ok &= began == supported;
        if (!began) {
            ok &= !memcmp(&state, &untouched, sizeof state) &&
                  hash_words(frame, DVBS2_FE_WORDS) == before;
            // This is the documented caller fallback; unsupported init did no work.
            dvbs2_frame_begin_into(bb[f], frame);
        } else {
            ok &= !state.done && !state.cursor;
            ok &= !dvbs2_frontend_step(&state, 0) && !state.cursor;
            uint32_t calls = 0;
            for (;;) {
                const uint32_t previous = state.cursor;
                const bool done = dvbs2_frontend_step(&state, chunk);
                ok &= state.cursor >= previous && state.cursor - previous <= chunk;
                ok &= done == state.done;
                if (done) break;
                if (++calls > DVBS2_FE_WORDS) return 0;
            }
            ok &= state.cursor == c->kbch / 32u && !memcmp(state.parity, serial, 6 * sizeof(uint32_t));
            const uint32_t completed = hash_words(frame, DVBS2_FE_WORDS);
            ok &= dvbs2_frontend_step(&state, 0) && dvbs2_frontend_step(&state, chunk);
            ok &= hash_words(frame, DVBS2_FE_WORDS) == completed;
        }
        ok &= !memcmp(frame, expected, sizeof other.words);
        ok &= other.before == GUARD && other.after == GUARD;
        ok &= !memcmp(bb, saved_bb, sizeof bb);
    }
    return ok;
}

static int run_interleaved(const dvbs2_code_t *c, symstream_t *s, uint32_t chunk) {
    uint32_t *a = dvbs2_frame_buffer(), *b = other.words;
    other.before = other.after = output.before = output.after = GUARD;
    memset(other.words, 0x5A, sizeof other.words);
    memset(output.words, 0xA5, sizeof output.words);
    *s = (symstream_t){.out = output.words, .mask = ~0u};
    int ok = a != b;
    dvbs2_frontend_t sa, sb;

    if (chunk) {
        ok &= dvbs2_frontend_begin(bb[0], a, &sa);
        ok &= dvbs2_frontend_begin(bb[1], b, &sb);
        // Alternate chunks with both contexts live, then finish A before B.
        while (!sa.done) {
            dvbs2_frontend_step(&sa, chunk);
            if (!sb.done) dvbs2_frontend_step(&sb, chunk);
        }
    } else dvbs2_frame_begin_into(bb[0], a);
    const uint32_t a_begin = hash_words(a, DVBS2_FE_WORDS);
    if (!chunk) dvbs2_frame_begin_into(bb[1], b);
    ok &= hash_words(a, DVBS2_FE_WORDS) == a_begin;
    const uint32_t b_begin = hash_words(b, DVBS2_FE_WORDS);
    dvbs2_frame_ldpc_into(a);
    const uint32_t a_done = hash_words(a, DVBS2_FE_WORDS);
    dvbs2_frame_finish_from(a, s);
    ok &= hash_words(a, DVBS2_FE_WORDS) == a_done;

    // A's prior frame has been consumed, B's BB+BCH is still waiting for the backend.
    if (chunk) {
        ok &= dvbs2_frontend_begin(bb[2], a, &sa);
        while (!sa.done) dvbs2_frontend_step(&sa, chunk);
        while (!sb.done) dvbs2_frontend_step(&sb, chunk);
    } else dvbs2_frame_begin_into(bb[2], a);
    ok &= hash_words(b, DVBS2_FE_WORDS) == b_begin;
    const uint32_t a_next = hash_words(a, DVBS2_FE_WORDS);
    dvbs2_frame_ldpc_into(b);
    dvbs2_frame_finish_from(b, s);
    ok &= hash_words(a, DVBS2_FE_WORDS) == a_next;
    dvbs2_frame_ldpc_into(a);
    dvbs2_frame_finish_from(a, s);

    ok &= !memcmp(bb, saved_bb, sizeof bb);
    ok &= other.before == GUARD && other.after == GUARD;
    ok &= output.before == GUARD && output.after == GUARD;
    ok &= s->n * 16u + s->fill == FRAMES * dvbs2_plframe_symbols();
    (void)c;
    return ok;
}

int main(int argc, char **argv) {
    int failed = 0;
    for (unsigned ci = 0; ci < N_DVBS2_CODES; ci++) {
        const dvbs2_code_t *c = &DVBS2_CODES[ci];
        if (argc > 1 && strcmp(argv[1], c->name)) continue;
        make_bb(c, ci);
        for (int pilots = 0; pilots <= 1; pilots++) {
            int ok = 1;
            symstream_t s = {0};
            uint32_t unflushed_n = 0, fill = 0, acc_i = 0, acc_q = 0, first_n = 0;
            for (int kernel = S2_BCH_BASELINE; kernel <= S2_BCH_T8_ARM; kernel++) {
                ok &= dvbs2_bch_set_kernel((dvbs2_bch_kernel_t)kernel);
                dvbs2_init(c, pilots);
                symstream_t old = {.out = sequential, .mask = ~0u};
                for (unsigned f = 0; f < FRAMES; f++) dvbs2_frame(bb[f], &old);

                const uint32_t chunks[] = {1, 7, 128, 511};
                for (unsigned bank = 0; bank <= 1; bank++) {
                    ok &= dvbs2_frontend_set_bank(bank);
                    for (unsigned k = 0; k < sizeof chunks / sizeof chunks[0]; k++) {
                        ok &= check_chunks(c, chunks[k]);
                        if ((c->t * (c->is_short ? 14u : 16u) == 160u ||
                             c->t * (c->is_short ? 14u : 16u) == 128u) && kernel != S2_BCH_BASELINE) {
                            ok &= run_interleaved(c, &s, chunks[k]);
                            ok &= s.n == old.n && s.fill == old.fill && s.acc_i == old.acc_i &&
                                  s.acc_q == old.acc_q && !memcmp(output.words, sequential, s.n * 4u);
                        }
                    }
                }
                ok &= run_interleaved(c, &s, 0);
                ok &= s.n == old.n && s.fill == old.fill && s.acc_i == old.acc_i &&
                      s.acc_q == old.acc_q && !memcmp(output.words, sequential, s.n * 4u);
                unflushed_n = s.n; fill = s.fill; acc_i = s.acc_i; acc_q = s.acc_q;
                symstream_flush(&s);
                symstream_flush(&old);
                ok &= s.n == old.n && !memcmp(output.words, sequential, s.n * 4u);
                ok &= output.before == GUARD && output.after == GUARD;
                if (kernel == S2_BCH_BASELINE) {
                    first_n = s.n;
                    memcpy(first_kernel, output.words, s.n * 4u);
                } else ok &= s.n == first_n && !memcmp(first_kernel, output.words, s.n * 4u);
            }
            if (!ok) fprintf(stderr, "FE buffer test failed: %s pilots=%d\n", c->name, pilots);
            failed |= !ok;
            printf("{\"code\":\"%s\",\"ci\":%u,\"pilots\":%d,\"ok\":%d,\"frames\":%d,"
                   "\"syms\":%u,\"unflushed_words\":%u,\"fill\":%u,\"acc_i\":%u,\"acc_q\":%u,\"words\":[",
                   c->name, ci, pilots, ok, FRAMES, FRAMES * dvbs2_plframe_symbols(),
                   unflushed_n, fill, acc_i, acc_q);
            for (uint32_t k = 0; k < s.n; k++) printf("%s%u", k ? "," : "", output.words[k]);
            puts("]}");
        }
    }
    return failed ? 1 : 0;
}
