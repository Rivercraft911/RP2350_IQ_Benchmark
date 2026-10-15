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
typedef enum {
    S2_BCH_BASELINE = 0,        // original six-word slicing-by-four C
    S2_BCH_WORDS_C = 1,         // five-word specialization for degree 160, otherwise baseline
    S2_BCH_STREAM_ARM = 2,      // degree-160 streamed Thumb-2, otherwise WORDS_C
} dvbs2_bch_kernel_t;
// Experimental selection, retained across dvbs2_init. Default is BASELINE. On non-Arm hosts,
// STREAM_ARM uses WORDS_C; the native tests validate the same recurrence, not Arm scheduling.
bool dvbs2_bch_set_kernel(dvbs2_bch_kernel_t kernel);
dvbs2_bch_kernel_t dvbs2_bch_get_kernel(void);
// Opt-in mapper experiment: 0 = baseline, 1 = packed-carry contiguous chunks.
// Select while the encoder is idle; retained across dvbs2_init. Canonical FEC bits are unchanged.
bool dvbs2_map_set_kernel(unsigned kernel);
unsigned dvbs2_map_get_kernel(void);
// Opt-in LDPC parity packer: 0 = baseline transpose + OR (+ copy for unaligned Kldpc),
// 1 = column-major streaming pack written in place, used where q <= 64 (except the
// unchanged q = 60 packer). Select while the encoder is idle; parity bits are identical.
bool dvbs2_ldpc_set_pack(unsigned pack);
unsigned dvbs2_ldpc_get_pack(void);
void dvbs2_bbscramble(uint32_t *bbframe);                      // kbch bits, in place
void dvbs2_bch(const uint32_t *bbframe, uint32_t *parity);    // kbch -> 16t or 14t parity bits
void dvbs2_bch_serial(const uint32_t *bbframe, uint32_t *parity);   // bit-serial reference
void dvbs2_ldpc(const uint32_t *info, uint32_t *parity);      // kldpc -> nldpc-kldpc bits
void dvbs2_ldpc_serial(const uint32_t *info, uint32_t *parity);     // bit-serial reference

// Full chain for one BBFRAME: scramble, BCH, LDPC, map, PL framing. Appends the PLFRAME symbols
// to a continuous shaper-format word stream (frames are not multiples of 16 symbols).
typedef struct {
    uint32_t *out;              // destination words
    uint32_t mask;              // index mask: ring size - 1, or ~0 for a linear buffer
    uint32_t n;                 // words written (monotonic)
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
        s->out[s->n++ & s->mask] = (s->acc_i & 0xFFFFu) | s->acc_q << 16;
        s->acc_i >>= 16, s->acc_q >>= 16, s->fill -= 16;
    }
}

// Cycle checkpoints (device only): LDPC groups, accumulate, transpose; frame BB+BCH, LDPC, map.
extern uint32_t dvbs2_prof[8];

// Full 16-symbol put: fill is unchanged, so no mask or branch.
static inline void symstream_put16(symstream_t *s, uint32_t w) {
    const uint32_t ai = s->acc_i | (w & 0xFFFFu) << s->fill, aq = s->acc_q | (w >> 16) << s->fill;
    s->out[s->n++ & s->mask] = (ai & 0xFFFFu) | aq << 16;
    s->acc_i = ai >> 16, s->acc_q = aq >> 16;
}

uint32_t dvbs2_plframe_symbols(void);                         // for the configured code
void dvbs2_frame(const uint32_t *bbframe, symstream_t *s);     // bbframe: kbch bits, unscrambled
// Explicit per-frame storage permits BB+BCH for the next frame while one backend performs
// LDPC/mapping for the current frame. Tables/configuration must remain fixed during use;
// LDPC's shared scratch still permits only one LDPC call at a time. Buffers must be disjoint,
// uint32-aligned arrays of DVBS2_FE_WORDS words. The original buffer is borrowed between runs.
enum { DVBS2_FE_WORDS = 64800 / 32 + 2 };
uint32_t *dvbs2_frame_buffer(void);
void dvbs2_frame_begin_into(const uint32_t *bbframe, uint32_t *frame);
void dvbs2_frame_ldpc_into(uint32_t *frame);
void dvbs2_frame_finish_from(const uint32_t *frame, symstream_t *s);
// Optional bounded frontend for degree160 with BCH selectors WORDS_C or STREAM_ARM.
// Successful begin copies the complete BBFRAME, then steps consume at most max_words
// complete input words. The final step also handles <=3 tail bytes and appends BCH.
// Unsupported begin returns false WITHOUT modifying frame or state, allowing full-begin
// fallback. Keep configuration fixed until done; each independent frontend owns its state.
typedef struct {
    uint32_t *frame;
    uint32_t cursor;
    uint32_t parity[7];
    dvbs2_bch_kernel_t kernel;
    bool done;
} dvbs2_frontend_t;
bool dvbs2_frontend_begin(const uint32_t *bbframe, uint32_t *frame, dvbs2_frontend_t *state);
// Call only after successful begin. max_words0 makes no progress; completed calls are
// idempotent and return true. On Arm selector2 continues the streamed assembly recurrence.
bool dvbs2_frontend_step(dvbs2_frontend_t *state, uint32_t max_words);
// Cooperative full-frame stages, in order. Shared encoder buffers make these non-reentrant:
// do not init/encode another frame between begin and finish. Allows shaping between stages.
void dvbs2_frame_begin(const uint32_t *bbframe);              // BB scramble + BCH
void dvbs2_frame_ldpc(void);                                  // LDPC + parity append
void dvbs2_frame_finish(symstream_t *s);                      // QPSK mapping + PL framing
void symstream_flush(symstream_t *s);
