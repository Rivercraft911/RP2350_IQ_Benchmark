"""Check the opt-in word-output TS adapter (pv_bbframe_words) natively; no USB.

1. Against the existing firmware path, compiled from the same pvproto.c: pv_bbframe bytes,
   bytes Kbch/8.. of the last word zeroed, every word swapped (bench_dual build_spi_frame).
   Every normal and short QPSK Kbch, all four source alignments, missing packets (nulls),
   frame boundaries at every packet offset, identical pv_stats and identical in-place
   sync-byte rewrites in the packet buffers.
2. Against the independent Python TS reference that validates device captures
   (host/dual_spi.py bbframe, reference/dvbs2): the on-chip pattern stream for 2/3, 5/6,
   8/9 and 9/10, 24 consecutive frames each.
Host execution only: this is C semantics on the build machine, not Arm timing.
"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import zlib

import numpy as np

import dual_spi as spi

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "pvproto.h"

// Packet store: each run regenerates it, because both adapters rewrite sync bytes in place.
static uint8_t store[4 + 64 * PV_MSG_BYTES];
static uint8_t *base;
static uint32_t calls, handed, nulls_every, nulls_run;
static uint32_t messages, pattern_period;

static void fill(unsigned align, uint32_t n_messages, uint32_t period) {
    base = store + align;
    messages = n_messages, pattern_period = period;
    for (uint32_t m = 0; m < n_messages; m++) {
        uint8_t msg[PV_MSG_BYTES];
        pv_pattern_message(msg, m % period, period);
        memcpy(base + m * PV_MSG_BYTES, msg, PV_MSG_BYTES);
    }
    calls = handed = 0;
}

// Packets in message order; optionally NULL (missing) on a deterministic pattern.
static uint8_t *next(void) {
    const uint32_t k = calls++;
    if (nulls_every && (k % nulls_every) < nulls_run) return NULL;
    const uint32_t g = handed++ % (messages * 7);
    return base + (g / 7) * PV_MSG_BYTES + 12 + PV_TS * (g % 7);
}

static uint32_t old_words[2048], new_words[2048];
static uint64_t leaf_words, framed_bytes;

// The device leaf loads whole words: hold every call to that contract and count its share.
static void checked_words(const uint32_t *src, uint32_t *dst, uint32_t n, pv_words_state_t *state) {
    assert(n >= 1 && !((uintptr_t)src & 3u) && !((uintptr_t)dst & 3u) && state->phase < 4);
    assert(!(state->acc << (8 * state->phase)) && state->crc < 256);
    leaf_words += n;
    pv_words_c(src, dst, n, state);
}

static int compare(uint32_t kbch, unsigned align, uint32_t every, uint32_t run, unsigned frames) {
    const uint32_t nb = kbch / 8, nw = (nb + 3) / 4;
    static uint8_t after_old[sizeof store];
    static pv_words_t ctx;
    pv_stats_t old_stats;
    uint32_t old_crc = 0, new_crc = 0;
    nulls_every = every, nulls_run = run;

    pv_init(16);
    fill(align, 64, 16);
    for (unsigned f = 0; f < frames; f++) {
        uint8_t *b = (uint8_t *)old_words;
        memset(old_words, 0xa5, sizeof old_words);
        pv_bbframe(b, kbch, 0xF2, next);
        for (uint32_t i = nb; i < 4 * nw; i++) b[i] = 0;
        for (uint32_t i = 0; i < nw; i++) old_words[i] = __builtin_bswap32(old_words[i]);
        old_crc = pv_crc32(old_crc, old_words, 4 * nw);
        if (f == frames - 1) memcpy(new_words, old_words, 4 * nw);   // keep the last frame
    }
    old_stats = pv_stats;
    memcpy(after_old, store, sizeof store);
    uint32_t last_old[2048];
    memcpy(last_old, new_words, 4 * nw);

    pv_init(16);
    fill(align, 64, 16);
    assert(pv_words_prepare(&ctx, NULL, 0) && ctx.words == pv_words_c);
    ctx.words = checked_words;
    static const uint32_t chunks[] = {0, 1, 2, 3, 7, 1, 0, 4};
    for (unsigned f = 0; f < frames; f++) {
        framed_bytes += nb;
        memset(new_words, 0x5a, sizeof new_words);
        if (f % 3 == 0) pv_bbframe_words(new_words, kbch, 0xF2, next, &ctx);
        else {
            // Bounded steps, as core 0 runs them between supervision polls (pvkernel 3).
            pv_bbframe_words_begin(&ctx, new_words, kbch, 0xF2);
            for (unsigned i = f;; i++) {
                const uint32_t calls_before = calls, chunk = chunks[i % 8];
                const bool done = pv_bbframe_words_step(&ctx, next, chunk);
                if (calls - calls_before > chunk || (!chunk && done)) {
                    printf("{\"error\":\"step bound\",\"kbch\":%u}\n", kbch);
                    return 1;
                }
                if (done) break;
            }
            const uint32_t calls_before = calls, frames_before = pv_stats.bbframes;
            if (!pv_bbframe_words_step(&ctx, next, 5) || calls != calls_before || pv_stats.bbframes != frames_before) {
                printf("{\"error\":\"completed step not idempotent\",\"kbch\":%u}\n", kbch);
                return 1;
            }
        }
        for (uint32_t i = nw; i < 2048; i++)
            if (new_words[i] != 0x5a5a5a5au) { printf("{\"error\":\"write beyond %u\"}\n", nw); return 1; }
        new_crc = pv_crc32(new_crc, new_words, 4 * nw);
    }
    if (old_crc != new_crc || memcmp(last_old, new_words, 4 * nw)) {
        printf("{\"error\":\"words differ\",\"kbch\":%u,\"align\":%u,\"every\":%u}\n", kbch, align, every);
        return 1;
    }
    if (memcmp(after_old, store, sizeof store)) {
        printf("{\"error\":\"packet buffers differ\",\"kbch\":%u,\"align\":%u}\n", kbch, align);
        return 1;
    }
    if (old_stats.ts_packets != pv_stats.ts_packets || old_stats.null_packets != pv_stats.null_packets ||
        old_stats.bbframes != pv_stats.bbframes) {
        printf("{\"error\":\"stats differ\",\"kbch\":%u}\n", kbch);
        return 1;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc > 1) {
        // Reference mode: by default the on-chip emulator pattern (16 messages, seq_mod 16);
        // "1 65536" is pvbench's source, the 7 packets of pattern message 0. No nulls.
        const uint32_t kbch = (uint32_t)atoi(argv[1]), frames = (uint32_t)atoi(argv[2]);
        const uint32_t n_messages = argc > 3 ? (uint32_t)atoi(argv[3]) : 16;
        const uint32_t period = argc > 4 ? (uint32_t)atoi(argv[4]) : 16;
        const uint32_t nw = (kbch / 8 + 3) / 4;
        static pv_words_t ctx;
        nulls_every = 0;
        pv_init(period);
        fill(0, n_messages, period);
        assert(pv_words_prepare(&ctx, NULL, 0));
        printf("[");
        for (uint32_t f = 0; f < frames; f++) {
            pv_bbframe_words(new_words, kbch, 0xF2, next, &ctx);
            printf("%s[", f ? "," : "");
            for (uint32_t i = 0; i < nw; i++) printf("%s%u", i ? "," : "", new_words[i]);
            printf("]");
        }
        printf("]\n");
        return pv_stats.null_packets != 0;
    }
    static const uint32_t kbch[] = {
        16008, 21408, 25728, 32208, 38688, 43040, 48408, 51648, 53840, 57472, 58192,   // normal
        3072, 5232, 6312, 7032, 9552, 10632, 11712, 12432, 13152, 14232,              // short
        // Data fields of exactly one, two and half a packet: frames end on packet boundaries
        // (real 2/3 does so every 94th frame) and start with SYNCD 0.
        80 + 8 * 188, 80 + 16 * 188, 80 + 8 * 94};
    static const uint32_t nulls[][2] = {{0, 0}, {37, 1}, {101, 9}, {5, 4}};
    unsigned configs = 0, frames = 0;
    for (unsigned c = 0; c < sizeof kbch / sizeof kbch[0]; c++)
        for (unsigned align = 0; align < 4; align++)
            for (unsigned z = 0; z < 4; z++) {
                // Enough frames for the frame start to visit many packet offsets.
                const unsigned count = kbch[c] < 20000 ? 47 : 23;
                if (compare(kbch[c], align, nulls[z][0], nulls[z][1], count)) return 1;
                configs++, frames += count;
            }
    printf("{\"configs\":%u,\"frames\":%u,\"leaf_share\":%.4f}\n", configs, frames,
           (double)(4 * leaf_words) / (double)framed_bytes);
    return 0;
}
'''


def build(directory):
    source, exe = Path(directory)/'pv_words_test.c', Path(directory)/'pv_words_test'
    source.write_text(HARNESS)
    src = ROOT/'firmware/src'
    subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all', '-I'+str(src), str(source), str(src/'pvproto.c'),
                    '-o', str(exe)], check=True)
    return exe


def main():
    with tempfile.TemporaryDirectory(prefix='pv-words-') as directory:
        exe = build(directory)
        result = json.loads(subprocess.check_output([str(exe)], text=True))
        print(f"pv_bbframe_words == pv_bbframe + zero tail + swap: {result['configs']} configurations, "
              f"{result['frames']} frames (21 QPSK Kbch + 3 packet-boundary lengths, 4 source alignments, 4 null patterns; 2 of 3 frames built in bounded steps of 0-7 packet chunks), "
              f"same stats and in-place sync bytes; {100*result['leaf_share']:.1f}% of bytes through "
              "the aligned word leaf; ASan/UBSan")
        assert result['leaf_share'] > 0.9, result
        for rate in ('2/3', '5/6', '8/9', '9/10'):
            kbch = spi.ref.code(rate, False).kbch
            frames = 100 if rate == "2/3" else 24   # 2/3 frame 93 ends on a packet boundary
            got = json.loads(subprocess.check_output([str(exe), str(kbch), str(frames)], text=True))
            for k, words in enumerate(got):
                bits = spi.bbframe(k, gcs=False, rate=rate)
                padded = np.concatenate([bits, np.zeros((-len(bits)) % 32, np.uint8)])
                want = np.packbits(padded).view('>u4').astype(np.uint32)
                assert np.array_equal(np.asarray(words, dtype=np.uint32), want), (rate, k)
            print(f"pv_bbframe_words == independent TS reference, normal {rate}: {frames} frames")
        # Expected device `pvbench reps 1|2` words_crc: CRC-32 of the first 16 frames' words.
        expected = []
        for rate in ('2/3', '9/10'):
            kbch = spi.ref.code(rate, False).kbch
            stream = np.frombuffer(b''.join(spi.pv.pattern_packet(k % 7) for k in range(16 * 40)), np.uint8)
            crc = 0
            for bits in spi.ref.ts_bbframes(stream, rate, ro=0.20)[:16]:
                padded = np.concatenate([bits, np.zeros((-len(bits)) % 32, np.uint8)])
                crc = zlib.crc32(np.packbits(padded).view('>u4').astype('<u4').tobytes(), crc)
            got = json.loads(subprocess.check_output([str(exe), str(kbch), '16', '1', '65536'], text=True))
            native = 0
            for words in got:
                native = zlib.crc32(np.asarray(words, dtype='<u4').tobytes(), native)
            assert native == crc, (rate, native, crc)
            expected.append(crc)
        print(f"pvbench words_crc expected for normal 2/3 and 9/10: {expected}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
