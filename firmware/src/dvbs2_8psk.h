// Experimental DVB-S2 8PSK PL mapper. FEC remains in dvbs2.c; shaping in iq8*.c.
// The complete PLFRAME is globally rotated by pi/8, including headers and pilots.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "dvbs2.h"
#include "iq8.h"

typedef struct {
    const dvbs2_code_t *code;
    uint32_t *gold0, *gold1;
    uint32_t data_symbols, body_symbols, frame_symbols, gold_code, pilot_blocks;
    uint8_t modcod, pilots, is_short, column_reverse;
    iq8_input_t header[6]; // 90 symbols: five complete groups and ten symbols
} dvbs2_8psk_cfg_t;

// Same ring convention as symstream_t, but each full group contains 16 symbols
// in two signed amplitude planes. mask is group_count-1, or ~0u for linear output.
// n counts complete groups monotonically. Partial symbols survive every frame.
typedef struct {
    iq8_input_t *out;
    uint32_t mask, n;
    uint32_t acc_ai, acc_aq, acc_bi, acc_bq, fill;
} dvbs2_8psk_stream_t;

// Returns zero for unsupported QPSK code-table entries. Arena holds two Gold
// bitplanes and one guard word each (at most 5536 bytes), with no heap or globals.
uint32_t dvbs2_8psk_arena_words(const dvbs2_code_t *code, bool pilots);
// gold_code range0..262142. Config, code and arena must outlive frame calls and
// remain unchanged during a frame. False leaves config and arena unchanged.
bool dvbs2_8psk_init(dvbs2_8psk_cfg_t *cfg, const dvbs2_code_t *code, bool pilots,
                   uint32_t gold_code, uint32_t *arena, uint32_t arena_words);

// frame is the existing MSB-first complete FE bit vector after LDPC append.
// It must have a zero/readable guard word after ceil(nldpc/32), as existing FE
// buffers do. No materialized interleaver or intermediate symbol frame is made.
void dvbs2_8psk_frame(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                     dvbs2_8psk_stream_t *stream);
// Explicit end-of-stream operation only; never flush between PLFRAMEs. Pads
// the last group with the valid rotated point pi/8 and clears partial state.
void dvbs2_8psk_flush(dvbs2_8psk_stream_t *stream);
