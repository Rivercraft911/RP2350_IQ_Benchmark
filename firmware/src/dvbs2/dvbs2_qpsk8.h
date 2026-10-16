// QPSK PL symbols on the existing globally pi/8-rotated signed-plane shaper.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "dvbs2_8psk.h"

enum { DVBS2_QPSK8_SCRATCH_WORDS = 4096 };

// Append full packed QPSK words, then exactly tail_symbols (0..16) low-lane
// symbols of tail_word. A=w, B=(-Q,I) in iq8.h's weight convention. No flush.
// Input and output storage must be disjoint; caller reserves complete output
// groups, including any crossing caused by the existing partial group.
// NULL stream/output, NULL nonempty input, tail_symbols>16 or fill>=16
// return false without changing stream or output.
bool dvbs2_qpsk8_append(dvbs2_8psk_stream_t *stream, const uint32_t *words,
                        uint32_t nwords, uint32_t tail_word, unsigned tail_symbols);

// Call after the existing QPSK dvbs2_init and FE stages. Uses the actual QPSK
// PL mapper, preserving its MODCOD/header/pilots, then appends into the shared
// 8PSK stream with one common pi/8 rotation. Partial groups survive mode changes.
// Caller owns >=4096 scratch words, disjoint from FE, Gold and output storage.
// FE guard/lifetime requirements are the same as dvbs2.h. Encoder configuration
// must remain unchanged for this call. No extra BSS/heap.
bool dvbs2_qpsk8_frame_from(const uint32_t *frame, dvbs2_8psk_stream_t *stream,
                            uint32_t *scratch, uint32_t scratch_words);
