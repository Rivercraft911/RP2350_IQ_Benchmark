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
    S2_BCH_T8_ARM = 3,          // degree-128 four-word Thumb-2, otherwise STREAM_ARM
} dvbs2_bch_kernel_t;
// Experimental selection, retained across dvbs2_init. Default is BASELINE. On non-Arm hosts,
// STREAM_ARM uses WORDS_C; T8_ARM uses a portable four-word recurrence for degree128.
// Native tests validate the recurrences, not Arm scheduling. The full-frame T8_ARM leaf
// remains in XIP: core 1 may call it while core 0 owns scratch Y.
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
// Read-only word sequence for fused source writes, valid until the next dvbs2_init.
// The final partial word's unused low bits are zero; callers mask their BB padding.
const uint32_t *dvbs2_bb_scrambler(void);
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
// Optional bounded frontend for degree128/160 with any nonbaseline BCH selector.
// Successful begin copies the complete BBFRAME, then steps consume at most max_words
// complete input words. The final step also handles <=3 tail bytes and appends BCH.
// Unsupported begin returns false WITHOUT modifying frame or state, allowing full-begin
// fallback. Keep configuration fixed until done; each independent frontend owns its state.
typedef struct {
    uint32_t *frame;
    uint32_t cursor;
    uint32_t parity[7];
    dvbs2_bch_kernel_t kernel;
    bool done, profile;
    uint8_t options;
    void (*leaf)(const uint32_t *, uint32_t *, const void *, uint32_t);
    const void *table; // NULL uses the original byte tables; private overrides are idle-only
} dvbs2_frontend_t;
bool dvbs2_frontend_begin(const uint32_t *bbframe, uint32_t *frame, dvbs2_frontend_t *state);
// Concurrent alternative: no shared profiling writes, accepts degree192 as well. The
// initialized code/tables and selectors must stay fixed; BB/frame/state must be disjoint
// between workers. Like the original begin, the copy/scramble is one complete bounded pass.
bool dvbs2_frontend_begin_reentrant(const uint32_t *bbframe, uint32_t *frame,
                                  dvbs2_frontend_t *state);
enum {
    S2_FE_FAST_BCH = 4u,         // scratch-Y streamed degree128/160/192 leaf on Arm
    S2_FE_LDM_BCH = 64u,         // C7 multiword-load leaf (XIP unless overridden by a copy)
};
typedef void (*dvbs2_bch_leaf_t)(const uint32_t *, uint32_t *, const void *, uint32_t);
typedef void (*dvbs2_transpose_fn)(uint32_t *);
// Idle-only C7 copies: variant0 = existing scratch-Y leaf, 1 = LDM, 2 = 11/11/10
// table. Current code must be normal t8/t10/t12. No prepared global pointer changes.
// Arm requires word-aligned code and enough bytes; native returns C and uses no code arena.
uint32_t dvbs2_bch_code_bytes(unsigned variant);
dvbs2_bch_leaf_t dvbs2_bch_copy(void *code, uint32_t bytes, unsigned variant);
uint32_t dvbs2_bch_table3_bytes(void);
bool dvbs2_bch_table3_init(void *table, uint32_t bytes);
uint32_t dvbs2_transpose_code_bytes(void);
dvbs2_transpose_fn dvbs2_transpose_copy(void *code, uint32_t bytes);
// Per-worker C4 options, captured at begin. Unknown options fail without writes.
// The original begin APIs keep their behavior.
bool dvbs2_frontend_begin_reentrant_options(const uint32_t *bbframe, uint32_t *frame,
                                          dvbs2_frontend_t *state, unsigned options);
// State-only admission for a caller that already scrambled/masked its BB input and
// zeroed every remaining frame word. No input/frame write occurs here.
bool dvbs2_frontend_begin_prepared(uint32_t *frame, dvbs2_frontend_t *state, unsigned options);
// Call only after successful begin. max_words0 makes no progress; completed calls are
// idempotent and return true. On Arm selector2 continues the streamed assembly recurrence.
bool dvbs2_frontend_step(dvbs2_frontend_t *state, uint32_t max_words);
// Concurrent bounded LDPC for q<=64, using the pack1 streaming order. Each worker owns
// frame, state, and q*12 uint32 workspace words (2880/2160/1440 bytes for normal2/3,
// 3/4, 5/6). There is no shared P or profiling write. Configuration must remain fixed.
// A step unit clears one row, handles one 360-bit group, accumulates one row, computes
// the 12-word prefix, applies it to one row, or packs one 32-column block. Steps are
// bounded by max_units; zero makes no progress, completed steps are idempotent.
// Begin validates arguments without changing them on failure. Workspace is cleared in
// the first steps; frame contains BB+BCH on entry and a complete FECFRAME on completion.
typedef struct {
    uint32_t *frame;
    uint32_t (*rows)[12];
    uint32_t cursor, stage, prefix[12], acc, fill, written;
    void (*group)(uint32_t *, const uint32_t *, uint32_t, const uint16_t *,
                  const uint16_t *, uint32_t);
    unsigned mode;
    bool done;
    dvbs2_transpose_fn transpose; // captured default; worker may override with its private copy
} dvbs2_ldpc_state_t;
uint32_t dvbs2_ldpc_workspace_words(void); // zero if q>64
bool dvbs2_ldpc_begin_reentrant(uint32_t *frame, uint32_t *workspace,
                               uint32_t workspace_words, dvbs2_ldpc_state_t *state);
// Mode1 extracts directly into one duplicated group and specializes normal2/3 and
// 5/6 parity packing. Arm uses a position-independent group leaf copied into the
// caller's word-aligned arena by prepare; keep it alive/fixed until both workers
// finish. Native uses the equivalent C leaf and needs zero arena bytes. Mode0 and
// the original begin retain the C3 path. Unsupported modes do not modify state.
uint32_t dvbs2_ldpc_code_bytes(void);
bool dvbs2_ldpc_prepare(void *code, uint32_t bytes);
// Copy the mode1 group leaf into another arena and return its entry without changing the
// prepared default: lets each core run its own copy from its own SRAM banks (NULL on error).
void *dvbs2_ldpc_copy(void *code, uint32_t bytes);
bool dvbs2_ldpc_begin_reentrant_mode(uint32_t *frame, uint32_t *workspace,
                                    uint32_t workspace_words, dvbs2_ldpc_state_t *state,
                                    unsigned mode);
bool dvbs2_ldpc_step(dvbs2_ldpc_state_t *state, uint32_t max_units);
// Instruction placement for frontend steps with selectors STREAM_ARM/T8_ARM: 0 = striped SRAM
// (default), 1 = an identical copy in scratch Y, for a core-0 caller. Select while idle.
// T8_ARM degree128 specializes only bank1 to four words; bank0 retains the five-word leaf.
// Portable builds accept the setting; the C recurrence is unchanged.
bool dvbs2_frontend_set_bank(unsigned bank);
unsigned dvbs2_frontend_get_bank(void);
// Cooperative full-frame stages, in order. Shared encoder buffers make these non-reentrant:
// do not init/encode another frame between begin and finish. Allows shaping between stages.
void dvbs2_frame_begin(const uint32_t *bbframe);              // BB scramble + BCH
void dvbs2_frame_ldpc(void);                                  // LDPC + parity append
void dvbs2_frame_finish(symstream_t *s);                      // QPSK mapping + PL framing
void symstream_flush(symstream_t *s);
