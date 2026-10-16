// Host build of firmware/src/dvbs2/dvbs2.c. For each code and pilot setting: check the fast BCH and
// LDPC against the bit-serial forms, then print the PLFRAME in shaper-format words for comparison
// with reference/dvbs2/dvbs2.py. Input: xorshift32 words (seed per code) as the unscrambled
// BBFRAME, truncated to Kbch bits.
#include <stdio.h>
#include <string.h>

#include "dvbs2_codes.h"
#include "iqgen.h"

// Exercise every selectable BCH implementation against the bit-serial definition, including
// nonzero unused bits in a final partial word. Kernels 2/3 use C recurrences on this host;
// the Thumb-2 scheduling and register preservation still require the device checks.
static int check_bch_kernels(const dvbs2_code_t *c, uint32_t *input, uint32_t *got, uint32_t *ref) {
    const uint32_t nw = (c->kbch + 31u) / 32;
    int ok = 1;
    for (unsigned pattern = 0; pattern < 6; pattern++) {
        uint32_t st = 0xD1B54A35u ^ c->kbch ^ pattern;
        for (uint32_t i = 0; i < nw; i++) {
            st ^= st << 13, st ^= st >> 17, st ^= st << 5;
            input[i] = pattern == 0 ? 0 : pattern == 1 ? ~0u : pattern == 2 ? 0xAAAAAAAAu :
                       pattern == 3 ? 0x55555555u : pattern == 4 ? st : 0;
        }
        if (pattern == 5) {
            input[0] = 0x80000001u;
            input[nw - 1] ^= 1u << (31u - ((c->kbch - 1u) & 31u));
        }
        dvbs2_bch_serial(input, ref);
        for (int kernel = S2_BCH_BASELINE; kernel <= S2_BCH_T8_ARM; kernel++) {
            ok &= dvbs2_bch_set_kernel((dvbs2_bch_kernel_t)kernel);
            dvbs2_bch(input, got);
            if (memcmp(got, ref, 24)) {
                fprintf(stderr, "BCH mismatch: %s pattern=%u kernel=%d\n", c->name, pattern, kernel);
                ok = 0;
            }
        }
    }
    // Reject out-of-range selectors without silently changing the active kernel.
    ok &= !dvbs2_bch_set_kernel((dvbs2_bch_kernel_t)-1);
    ok &= !dvbs2_bch_set_kernel((dvbs2_bch_kernel_t)4);
    ok &= dvbs2_bch_get_kernel() == S2_BCH_T8_ARM;
    return ok;
}

// Exercise frame boundaries without flushing: normal and short PLFRAMEs both leave partial
// symbol words. Different payloads catch accidental reuse of the prior frame's FEC workspace.
static int check_staged_frames(const dvbs2_code_t *c, unsigned ci, int pilots,
                               dvbs2_bch_kernel_t kernel) {
    enum { FRAMES = 3, STREAM_WORDS = (FRAMES * 33282 + 15) / 16 + 1 };
    static uint32_t bb[64800 / 32 + 2], full[STREAM_WORDS], staged[STREAM_WORDS];
    symstream_t streams[2] = {{.out = full, .mask = ~0u}, {.out = staged, .mask = ~0u}};
    for (int mode = 0; mode < 2; mode++) {
        dvbs2_bch_set_kernel(kernel);
        dvbs2_init(c, pilots);
        for (unsigned frame = 0; frame < FRAMES; frame++) {
            uint32_t st = 0xD1B54A35u ^ ci ^ (frame * 0x9E3779B9u);
            const uint32_t nw = (c->kbch + 31u) / 32;
            memset(bb, 0, sizeof bb);
            prbs_fill(bb, nw, &st);
            if (c->kbch % 32u) bb[nw - 1] &= ~(~0u >> (c->kbch % 32u));
            if (!mode) dvbs2_frame(bb, &streams[mode]);
            else {
                dvbs2_frame_begin(bb);
                dvbs2_frame_ldpc();
                dvbs2_frame_finish(&streams[mode]);
            }
        }
    }
    const symstream_t *a = &streams[0], *b = &streams[1];
    int ok = a->n == b->n && a->fill == b->fill && a->acc_i == b->acc_i &&
             a->acc_q == b->acc_q && b->n * 16u + b->fill == FRAMES * dvbs2_plframe_symbols();
    if (ok) ok = !memcmp(full, staged, a->n * sizeof full[0]);
    symstream_flush(&streams[0]);
    symstream_flush(&streams[1]);
    ok &= streams[0].n == streams[1].n;
    if (ok) ok = !memcmp(full, staged, streams[0].n * sizeof full[0]);
    if (!ok) fprintf(stderr, "Staged stream mismatch: %s pilots=%d kernel=%d\n", c->name, pilots, kernel);
    return ok;
}

// Both parity packers must give identical words, including the generic path's zero guard,
// for the separate-buffer API and for in-place FE construction after an unaligned Kldpc.
// The in-place input mimics frame_begin: information bits, then zero bits after Kldpc.
static int check_ldpc_packs(const dvbs2_code_t *c, unsigned ci) {
    enum { W = 64800 / 32 + 2 };
    static uint32_t info[W], par[2][W], frame[2][W];
    const uint32_t m = c->nldpc - c->kldpc, guard = (m + 63) / 32, kw = c->kldpc / 32;
    uint32_t st = 0x2545F491u ^ ci;
    for (uint32_t k = 0; k < W; k++) info[k] = st = st * 1664525u + 1013904223u;
    int ok = 1;
    for (unsigned pack = 0; pack < 2; pack++) {
        ok &= dvbs2_ldpc_set_pack(pack) && dvbs2_ldpc_get_pack() == pack;
        memset(par[pack], 0xA5, sizeof par[pack]);
        dvbs2_ldpc(info, par[pack]);
        memcpy(frame[pack], info, sizeof info);
        if (c->kldpc & 31u) frame[pack][kw] &= ~(~0u >> (c->kldpc & 31u));
        memset(frame[pack] + kw + !!(c->kldpc & 31u), 0, (W - kw - !!(c->kldpc & 31u)) * 4);
        dvbs2_frame_ldpc_into(frame[pack]);
    }
    if (memcmp(par[0], par[1], guard * 4) || par[1][guard] != 0xA5A5A5A5u) {
        fprintf(stderr, "LDPC pack mismatch (separate buffer): %s\n", c->name);
        ok = 0;
    }
    if (memcmp(frame[0], frame[1], sizeof frame[0])) {
        fprintf(stderr, "LDPC pack mismatch (in-place FE): %s\n", c->name);
        ok = 0;
    }
    ok &= !dvbs2_ldpc_set_pack(2) && dvbs2_ldpc_get_pack() == 1;
    return ok;
}

int main(int argc, char **argv) {
    static uint32_t bb[64800 / 32 + 2], fe[64800 / 32 + 2], p1[64800 / 32 + 2], p2[64800 / 32 + 2];
    static uint32_t out[33282 / 16 + 2], baseline[33282 / 16 + 2];
    int failed = 0;
    const char *only = argc > 1 ? argv[1] : 0;
    for (int ci = 0; ci < N_DVBS2_CODES; ci++) {
        const dvbs2_code_t *c = &DVBS2_CODES[ci];
        if (only && strcmp(only, c->name)) continue;
        for (int pil = 0; pil < 2; pil++) {
            dvbs2_bch_set_kernel(S2_BCH_WORDS_C);
            dvbs2_init(c, pil);
            const int selection_ok = dvbs2_bch_get_kernel() == S2_BCH_WORDS_C;
            uint32_t st = 0x9E3779B9u ^ (uint32_t)ci, nw = (c->kbch + 31) / 32;
            memset(bb, 0, sizeof bb);
            prbs_fill(bb, nw, &st);
            if (c->kbch % 32) bb[nw - 1] &= ~(~0u >> (c->kbch % 32));
            const int bch_ok = selection_ok && check_bch_kernels(c, fe, p1, p2);
            for (uint32_t k = 0; k < 64800 / 32 + 2; k++) fe[k] = st = st * 1664525u + 1013904223u;
            const uint32_t m = c->nldpc - c->kldpc;
            int ldpc_ok = 1;
            dvbs2_ldpc_serial(fe, p2);
            for (unsigned pack = 0; pack < 2; pack++) {
                dvbs2_ldpc_set_pack(pack);
                dvbs2_ldpc(fe, p1);
                ldpc_ok &= !memcmp(p1, p2, m / 32 * 4);
                if (m % 32) ldpc_ok &= ((p1[m / 32] ^ p2[m / 32]) & ~(~0u >> (m % 32))) == 0;
            }
            if (!pil) ldpc_ok &= check_ldpc_packs(c, (unsigned)ci);
            symstream_t s = {0};
            int frame_ok = 1;
            uint32_t baseline_n = 0;
            for (unsigned pack = 0; pack < 2; pack++)
            for (int kernel = S2_BCH_BASELINE; kernel <= S2_BCH_T8_ARM; kernel++) {
                dvbs2_ldpc_set_pack(pack);
                dvbs2_bch_set_kernel((dvbs2_bch_kernel_t)kernel);
                s = (symstream_t){.out = out, .mask = ~0u};
                dvbs2_frame(bb, &s);                           // bb is not modified
                symstream_flush(&s);
                if (!pack && kernel == S2_BCH_BASELINE) {
                    baseline_n = s.n;
                    memcpy(baseline, out, s.n * sizeof out[0]);
                } else {
                    frame_ok &= s.n == baseline_n && !memcmp(baseline, out, s.n * sizeof out[0]);
                }
                frame_ok &= check_staged_frames(c, (unsigned)ci, pil, (dvbs2_bch_kernel_t)kernel);
            }
            dvbs2_ldpc_set_pack(0);
            failed |= !(bch_ok && ldpc_ok && frame_ok);
            printf("{\"code\":\"%s\",\"pilots\":%d,\"seed\":%u,\"bch_ok\":%d,\"ldpc_ok\":%d,"
                   "\"syms\":%u,\"words\":[", c->name, pil, 0x9E3779B9u ^ (uint32_t)ci, bch_ok && frame_ok,
                   ldpc_ok, dvbs2_plframe_symbols());
            for (uint32_t k = 0; k < s.n; k++) printf("%s%u", k ? "," : "", out[k]);
            printf("]}\n");
        }
    }
    return failed ? 1 : 0;
}
