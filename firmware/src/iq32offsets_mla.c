// A3 opt-in multiply-form native-to-offset conversion (declared in iq32offsets_native.h).
// CMake links this file after every other source so its SCRATCH_Y section comes last
// and no existing scratch leaf moves.
#include "iq32offsets_native.h"

static inline uint32_t offset(uint32_t a, uint32_t b, uint32_t c) {
    return (a + 17u * (b + 17u * c)) << 3;
}

#if defined(IQ_ON_DEVICE) && !defined(IQ4HIST_EXPERIMENT)
#define PREP_PLACEMENT __attribute__((section(".scratch_y.iq32prep"), noinline))
#elif defined(IQ_ON_DEVICE)
// The experiment image has no SCRATCH_Y room beside B9's grant IRQ (376 B over); there the
// conversion runs from flash and records report native_to_offsets_mla_flash. A3's device
// results were measured on the default image, where it stays in SCRATCH_Y.
#define PREP_PLACEMENT __attribute__((noinline))
#else
#define PREP_PLACEMENT __attribute__((noinline))
#endif

// Keys of symbols s0, s1 after s[-2], s[-1], in packed I|Q<<16 lanes:
//   8 s[-2] + 136 s[-1] + 2312 s0 = 8 s[-2] + 136 u,   u = s[-1] + 17 s0
//   8 s[-1] + 136 s0   + 2312 s1 = 8 u + 2312 s1.
// These are the same integer polynomials as offset(); equality holds modulo 2^32
// for any input, so lane overflow (impossible for codes 0..16) would also match.
// The five-word history prefix is the original code; only the loop changes.
void PREP_PLACEMENT iq32offsets_from_native_mla(uint32_t *out, const uint32_t *input,
                                                uint32_t blocks, const uint32_t previous[4]) {
    if (!blocks) return;
    const uint32_t mask = 0x001f001fu;
    const uint32_t pilot = 6u | 6u << 5 | 6u << 16 | 6u << 21;
    uint32_t last = pilot;
    if (previous) {
        uint32_t current = previous[1];
        out[0] = offset((previous[0] >> 5) & mask, current & mask, (current >> 5) & mask);
        last = current;
        for (unsigned word = 2; word < 4; ++word) {
            current = previous[word];
            const uint32_t a = current & mask, b = (current >> 5) & mask;
            out[2*word-3] = offset(last & mask, (last >> 5) & mask, a);
            out[2*word-2] = offset((last >> 5) & mask, a, b);
            last = current;
        }
    } else {
        for (unsigned word = 0; word < 5; ++word) out[word] = 14736u | 14736u << 16;
    }
    out += 5;
    // Opaque constants keep one MUL/MLA per product on the M33; GCC otherwise
    // rebuilds the longer shift-add Horner chains of offset().
    uint32_t k17 = 17u, k136 = 136u, k2312 = 2312u, lanes = mask;
#if defined(IQ_ON_DEVICE)
    __asm__("" : "+r"(k17), "+r"(k136), "+r"(k2312), "+r"(lanes));
#endif
    uint32_t older = last & mask, before = (last >> 5) & mask;
    for (; blocks >= 2; blocks -= 2) {
        const uint32_t first = input[0], second = input[1];
        input += 2;
        const uint32_t a0 = first & lanes, b0 = (first >> 5) & lanes;
        const uint32_t u0 = before + k17 * a0;
        out[0] = 8u * older + k136 * u0;
        out[1] = 8u * u0 + k2312 * b0;
        const uint32_t a1 = second & lanes, b1 = (second >> 5) & lanes;
        const uint32_t u1 = b0 + k17 * a1;
        out[2] = 8u * a0 + k136 * u1;
        out[3] = 8u * u1 + k2312 * b1;
        older = a1; before = b1;
        out += 4;
    }
    if (blocks) {
        const uint32_t current = *input;
        const uint32_t a = current & lanes, b = (current >> 5) & lanes;
        const uint32_t u = before + k17 * a;
        out[0] = 8u * older + k136 * u;
        out[1] = 8u * u + k2312 * b;
    }
}
