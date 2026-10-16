// Two frame-parallel producers, one ordered symbol publisher. No payload storage.
// Native-tested design model only; no device benchmark includes this header yet. It does
// not make the shared DVB-S2 LDPC scratch reentrant. Keep configuration fixed and serialize
// LDPC, or give each core private scratch. Staged padding is never transmitted: the ordered
// publisher appends exactly `symbols`, carrying its partial word across frame boundaries.
#pragma once
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

enum frame_order_stage {
    FRAME_ORDER_FREE, FRAME_ORDER_INPUT, FRAME_ORDER_ENCODING,
    FRAME_ORDER_READY, FRAME_ORDER_COPYING
};

typedef struct {
    _Atomic uint32_t stage;
    uint32_t sequence, symbols;
} frame_order_slot_t;

typedef struct {
    frame_order_slot_t slot[2];
    _Atomic uint32_t next_input, published_words;
    uint32_t next_output, words, fill, ring_words, guard_words;
} frame_order_t;

typedef struct {
    uint32_t sequence, symbols, first_word, end_word, fill;
} frame_order_ticket_t;

// Initialize while stopped. Counters may wrap; all live word distances must stay < 2^31.
// first_word is also the initial retired cursor. No partial output word exists at init.
static inline bool frame_order_init(frame_order_t *q, uint32_t first_sequence,
                                    uint32_t first_word, uint32_t ring_words, uint32_t guard_words) {
    if (!ring_words || (ring_words & (ring_words - 1u)) || ring_words >= 0x80000000u ||
        guard_words >= ring_words) return false;
    for (unsigned core = 0; core < 2; core++) {
        q->slot[core].sequence = first_sequence + ((first_sequence ^ core) & 1u);
        q->slot[core].symbols = 0;
        atomic_init(&q->slot[core].stage, FRAME_ORDER_FREE);
    }
    atomic_init(&q->next_input, first_sequence);
    atomic_init(&q->published_words, first_word);
    q->next_output = first_sequence;
    q->words = first_word;
    q->fill = 0;
    q->ring_words = ring_words;
    q->guard_words = guard_words;
    return true;
}

// Exactly core (sequence & 1) calls input_begin/input_done/ready for this generation.
// The input token serializes stateful TS/GCS packet consumption across the two cores.
// Acquiring FREE prevents reuse while the publisher still reads this slot's payload.
static inline bool frame_order_input_begin(frame_order_t *q, unsigned core, uint32_t sequence) {
    if (core > 1 || (sequence & 1u) != core ||
        atomic_load_explicit(&q->next_input, memory_order_acquire) != sequence) return false;
    frame_order_slot_t *slot = &q->slot[core];
    if (atomic_load_explicit(&slot->stage, memory_order_acquire) != FRAME_ORDER_FREE ||
        slot->sequence != sequence) return false;
    atomic_store_explicit(&slot->stage, FRAME_ORDER_INPUT, memory_order_relaxed);
    return true;
}

static inline bool frame_order_input_done(frame_order_t *q, unsigned core, uint32_t sequence) {
    if (core > 1 || (sequence & 1u) != core) return false;
    frame_order_slot_t *slot = &q->slot[core];
    if (atomic_load_explicit(&slot->stage, memory_order_relaxed) != FRAME_ORDER_INPUT ||
        slot->sequence != sequence) return false;
    atomic_store_explicit(&slot->stage, FRAME_ORDER_ENCODING, memory_order_relaxed);
    atomic_store_explicit(&q->next_input, sequence + 1u, memory_order_release);
    return true;
}

// Staging uses ceil(symbols/16) words; its last word is zero-padded locally only.
// Publish the exact symbol count, so padding never enters the continuous output stream.
static inline bool frame_order_ready(frame_order_t *q, unsigned core, uint32_t sequence,
                                     uint32_t symbols) {
    if (core > 1 || (sequence & 1u) != core || !symbols || symbols >= 0x80000000u) return false;
    frame_order_slot_t *slot = &q->slot[core];
    if (atomic_load_explicit(&slot->stage, memory_order_relaxed) != FRAME_ORDER_ENCODING ||
        slot->sequence != sequence) return false;
    slot->symbols = symbols;
    atomic_store_explicit(&slot->stage, FRAME_ORDER_READY, memory_order_release);
    return true;
}

// One designated publisher calls output_begin/output_done. retired_words is the slowest
// safe input-read cursor of both axes, after accounting for every DMA/FIFO in-flight read.
// guard_words remain untouched behind that cursor. A failed call changes nothing.
static inline bool frame_order_output_begin(frame_order_t *q, uint32_t retired_words,
                                            frame_order_ticket_t *ticket) {
    frame_order_slot_t *slot = &q->slot[q->next_output & 1u];
    if (atomic_load_explicit(&slot->stage, memory_order_acquire) != FRAME_ORDER_READY ||
        slot->sequence != q->next_output) return false;
    const uint32_t symbols = slot->symbols, writes = (q->fill + symbols) / 16u;
    const uint32_t used = q->words - retired_words, usable = q->ring_words - q->guard_words;
    if (used > usable || writes > usable - used) return false;
    *ticket = (frame_order_ticket_t){q->next_output, symbols, q->words,
                                   q->words + writes, (q->fill + symbols) % 16u};
    atomic_store_explicit(&slot->stage, FRAME_ORDER_COPYING, memory_order_relaxed);
    return true;
}

// Call only after appending the ticket's full words and exact tail into one persistent
// symstream_t. Only completed output words are released to the hardware feeder. The
// partial word stays publisher-owned; the producer may now reuse the entire staging slot.
static inline bool frame_order_output_done(frame_order_t *q, uint32_t sequence) {
    frame_order_slot_t *slot = &q->slot[q->next_output & 1u];
    if (sequence != q->next_output ||
        atomic_load_explicit(&slot->stage, memory_order_relaxed) != FRAME_ORDER_COPYING ||
        slot->sequence != sequence) return false;
    q->words += (q->fill + slot->symbols) / 16u;
    q->fill = (q->fill + slot->symbols) % 16u;
    q->next_output++;
    atomic_store_explicit(&q->published_words, q->words, memory_order_release);
    slot->sequence = sequence + 2u;
    atomic_store_explicit(&slot->stage, FRAME_ORDER_FREE, memory_order_release);
    return true;
}

static inline uint32_t frame_order_published(const frame_order_t *q) {
    return atomic_load_explicit(&q->published_words, memory_order_acquire);
}
