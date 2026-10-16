// B8's single-consumer PV-SPI GCS adapter. All storage is caller-owned; only
// core0 calls this API, in frame order, before handing a prepared job to a core.
#pragma once
#include "c3_fec.h"
#include "pvproto.h"

typedef enum {
    B8_INGRESS_MORE = 0, B8_INGRESS_READY, B8_INGRESS_WAIT, B8_INGRESS_ERROR,
} b8_ingress_result_t;

typedef struct {
    uint8_t *packet;                 // borrowed until the next packet callback
    uint32_t packet_off;
    c3_job_t *active;
    uint32_t partial, partial_bytes, prepare_cursor, phase;
    uint8_t header[10];
    uint32_t crc, frames, bb_crc[4];  // first four raw BBFRAME CRCs, byte order on air
} b8_ingress_t;

// Call after pv_init. Does not consume a packet or allocate storage.
void b8_ingress_init(b8_ingress_t *source);
// First call implicitly starts a job initialized by c3_job_begin_options().
// Each call emits/prepares <=min(max_words,64) words. A zero budget does nothing.
// Option8 fuses scrambling into the source write; without it, the subsequent
// copy/scramble/zero pass is also sliced. READY has admitted the prepared BCH
// state and changed job->stage to C3_BCH; c3_job_step then needs no pattern.
// WAIT preserves an incomplete word and the current stream position. It is not
// a GCS underflow: the caller enforces its own startup/runtime starvation limit.
// Never start another job until READY. Packets remain unmodified and retained
// across calls and frame boundaries, until all188 bytes have been consumed.
b8_ingress_result_t b8_ingress_step(b8_ingress_t *source, c3_job_t *job,
                                    pv_packet_fn next, uint32_t max_words);
