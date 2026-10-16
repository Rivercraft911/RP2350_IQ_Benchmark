// Experimental DVB-S2 8PSK PL mapper. FEC remains in dvbs2.c; shaping in iq8*.c.
// The complete PLFRAME is globally rotated by pi/8, including headers and pilots.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "dvbs2.h"
#include "iq8.h"

typedef struct { uint32_t r0, r1; } dvbs2_8psk_gold_t;

typedef struct {
    const dvbs2_code_t *code;
    dvbs2_8psk_gold_t *gold; // paired Gold words: data first, then pilot bits
    uint32_t data_symbols, body_symbols, frame_symbols, gold_code, pilot_blocks;
    uint8_t modcod, pilots, is_short, column_reverse;
    iq8_input_t header[6]; // 90 symbols: five complete groups and ten symbols
    const uint16_t *codes_lut; // optional dilation/header/pilot tables, shared read-only
} dvbs2_8psk_cfg_t;

// Same ring convention as symstream_t, but each full group contains 16 symbols
// in two signed amplitude planes. mask is group_count-1, or ~0u for linear output.
// n counts complete groups monotonically. Partial symbols survive every frame.
typedef struct {
    iq8_input_t *out;
    uint32_t mask, n;
    uint32_t acc_ai, acc_aq, acc_bi, acc_bq, fill;
} dvbs2_8psk_stream_t;

// Returns zero for unsupported QPSK code-table entries. Arena holds paired Gold
// words plus one guard pair (at most 5536 bytes), with no heap or globals. Gold
// advances in transmission order; storage groups data bits before pilot bits.
uint32_t dvbs2_8psk_arena_words(const dvbs2_code_t *code, bool pilots);
// gold_code range0..262142. Config, code and arena must outlive frame calls and
// remain unchanged during a frame. False leaves config and arena unchanged.
bool dvbs2_8psk_init(dvbs2_8psk_cfg_t *cfg, const dvbs2_code_t *code, bool pilots,
                   uint32_t gold_code, uint32_t *arena, uint32_t arena_words);
// Optional packed-code tables: 512-byte dilation, 48-byte header, up to 336
// bytes of pilots, 16 bytes of metadata and 1024 bytes reserved for the
// position-independent M33 data leaf. Caller retains all 1936 bytes for
// concurrent frames; no static SRAM is added.
enum { DVBS2_8PSK_CODES_LUT_WORDS = 484 };
bool dvbs2_8psk_codes_lut_init(dvbs2_8psk_cfg_t *cfg, uint32_t *arena, uint32_t words);
// C6 reuses C4's tables and appends a private PIC instruction reservation.
// The first 484 words keep their C4 layout, including its unmodified leaf.
enum { DVBS2_8PSK_CODES_C6_WORDS = DVBS2_8PSK_CODES_LUT_WORDS + 768 };
bool dvbs2_8psk_codes_c6_init(dvbs2_8psk_cfg_t *cfg, uint32_t *arena, uint32_t words);

// frame is the existing MSB-first complete FE bit vector after LDPC append.
// It must have a zero/readable guard word after ceil(nldpc/32), as existing FE
// buffers do. No materialized interleaver or intermediate symbol frame is made.
void dvbs2_8psk_frame(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                     dvbs2_8psk_stream_t *stream);
// Explicit end-of-stream operation only; never flush between PLFRAMEs. Pads
// the last group with the valid rotated point pi/8 and clears partial state.
void dvbs2_8psk_flush(dvbs2_8psk_stream_t *stream);

// C3: directly produce the four-level shaper's chronological two-bit codes.
// Each complete word holds sixteen symbols, low bits first. n is monotonic;
// mask is ring_words-1, or ~0u for linear output. Partial words survive frames.
// For an independently staged frame use fill=0 and flush only its final pad.
typedef struct {
    uint32_t *i, *q;
    uint32_t mask, n, acc_i, acc_q, fill;
    // Frame-parallel ownership: defer the first complete word, while advancing
    // n normally. The ordered publisher ORs first_i/q with the prior frame's
    // trailing acc_i/q and writes that boundary word only after both are ready.
    // Each new frame starts with zero accumulators and its chronological fill;
    // its later complete words are disjoint from the adjacent frame's words.
    bool defer_first, first_pending;
    uint32_t first_i, first_q;
} dvbs2_8psk_codes_stream_t;

typedef struct {
    const dvbs2_8psk_cfg_t *cfg;
    const uint32_t *frame;
    dvbs2_8psk_codes_stream_t *stream;
    uint32_t data, pilot;
    bool header_done, fast, c6;
} dvbs2_8psk_codes_state_t;

// begin does no mapping. Each nonzero step maps at most max_words chunks of
// <=32 data symbols, plus the header once and any pilots reached by that slice.
// A zero budget makes no progress. True means the complete frame was appended.
// cfg, frame and stream must remain valid until completion. No static workspace.
void dvbs2_8psk_codes_begin(dvbs2_8psk_codes_state_t *state,
                          const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                          dvbs2_8psk_codes_stream_t *stream);
// False returns without touching state when the optional table is unavailable.
bool dvbs2_8psk_codes_begin_fast(dvbs2_8psk_codes_state_t *state,
                               const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                               dvbs2_8psk_codes_stream_t *stream);
// C6 is selected separately from C4; no change to either old begin function.
bool dvbs2_8psk_codes_begin_c6(dvbs2_8psk_codes_state_t *state,
                             const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                             dvbs2_8psk_codes_stream_t *stream);
bool dvbs2_8psk_codes_step(dvbs2_8psk_codes_state_t *state, uint32_t max_words);
void dvbs2_8psk_codes_frame(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                           dvbs2_8psk_codes_stream_t *stream);
// End-of-stream only. Pad with the same pi/8 point as the signed-plane mapper.
void dvbs2_8psk_codes_flush(dvbs2_8psk_codes_stream_t *stream);
