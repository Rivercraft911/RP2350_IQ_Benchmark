// DVB-S2 QPSK transmitter chain, ETSI EN 302 307-1 V1.4.1: BB scrambling (5.2.2), BCH (5.3.1),
// LDPC (5.3.2), QPSK mapping (5.4.1), PL framing and scrambling (5.5). Portable C.
//
// Bit vectors are uint32 words, MSB first: bit 31 of word 0 is the first bit in transmission
// order. Output symbols use the shaper input format (reference/iqlut.py): one word per 16
// symbols, I bits in [15:0] and Q bits in [31:16], symbol j in bit j, amplitude s = 1 - 2b.
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    const char *name;           // e.g. "normal 2/3"
    uint16_t nldpc, kldpc, kbch, q;
    uint8_t modcod, is_short, t; // BCH degree = t * (is_short ? 14 : 16)
    uint16_t n_groups;          // kldpc / 360
    const uint16_t *grp;        // grp[g] .. grp[g+1]-1 index addr[] for info group g
    const uint16_t *addr;       // parity addresses x from annex B/C, rows concatenated
} dvbs2_code_t;

// One-time setup for a code: BCH table, BB-scrambling and PL-scrambling sequences.
void dvbs2_init(const dvbs2_code_t *c, bool pilots);

// FEC stages (exposed for benchmarking). Buffers are MSB-first word vectors.
void dvbs2_bbscramble(uint32_t *bbframe);                      // kbch bits, in place
void dvbs2_bch(const uint32_t *bbframe, uint32_t *parity);    // kbch -> 16t or 14t parity bits
void dvbs2_bch_serial(const uint32_t *bbframe, uint32_t *parity);   // bit-serial reference
void dvbs2_ldpc(const uint32_t *info, uint32_t *parity);      // kldpc -> nldpc-kldpc bits
void dvbs2_ldpc_serial(const uint32_t *info, uint32_t *parity);     // bit-serial reference

// Full chain for one BBFRAME: scramble, BCH, LDPC, map, PL framing. Appends the PLFRAME symbols
// to a continuous shaper-format word stream (frames are not multiples of 16 symbols).
typedef struct {
    uint32_t *out;              // destination words
    uint32_t n;                 // words written
    uint32_t acc_i, acc_q;      // partial word (fill < 16 bits each)
    uint32_t fill;              // symbols in the partial word
} symstream_t;

// Append nsym (<= 16) symbols of a shaper word. A full 16-symbol put leaves fill unchanged.
static inline void symstream_put(symstream_t *s, uint32_t w, uint32_t nsym) {
    const uint32_t m = nsym == 16 ? 0xFFFFu : (1u << nsym) - 1;
    s->acc_i |= (w & m) << s->fill;
    s->acc_q |= ((w >> 16) & m) << s->fill;
    s->fill += nsym;
    if (s->fill >= 16) {
        s->out[s->n++] = (s->acc_i & 0xFFFFu) | s->acc_q << 16;
        s->acc_i >>= 16, s->acc_q >>= 16, s->fill -= 16;
    }
}

// Cycle checkpoints (device only): LDPC groups, accumulate, transpose; frame BB+BCH, LDPC, map.
extern uint32_t dvbs2_prof[8];

uint32_t dvbs2_plframe_symbols(void);                         // for the configured code
void dvbs2_frame(const uint32_t *bbframe, symstream_t *s);     // bbframe: kbch bits, unscrambled
void symstream_flush(symstream_t *s);
