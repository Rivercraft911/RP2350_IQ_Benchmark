"""Actual live prefill/frontend leaves + portable FEC, finite DMA credit model; no USB.

The scheduler interleaves events, not M33 time. Python independently checks every
TS/GCS symbol through sixteen frames, including source-ring wrap and frame tails.
"""
from pathlib import Path
import json
import subprocess
import tempfile

import numpy as np

import dual_spi as spi

ROOT = Path(__file__).resolve().parents[1]


def function(source, signature):
    start = source.index(signature)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PREFIX = r'''
#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "dvbs2_codes.h"
#include "iqgen.h"
#include "iqquadptr.h"
#include "pvproto.h"
#define __dmb() atomic_thread_fence(memory_order_seq_cst)
enum dual_source { SOURCE_DVBS2_SPLIT=4, SOURCE_PV_SPI=5, SOURCE_PV_GCS=6 };
enum { IN_WORDS=8192, CAP_WORDS_MAX=14336, CAPTURE_WORDS=4096,
       BB_WORDS=DVBS2_FE_WORDS, FRAME_SLOTS=2, FRAME_SLOT_MASK=1,
       SYMBOLS_PER_WORD=16, BB_TOGGLE_STRIDE=64, PV_EMU_WORDS=16*333,
       TABLE_WORDS=8192, GCS_MATYPE1=0x72, GCS_SYNC=0xb9, TS_MATYPE1=0xf2,
       FRAMES=16, FRAME_SYMS=33282, OUT_WORDS=(FRAMES*FRAME_SYMS+15)/16 };
static uint32_t bb_buf[BB_WORDS], cap_buf[CAP_WORDS_MAX], qtable[TABLE_WORDS];
static uint32_t input[IN_WORDS], captured[OUT_WORDS];
static iq_cfg_t cfg = {.tq=qtable};
static struct { uint32_t errors,head; } pvspi;
static uint32_t stamp, message_no, packet_no;
static uint8_t message[PV_MSG_BYTES];
static uint32_t cycles(void) { return stamp++; }
static bool spi_source(enum dual_source source) { return source>=5; }
static void tx_code(int code, bool pilots) { dvbs2_init(&DVBS2_CODES[code],pilots); }
static uint8_t *pvspi_next_packet(void) {
    if (packet_no==7) packet_no=0;
    if (!packet_no) {
        pv_pattern_message(message,message_no++%16,16);
        assert(pv_accept(message,-1)==7);
        pvspi.head++;
    }
    return message+12+188*packet_no++;
}
'''

CHECK = r'''
static void run(enum dual_source source, bool freeze) {
    pv_init(16); message_no=packet_no=0; pvspi.head=pvspi.errors=0;
    assert(dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM));
    assert(dvbs2_map_set_kernel(1) && dvbs2_ldpc_set_pack(1));
    memset(input,0xa5,sizeof input);
    assert(prefill_encoder(source,input,0,S2_N5_6,53840));
    assert(encoder.prefill==2 && encoder.total==4160 && encoder.stream.fill==4);
    encoder.consumed=0;
    volatile uint32_t grants[IQQUADPTR_GRANT_ENTRIES]={0};
    uint64_t granted=iqquadptr_grant_until(grants,0,encoder.total,0);
    uint32_t read[2]={0}, copied=0, loops=0;
    bool active=false, frozen=false;
    while (copied<FRAMES*FRAME_SYMS/16) {
        assert(++loops<100000);
        if (freeze && copied>=8192) frozen=true;
        const uint32_t low=read[0]<read[1]?read[0]:read[1];
        encoder.consumed=low>IQQUADPTR_REUSE_GUARD_WORDS?low-IQQUADPTR_REUSE_GUARD_WORDS:0;
        if (!frozen) granted=iqquadptr_grant_until(grants,granted,encoder.total,encoder.consumed);
        assert(granted<=encoder.total && encoder.total-encoder.consumed<=IN_WORDS);
        // Independent readers, with deterministic stalls and bounded axis skew.
        for (unsigned axis=0;axis<2;axis++) {
            const uint32_t step=loops%(axis?11:13)?37:0;
            uint64_t end=(uint64_t)read[axis]+step;
            if (end>granted) end=granted;
            read[axis]=(uint32_t)end;
        }
        const uint32_t retired=read[0]<read[1]?read[0]:read[1];
        while (copied<retired && copied<FRAMES*FRAME_SYMS/16) {
            captured[copied]=input[copied&(IN_WORDS-1)]; copied++;
        }
        if (frozen && read[0]==granted && read[1]==granted) break;
        // The production frontend and BB queue leaves, with one backend event.
        if (active || (encoder.fe_produced-encoder.fe_consumed<FRAME_SLOTS &&
                       encoder.bb_consumed!=encoder.bb_produced)) {
            const uint32_t produced=encoder.fe_produced;
            bool done;
            if (!active) { done=!begin_frontend(true,produced); active=!done; }
            else { done=dvbs2_frontend_step(&encoder.front,96); if(done) active=false; }
            if (done) encoder.fe_produced=produced+1;
        }
        if (refill_bb_queue()) continue;
        if (encoder.fe_consumed!=encoder.fe_produced &&
            encoder.stream.n+encoder.frame_words+1u-encoder.consumed<=IN_WORDS) {
            uint32_t *frame=encoder.fe_slots[encoder.fe_consumed&FRAME_SLOT_MASK];
            dvbs2_frame_ldpc_into(frame);
            dvbs2_frame_finish_from(frame,&encoder.stream);
            encoder.total=encoder.stream.n; encoder.fe_consumed++;
        }
    }
    assert(!encoder.input_failed && !pvspi.errors && !pv_stats.lost && !pv_stats.null_packets);
    if (freeze) assert(frozen && copied==granted && copied>=8192);
    else assert(copied==FRAMES*FRAME_SYMS/16);
    printf("{\"source\":%d,\"freeze\":%d,\"copied\":%u,\"granted\":%llu,\"words\":[",source,freeze,copied,(unsigned long long)granted);
    for (unsigned k=0;k<copied;k++) printf("%s%u",k?",":"",captured[k]);
    puts("]}");
}
int main(void) {
    for (unsigned s=5;s<=6;s++) for (unsigned freeze=0;freeze<=1;freeze++) run(s,freeze);
}
'''


def main():
    src = ROOT/'firmware/src'
    text = (src/'bench_dual.c').read_text()
    start = text.index('static struct {')
    end = text.index('} encoder;', start) + len('} encoder;')
    leaves = ('static bool build_spi_frame(', 'static void split_flip_bb(',
              'static void split_begin_frame(', 'static inline bool refill_bb_queue(',
              'static bool prefill_encoder(', 'static inline bool begin_frontend(')
    code = PREFIX + text[start:end] + '\n' + '\n'.join(function(text, sig) for sig in leaves) + CHECK
    with tempfile.TemporaryDirectory(prefix='dualptr-live-native-') as directory:
        c, exe = Path(directory)/'test.c', Path(directory)/'test'
        c.write_text(code)
        subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-I'+str(src), str(c), *[str(src/name) for name in
                        ('dvbs2.c', 'iqgen.c', 'pvproto.c', 'iqquadptr.c', 'iqquad.c')],
                        '-o', str(exe)], check=True)
        records = [json.loads(line) for line in subprocess.check_output([str(exe)], text=True).splitlines()]
    for source in (5, 6):
        frames = [spi.ref.plframe(spi.ref.fecframe(spi.bbframe(k, gcs=source == 6, rate='5/6'), '5/6'),
                                 '5/6', pilots=True) for k in range(16)]
        want = spi.ref.pack_words(*(np.concatenate([f[axis] for f in frames]) for axis in (0, 1)))
        for record in [r for r in records if r['source'] == source]:
            got = np.asarray(record['words'], dtype=np.uint32)
            assert np.array_equal(got, want[:len(got)]), (source, record['freeze'], 'stream differs')
            print(f"dualptr native source{source} freeze{record['freeze']}: {len(got)} exact words; "
                  "actual prefill/BB/frontend leaves, split backend, axis skew, ring wrap, finite credits")


if __name__ == '__main__':
    main()
