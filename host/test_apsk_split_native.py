"""Split APSK encoder: the actual bench_apsk_fec.c leaves, sequentially interleaved.

The split mode moves BB scrambling and BCH to core 0 (bounded frontend slices) while
core 1 builds BBFRAMEs and runs LDPC + PL mapping over two FE slots. This harness
extracts the firmware's own BB, priming, core-1 step and core-0 service functions,
stubs device I/O, and runs them under seeded random interleavings (single thread).
The continuous native APSK stream, BB CRCs and the first FE CRCs must equal the
original serial encode_frame path bit for bit, for 16APSK and 32APSK and with both
LDPC parity packers. This checks ownership and ordering, not multicore timing,
memory ordering on Arm, or the device's cycle budget.
"""
from __future__ import annotations

import ctypes as ct
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware" / "src"
FRAMES = 9


def function(source: str, name: str) -> str:
    """Copy the named firmware function, including its original argument/body text."""
    start = source.rfind("\n", 0, source.index(name + "(")) + 1
    brace = source.index("{", source.index(name + "("))
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def harness() -> str:
    source = (SRC / "bench_apsk_fec.c").read_text()
    constants = source[source.index("enum {\n    INPUT_MASK"):]
    constants = constants[:constants.index("};") + 2]
    live_type = source[source.index("typedef struct {\n    union"):source.index("} live_t;") + len("} live_t;")]
    macros = "\n".join(line for name in ("pvspi.h", "tx.h")
                       for line in (SRC / name).read_text().splitlines()
                       if line.startswith(("#define PV_SLOTS ", "#define PV_EMU_MSGS ",
                                           "#define PV_EMU_WORDS ", "#define BB_WORDS ")))
    head = r'''
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "config.h"
#include "dvbs2_codes.h"
#include "dvbs2_apsk.h"
#include "iq16native8.h"
#include "iq32native8.h"
#include "pvproto.h"
#define __dmb() ((void)0)
#define tight_loop_contents() ((void)0)
'''
    setup = r'''
static uint32_t bb_buf[BB_WORDS];
static uint32_t clock_now;
static uint32_t cycles(void) {return clock_now += 7;}
static uint32_t packet_index;
static struct {
    volatile uint32_t errors, short_msgs, long_msgs, overflows, drain_timeouts, late_closes;
} pvspi;
static uint8_t *pvspi_next_packet(void) {
    static uint8_t packet[188];
    const uint32_t k = packet_index++;
    packet[0] = 0x47; packet[1] = k >> 8; packet[2] = k; packet[3] = 0x10 | (k & 15);
    for (unsigned j = 0; j < 184; j++) packet[j + 4] = (uint8_t)(k * 31u + j * 7u);
    return packet;
}
'''
    leaves = "\n\n".join(function(source, name) for name in (
        "transport_failed", "build_bbframe", "map_frame", "charge", "encode_frame",
        "split_encode_step", "split_prime", "bch_service"))
    tail = r'''
static uint32_t gold[2048], slot1[DVBS2_FE_WORDS + 8], fast_arena[DVBS2_APSK_FAST_WORDS];
enum { OUT_MAX = 12 * 16686 / 2 + 64, FRAMES = 9 };
static uint32_t serial_out[OUT_MAX], split_out[OUT_MAX];
uint32_t result_n[2], result_fill[2], result_acc_i[2], result_acc_q[2], result_out_crc;
uint32_t result_bb_crc[2][4], result_fe_crc[2][4], result_slices, result_core0_frames;

static int setup(int modulation, int split, uint32_t *out, unsigned options) {
    memset(&live, 0, sizeof live);
    live.modulation = modulation;
    live.split = split;
    live.options = options;
    live.stream = (dvbs2_apsk_stream_t){.out = out, .mask = ~0u};
    packet_index = 0;
    memset(&pvspi, 0, sizeof pvspi);
    pv_init(PV_EMU_MSGS);
    dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);
    dvbs2_init(&DVBS2_CODES[S2_N5_6], true);
    if (!dvbs2_apsk_init(&live.profile, &DVBS2_CODES[S2_N5_6], modulation, true, 0, gold, 2048)) return 1;
    if ((options & OPT_FAST_MAP) &&
        !dvbs2_apsk_fast_init(&live.fast, &live.profile, fast_arena, DVBS2_APSK_FAST_WORDS, 0)) return 1;
    live.fe_slots[0] = dvbs2_frame_buffer();
    live.fe_slots[1] = slot1;
    for (unsigned k = DVBS2_FE_WORDS; k < DVBS2_FE_WORDS + 8; k++) slot1[k] = 0x6b6b6b6bu;
    return 0;
}

// Same serial frame 0 as the bench prefill, then either serial frames or the split
// schedule. A seeded LCG chooses which core steps; core 1 only steps when it would
// not spin (its device wait loop has no other thread to release it here).
// options selects the bench's PL mapper (0 original, 1 A3 fast leaves).
int trial(int modulation, unsigned pack, uint32_t seed, unsigned bias, unsigned options) {
    dvbs2_ldpc_set_pack(pack);
    for (int split = 0; split < 2; split++) {
        if (setup(modulation, split, split ? split_out : serial_out, options)) return 1;
        if (!encode_frame()) return 2;
        if (split) {
            if (!split_prime()) return 3;
            uint32_t s = seed;
            while (live.frames < FRAMES) {
                s = s * 1664525u + 1013904223u;
                const bool core1_ready = live.bb_produced == live.bb_consumed ||
                    live.fe_consumed != live.fe_produced;
                if (core1_ready && (s >> 24) < bias) {
                    if (!split_encode_step()) return 4;
                } else if (!bch_service() && !core1_ready) return 5;   // no progress possible
                for (unsigned k = DVBS2_FE_WORDS; k < DVBS2_FE_WORDS + 8; k++)
                    if (slot1[k] != 0x6b6b6b6bu) return 6;
            }
            result_slices = live.bch_slices;
            result_core0_frames = live.bch_frames;
        } else {
            while (live.frames < FRAMES) if (!encode_frame()) return 7;
        }
        result_n[split] = live.stream.n; result_fill[split] = live.stream.fill;
        result_acc_i[split] = live.stream.acc_i; result_acc_q[split] = live.stream.acc_q;
        memcpy(result_bb_crc[split], live.bb_crc, sizeof live.bb_crc);
        memcpy(result_fe_crc[split], live.fe_crc, sizeof live.fe_crc);
        if (live.encoder_errors) return 8;
    }
    // Split mode may have mapped part of later frames; compare the common prefix.
    const uint32_t n = result_n[0];
    if (result_n[1] < n) return 9;
    if (memcmp(serial_out, split_out, n * sizeof serial_out[0])) return 10;
    result_out_crc = pv_crc32(0, serial_out, n * sizeof serial_out[0]);
    return 0;
}
'''
    return (head + macros + setup + constants + live_type +
            "\nstatic live_t live_store;\n#define live live_store\n" + leaves + tail)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yapogee-apsk-split-") as build:
        source, library = Path(build) / "trial.c", Path(build) / "trial.so"
        source.write_text(harness())
        subprocess.run([shutil.which("cc"), "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-function", "-shared", "-fPIC", "-I" + str(SRC), str(source),
                        str(SRC / "dvbs2.c"), str(SRC / "dvbs2_apsk.c"), str(SRC / "iqgen.c"),
                        str(SRC / "pvproto.c"), "-o", str(library)], check=True)
        lib = ct.CDLL(str(library))
        lib.trial.argtypes = [ct.c_int, ct.c_uint, ct.c_uint32, ct.c_uint, ct.c_uint]
        lib.trial.restype = ct.c_int
        get = lambda name: ct.c_uint32.in_dll(lib, name).value
        array = lambda name, n: list((ct.c_uint32 * n).in_dll(lib, name))
        for modulation in (16, 32):
            for pack in (0, 1):
                for seed, bias in ((1, 128), (0x9E3779B9, 32), (0x2545F491, 224), (7, 255)):
                    reference = None
                    # options 1: the A3 fast mapper inside the actual bench leaves must
                    # reproduce the original serial stream word for word.
                    for options in (0, 1):
                        code = lib.trial(modulation, pack, seed, bias, options)
                        assert code == 0, (modulation, pack, seed, bias, options, code)
                        crcs = array("result_bb_crc", 8), array("result_fe_crc", 8)
                        assert crcs[0][:4] == crcs[0][4:] and crcs[1][:4] == crcs[1][4:], "BB/FE CRCs differ"
                        assert get("result_core0_frames") >= FRAMES - 3  # frame 0 serial, frames 1-2 primed
                        stream = (array("result_n", 2)[0], array("result_fill", 2)[0], get("result_out_crc"))
                        if reference is None:
                            reference = stream
                        assert stream == reference, ("fast mapper stream differs", modulation, pack, seed, options)
                        print(json.dumps(dict(modulation=modulation, ldpc_pack=pack, seed=seed, core1_bias=bias,
                                              options=options, frames=FRAMES, compared_words=stream[0],
                                              stream_crc=stream[2], core0_slices=get("result_slices"),
                                              core0_frames=get("result_core0_frames"))), flush=True)


if __name__ == "__main__":
    main()
