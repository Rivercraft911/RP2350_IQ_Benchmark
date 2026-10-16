// DVB-S2 native16/32APSK PL mapping, ETSI EN302307-1 V1.4.1 §§5.3.3–5.5.4.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "dvbs2.h"

typedef struct { uint32_t r0, r1; } dvbs2_apsk_gold_t;
typedef struct {
    const dvbs2_code_t *code;
    dvbs2_apsk_gold_t *gold; // data first, then pilots; generated in transmission order
    uint32_t header[6];     //90 pi/2-BPSK symbols as QPSK sign words
    uint32_t data_symbols, body_symbols, frame_symbols, gold_code, pilot_blocks;
    uint8_t modulation, modcod, pilots, is_short;
} dvbs2_apsk_cfg_t;

enum { DVBS2_APSK_BATCH_WORDS = 64 };
typedef struct {
    uint32_t *out;
    uint32_t mask, n;       // ring word-count-1, or~0u; monotonic complete words
    uint32_t acc_i, acc_q, fill;
    // Before each64-word batch, publish n and reserve room for the next64.
    // Also called at frame end to publish the final complete words. NULL
    // disables backpressure. False stops mapping; an aborted frame cannot resume.
    bool (*service)(void *context, uint32_t produced_words);
    void *context;
} dvbs2_apsk_stream_t;

// Normal and short DVB-S2 APSK rates; short9/10 is unsupported. Caller-owned
// Gold arena: maximum4160bytes. Invalid init leaves config/arena unchanged.
uint32_t dvbs2_apsk_arena_words(const dvbs2_code_t *code, unsigned modulation, bool pilots);
bool dvbs2_apsk_init(dvbs2_apsk_cfg_t *cfg, const dvbs2_code_t *code, unsigned modulation,
                     bool pilots, uint32_t gold_code, uint32_t *arena, uint32_t arena_words);

// Existing complete MSB-first FE vector, with a readable guard word after
// ceil(nldpc/32). Output16APSK:4symbols/u32,3-bit I codes0..11,Q16..27.
// Output32APSK:2symbols/u32,5-bit I codes0..9,Q16..25. Codes are those in
// iq16native8/iq32native8. Outer radius=1; header/pilots have unit radius,
// as explicitly admitted by the standard. No global phase rotation.
// Partial native words survive frames; do not pad/flush between PLFRAMEs.
bool dvbs2_apsk_frame(const dvbs2_apsk_cfg_t *cfg, const uint32_t *frame,
                      dvbs2_apsk_stream_t *stream);

// Opt-in table-driven data path (A3). Same output words, service calls and stop
// state as dvbs2_apsk_frame for every input; normal frames with 16APSK fill0/2
// or 32APSK fill0 use32-symbol leaves, everything else takes the original code.
// The caller's arena holds the lookup tables, the precomputed32APSK header and
// pilot words and, with placement0 on the M33, a copy of the leaf code.
// Placement1 executes the leaves where linked (cached flash; diagnostic).
// Init is idle-only and needs the initialized cfg; the arena must stay intact.
enum { DVBS2_APSK_FAST_TABLE_WORDS = 512, DVBS2_APSK_FAST_CODE_WORDS = 512,
       DVBS2_APSK_FAST_WORDS = DVBS2_APSK_FAST_TABLE_WORDS + DVBS2_APSK_FAST_CODE_WORDS };
typedef struct {
    const uint32_t *lut;      // 16: magnitude[256], sign[256]; 32: points[128]
    const uint32_t *header32; // 32: 45 header words, then 18 per pilot block (fill0)
    uintptr_t run[3];         // leaf entries: 16 fill0, 16 fill2, 32 fill0
    uint32_t modulation, placement, code_bytes;
} dvbs2_apsk_fast_t;
bool dvbs2_apsk_fast_init(dvbs2_apsk_fast_t *fast, const dvbs2_apsk_cfg_t *cfg,
                          uint32_t *arena, uint32_t arena_words, unsigned placement);
bool dvbs2_apsk_frame_fast(const dvbs2_apsk_cfg_t *cfg, const dvbs2_apsk_fast_t *fast,
                           const uint32_t *frame, dvbs2_apsk_stream_t *stream);
