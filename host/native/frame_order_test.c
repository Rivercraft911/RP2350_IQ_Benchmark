// Ownership model and staged PLFRAME append, including out-of-order FEC completion.
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "dvbs2_codes.h"
#include "frame_order.h"
#include "iqgen.h"

enum { FRAMES = 10, RING_WORDS = 4096, GUARD_WORDS = 64,
       STAGE_WORDS = (33282 + 15) / 16, OUTPUT_WORDS = FRAMES * STAGE_WORDS,
       CANARY = 0x6C9253A1 };
static uint32_t bb[2][DVBS2_FE_WORDS], fe_other[DVBS2_FE_WORDS];
static struct { uint32_t before, words[STAGE_WORDS], after; } staged[2];
static struct { uint32_t before, words[RING_WORDS], after; } ring;
static uint32_t serial[OUTPUT_WORDS], result[OUTPUT_WORDS];

static void queue_units(void) {
    frame_order_t q;
    frame_order_ticket_t t = {0};
    assert(!frame_order_init(&q, 0, 0, 3, 0));
    assert(!frame_order_init(&q, 0, 0, 16, 16));
    assert(frame_order_init(&q, 0xFFFFFFFEu, 0xFFFFFFFEu, 16, 2));
    assert(!frame_order_input_begin(&q, 1, 0xFFFFFFFFu));
    assert(!frame_order_input_begin(&q, 2, 0xFFFFFFFEu));
    assert(frame_order_input_begin(&q, 0, 0xFFFFFFFEu));
    assert(!frame_order_input_begin(&q, 0, 0xFFFFFFFEu));
    assert(!frame_order_input_done(&q, 0, 0));
    assert(!frame_order_ready(&q, 0, 0xFFFFFFFEu, 1));
    assert(frame_order_input_done(&q, 0, 0xFFFFFFFEu));
    assert(!frame_order_ready(&q, 0, 0xFFFFFFFEu, 0));
    assert(frame_order_ready(&q, 0, 0xFFFFFFFEu, 15));
    assert(!frame_order_ready(&q, 0, 0xFFFFFFFEu, 15));
    assert(!frame_order_output_begin(&q, 0xFFFFFFFFu, &t)); // consumer cannot read unpublished words
    assert(frame_order_output_begin(&q, 0xFFFFFFFEu, &t));
    assert(t.first_word == 0xFFFFFFFEu && t.end_word == 0xFFFFFFFEu && t.fill == 15);
    assert(!frame_order_input_begin(&q, 0, 0)); // COPYING still owns the entire staging slot
    assert(!frame_order_output_begin(&q, 0xFFFFFFFEu, &t));
    assert(!frame_order_output_done(&q, 0));
    assert(frame_order_output_done(&q, 0xFFFFFFFEu));
    assert(frame_order_published(&q) == 0xFFFFFFFEu && q.fill == 15);
    assert(frame_order_input_begin(&q, 1, 0xFFFFFFFFu));
    assert(frame_order_input_done(&q, 1, 0xFFFFFFFFu));
    assert(frame_order_ready(&q, 1, 0xFFFFFFFFu, 33));
    assert(frame_order_output_begin(&q, 0xFFFFFFFEu, &t));
    assert(t.first_word == 0xFFFFFFFEu && t.end_word == 1 && !t.fill);
    assert(frame_order_output_done(&q, 0xFFFFFFFFu));
    assert(q.next_output == 0 && frame_order_published(&q) == 1);
    assert(frame_order_input_begin(&q, 0, 0));
    assert(frame_order_input_done(&q, 0, 0));
    assert(frame_order_ready(&q, 0, 0, 12 * 16));
    assert(!frame_order_output_begin(&q, 0xFFFFFFFEu, &t)); // 3+12 words exceed 16-2
    assert(frame_order_output_begin(&q, 0xFFFFFFFFu, &t)); // exact guard boundary: 2+12
    assert(frame_order_output_done(&q, 0));
    assert(frame_order_published(&q) == 13);
    assert(!frame_order_input_begin(&q, 0, 0)); // stale generation after reuse
    assert(!frame_order_input_begin(&q, 0, 2)); // stateful input still needs odd frame 1
}

static void make_bb(uint32_t *out, const dvbs2_code_t *c, unsigned sequence) {
    uint32_t seed = 0xD1B54A35u ^ (sequence * 0x9E3779B9u);
    memset(out, 0xA5, DVBS2_FE_WORDS * sizeof *out);
    prbs_fill(out, (c->kbch + 31u) / 32u, &seed);
    out[0] ^= (sequence & 1u) << 31;
}

static void finish(frame_order_t *q, unsigned sequence, uint32_t symbols) {
    const unsigned core = sequence & 1u;
    uint32_t *frame = core ? fe_other : dvbs2_frame_buffer();
    staged[core].before = staged[core].after = CANARY;
    memset(staged[core].words, 0xA5, sizeof staged[core].words);
    dvbs2_frame_begin_into(bb[core], frame);
    dvbs2_frame_ldpc_into(frame); // shared LDPC scratch: these calls are deliberately serialized
    symstream_t s = {.out = staged[core].words, .mask = ~0u};
    dvbs2_frame_finish_from(frame, &s);
    assert(s.n * 16u + s.fill == symbols);
    symstream_flush(&s); // local staging padding is never appended to the destination
    assert(staged[core].before == CANARY && staged[core].after == CANARY);
    assert(frame_order_ready(q, core, sequence, symbols));
}

static void append(frame_order_t *q, symstream_t *s, const frame_order_ticket_t *t) {
    const unsigned core = t->sequence & 1u;
    assert(s->n == t->first_word);
    for (unsigned word = 0; word < t->symbols / 16u; word++)
        symstream_put16(s, staged[core].words[word]);
    if (t->symbols % 16u)
        symstream_put(s, staged[core].words[t->symbols / 16u], t->symbols % 16u);
    assert(s->n == t->end_word && s->fill == t->fill);
    assert(frame_order_output_done(q, t->sequence));
    assert(frame_order_published(q) == s->n && q->fill == s->fill);
    assert(ring.before == CANARY && ring.after == CANARY);
}

static void drain(const frame_order_t *q, uint32_t *retired) {
    const uint32_t published = frame_order_published(q);
    while (*retired != published) {
        result[*retired] = ring.words[*retired & (RING_WORDS - 1u)];
        (*retired)++;
    }
}

static void run(unsigned pilots) {
    const dvbs2_code_t *c = &DVBS2_CODES[8]; // normal 5/6, the Yapogee profile
    dvbs2_init(c, pilots != 0);
    assert(dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM));
    assert(dvbs2_map_set_kernel(1) && dvbs2_ldpc_set_pack(1));
    const uint32_t symbols = dvbs2_plframe_symbols();
    symstream_t expected = {.out = serial, .mask = ~0u};
    for (unsigned sequence = 0; sequence < FRAMES; sequence++) {
        make_bb(bb[0], c, sequence);
        dvbs2_frame(bb[0], &expected);
    }

    frame_order_t q;
    assert(frame_order_init(&q, 0, 0, RING_WORDS, GUARD_WORDS));
    ring.before = ring.after = CANARY;
    symstream_t stream = {.out = ring.words, .mask = RING_WORDS - 1u};
    uint32_t retired = 0, blocked = 0;
    frame_order_ticket_t t;
    for (unsigned sequence = 0; sequence < FRAMES; sequence += 2) {
        for (unsigned core = 0; core < 2; core++) {
            assert(frame_order_input_begin(&q, core, sequence + core));
            make_bb(bb[core], c, sequence + core);
            assert(frame_order_input_done(&q, core, sequence + core));
        }
        finish(&q, sequence + 1u, symbols); // odd frame finishes first
        assert(!frame_order_output_begin(&q, retired, &t));
        assert(frame_order_published(&q) == stream.n);
        finish(&q, sequence, symbols);
        assert(frame_order_output_begin(&q, retired, &t));
        assert(!frame_order_input_begin(&q, 0, sequence + 2u));
        append(&q, &stream, &t);
        assert(!frame_order_output_begin(&q, retired, &t));
        blocked++;
        drain(&q, &retired);
        assert(frame_order_output_begin(&q, retired, &t));
        append(&q, &stream, &t);
        drain(&q, &retired);
    }
    assert(blocked == FRAMES / 2);
    assert(stream.n == expected.n && stream.fill == expected.fill &&
           stream.acc_i == expected.acc_i && stream.acc_q == expected.acc_q);
    assert(!memcmp(result, serial, expected.n * sizeof(uint32_t)));
    const uint32_t full = stream.n, fill = stream.fill;
    if (stream.fill) result[stream.n++] = stream.acc_i | stream.acc_q << 16;
    symstream_flush(&expected);
    assert(stream.n == expected.n && !memcmp(result, serial, expected.n * sizeof(uint32_t)));
    printf("{\"code\":\"%s\",\"pilots\":%u,\"frames\":%u,\"symbols\":%u,"
           "\"full_words\":%u,\"fill\":%u,\"blocked\":%u,\"words\":[",
           c->name, pilots, FRAMES, symbols, full, fill, blocked);
    for (uint32_t word = 0; word < stream.n; word++)
        printf("%s%u", word ? "," : "", result[word]);
    puts("]}");
}

int main(void) {
    queue_units();
    run(0);
    run(1);
    return 0;
}
