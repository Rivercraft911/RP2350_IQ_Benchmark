"""Exercise the actual dualstream BB/FE queue leaves with all optional code rates.

This extracts portable leaves from bench_dual.c and tx.c into a temporary native
harness. Device I/O and timing are stubbed; BB construction, queue allocation,
frontend, BCH, LDPC, and mapping remain the firmware's actual C. Sequentially
interleaving 14 frames verifies buffer ownership and tails, not multicore timing.
Independent Python DVB-S2 references check every emitted symbol and first BB CRCs.
"""
from __future__ import annotations

import ctypes as ct
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zlib

import numpy as np

import dual_spi as spi

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware" / "src"
FRAMES = 14


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
    source = (SRC / "bench_dual.c").read_text()
    tx = (SRC / "tx.c").read_text()
    source_enum = source[source.index("enum dual_source {"):]
    source_enum = source_enum[:source_enum.index("};") + 2]
    constants = source[source.index("enum { SYMBOLS_PER_WORD"):]
    constants = constants[:constants.index("};") + 2]
    encoder = source[source.index("static struct {"):source.index("} encoder;") + len("} encoder;")]
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
#include "iqgen.h"
#include "pvproto.h"
#define __dmb() ((void)0)
'''
    setup = r'''
static uint32_t input[IN_WORDS], cap_buf[CAP_WORDS_MAX];
static struct {uint32_t words[BB_WORDS], guard[8];} bb_store;
static struct {uint32_t words[TABLE_WORDS], guard[8];} q_store;
#define bb_buf bb_store.words
static struct {uint32_t *tq;} cfg = {.tq=q_store.words};
static const dvbs2_code_t *s2c;
static uint32_t cycles(void) {return 0;}
static uint32_t time_us_32(void) {return 0;}
static uint32_t packet_index;
static struct {volatile uint32_t errors;} pvspi;
static uint8_t *pvspi_next_packet(void) {
    static uint8_t packet[188];
    const uint32_t k=packet_index++%112;
    packet[0]=0x47; packet[1]=1; packet[2]=0; packet[3]=0x10|(k&15);
    for(unsigned j=0;j<184;j++) packet[j+4]=(k+j)&255;
    return packet;
}
'''
    leaves = "\n\n".join(function(source, name) for name in (
        "spi_source", "build_spi_frame", "split_flip_bb", "split_begin_frame",
        "prefill_encoder", "begin_frontend", "refill_bb_queue"))
    tail = r'''
static uint32_t trace[(14*33282+15)/16];
uint32_t trial_crc[4], trial_n, trial_fill, trial_acc_i, trial_acc_q;
uint32_t trial_frames, trial_incremental, trial_bb_words, trial_alias;
uint32_t trial_frame_symbols;
int trial(int code_index,int gcs) {
    memset(input,0xa5,sizeof input);
    memset(&q_store,0x5a,sizeof q_store);
    memset(&bb_store,0x3c,sizeof bb_store);
    packet_index=0; pvspi.errors=0; pv_init(16); dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);
    const dvbs2_code_t *c=&DVBS2_CODES[code_index];
    if(!prefill_encoder(gcs?SOURCE_PV_GCS:SOURCE_PV_SPI,input,0,code_index,c->kbch)) return 1;
    if(encoder.bb_frames!=6) return 2;
    for(unsigned i=0;i<4;i++) trial_crc[i]=encoder.bb_crc[i];
    trial_bb_words=encoder.bb_words;
    trial_alias=encoder.bb_slots[1]==bb_buf;
    trial_incremental=0;
    uint32_t copied=0;
    for(;copied<encoder.stream.n;copied++) trace[copied]=input[copied&(IN_WORDS-1)];
    // The actual queue leaves repeatedly reuse both BB and frontend slots.
    for(unsigned iteration=0;iteration<12;iteration++) {
        const unsigned taken=encoder.fe_consumed;
        uint32_t *frame=encoder.fe_slots[taken&1];
        dvbs2_frame_ldpc_into(frame);
        dvbs2_frame_finish_from(frame,&encoder.stream);
        encoder.frames++; encoder.fe_consumed++;
        for(;copied<encoder.stream.n;copied++) trace[copied]=input[copied&(IN_WORDS-1)];
        if(begin_frontend(true,encoder.fe_produced)) {
            trial_incremental++;
            while(!dvbs2_frontend_step(&encoder.front,128)) {}
        }
        encoder.fe_produced++;
        if(!refill_bb_queue()) return 3;
        for(unsigned i=0;i<PV_EMU_WORDS;i++) if(q_store.words[i]!=0x5a5a5a5a) return 4;
        for(unsigned i=0;i<8;i++) {
            if(q_store.guard[i]!=0x5a5a5a5a) return 5;
            if(bb_store.guard[i]!=0x3c3c3c3c) return 6;
        }
        for(unsigned slot=0;slot<2;slot++) {
            const uint32_t last=encoder.bb_slots[slot][encoder.bb_words-1];
            const unsigned tail=c->kbch%32;
            if(tail && (last&(~0u>>tail))) return 7;
        }
    }
    trial_n=encoder.stream.n; trial_fill=encoder.stream.fill;
    trial_acc_i=encoder.stream.acc_i; trial_acc_q=encoder.stream.acc_q;
    trial_frames=encoder.frames; trial_frame_symbols=dvbs2_plframe_symbols();
    return 0;
}
uint32_t trial_word(unsigned index) {return trace[index];}
int trial_reject(int transport) {
    pvspi.errors=0; pv_stats.bad_crc=0; encoder.input_failed=false;
    const uint32_t before=encoder.bb_frames;
    if(transport) pvspi.errors=1; else pv_stats.bad_crc=1;
    const bool accepted=build_spi_frame(bb_buf);
    return !accepted && encoder.input_failed && encoder.bb_frames==before;
}
'''
    return (head + macros + setup + source_enum + constants + encoder +
            function(tx, "tx_code") + leaves + tail)


def bbframe(index: int, rate: str, gcs: bool) -> np.ndarray:
    data_bytes = spi.ref.code(rate, False).kbch // 8 - 10
    first, offset = divmod(index * data_bytes, 188)
    count = (offset + data_bytes + 187) // 188
    raw = tuple(spi.pv.pattern_packet(k) for k in range(spi.PACKET_PERIOD))
    packets = raw if gcs else tuple(
        bytes([spi.ref.crc8(np.unpackbits(np.frombuffer(raw[k - 1][1:], np.uint8)))]) + packet[1:]
        for k, packet in enumerate(raw))
    data = bytearray(b"".join(packets[k % spi.PACKET_PERIOD] for k in range(first, first + count)))
    if first == 0 and not gcs:
        data[0] = 0
    header = (spi.ref.bbheader(spi.ref.matype1(spi.ref.GCS, 0.20), data_bytes * 8, sync=0xB9) if gcs else
              spi.ref.bbheader(spi.ref.matype1(spi.ref.TS, 0.20), data_bytes * 8, 1504, 0x47,
                               (-index * data_bytes * 8) % 1504))
    return np.concatenate([header, np.unpackbits(np.frombuffer(data[offset:offset + data_bytes], np.uint8))])


def check(lib: ct.CDLL, code_index: int, rate: str, gcs: bool) -> dict:
    assert lib.trial(code_index, gcs) == 0, (code_index, gcs, "native queue/canary")
    crc = list((ct.c_uint32 * 4).in_dll(lib, "trial_crc"))
    frames = [bbframe(k, rate, gcs) for k in range(FRAMES)]
    assert crc == [zlib.crc32(np.packbits(frame).tobytes()) for frame in frames[:4]]
    get = lambda name: ct.c_uint32.in_dll(lib, "trial_" + name).value
    n, fill = get("n"), get("fill")
    assert get("frames") == FRAMES
    assert get("incremental") == (0 if code_index == 6 else FRAMES - 2)
    assert get("alias") == (code_index != 5)
    axes = [spi.ref.plframe(spi.ref.fecframe(frame, rate), rate, pilots=True) for frame in frames]
    bi, bq = (np.concatenate([frame[axis] for frame in axes]) for axis in (0, 1))
    assert len(bi) == FRAMES * get("frame_symbols") == n * 16 + fill
    expected = spi.ref.pack_words(bi[:n * 16], bq[:n * 16])
    actual = np.asarray([lib.trial_word(k) for k in range(n)], np.uint32)
    assert np.array_equal(expected, actual), (code_index, gcs, "continuous symbol stream")
    for name, bits in (("acc_i", bi), ("acc_q", bq)):
        tail = sum(int(bit) << i for i, bit in enumerate(bits[n * 16:]))
        assert get(name) == tail, (code_index, gcs, name)
    return dict(code_index=code_index, rate=rate, gcs=gcs, bb_crc=crc, frames=FRAMES,
                checked_words=n, partial_symbols=fill, bb_words=get("bb_words"),
                second_slot_bb_buf=bool(get("alias")), incremental_frames=get("incremental"))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yapogee-dual-codes-") as build:
        source, library = Path(build) / "trial.c", Path(build) / "trial.so"
        source.write_text(harness())
        subprocess.run([shutil.which("cc"), "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", "-I" + str(SRC), str(source), str(SRC / "dvbs2.c"),
                        str(SRC / "iqgen.c"), str(SRC / "pvproto.c"), "-o", str(library)], check=True)
        lib = ct.CDLL(str(library))
        lib.trial.argtypes, lib.trial.restype = [ct.c_int, ct.c_int], ct.c_int
        lib.trial_word.argtypes, lib.trial_word.restype = [ct.c_uint], ct.c_uint
        for code_index, rate in ((5, "2/3"), (6, "3/4"), (8, "5/6")):
            for gcs in (False, True):
                print(json.dumps(check(lib, code_index, rate, gcs)), flush=True)
                for transport in (False, True):
                    assert lib.trial_reject(transport) == 1, "faulted BBFRAME was published"


if __name__ == "__main__":
    main()
