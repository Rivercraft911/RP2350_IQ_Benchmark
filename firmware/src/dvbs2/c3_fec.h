// CPU-only frame-parallel experiment. All storage is caller-owned. Initialize
// pv_init and dvbs2_init, select a nonbaseline BCH kernel, and keep those tables,
// selectors and the matching normal-frame mapper configuration fixed during jobs.
#pragma once
#include "dvbs2_8psk.h"

enum {
    C3_FRAME_SYMBOLS = 22194, C3_FRAME_CYCLES = 8 * C3_FRAME_SYMBOLS,
    C3_RING_GROUPS = 4096, C3_ROWS_WORDS = 60 * 12,
    C3_GCS = 0, C3_BCH, C3_LDPC, C3_MAP, C3_DONE,
    C4_LDPC = 1, C4_MAP = 2, C4_BCH = 4, C4_BB = 8, C4_DEADLINES = 16, C4_ALL = 31,
    C5_LDPC = 32, C6_MAP = 64,
    C7_BCH_PLACE = 128, C7_BCH_LDM = 256, C7_TRANSPOSE = 512, C7_TABLE3 = 1024,
    C8_ALL = C4_ALL | C5_LDPC | C6_MAP | C7_BCH_PLACE | C7_BCH_LDM | C7_TRANSPOSE | C7_TABLE3,
    // The best candidate is unmeasured in combination. TABLE3 needs c3pair layout4;
    // c3fec's "best" spelling selects BASE because its arena has no large BCH table.
    C8_BEST_BASE = (C4_ALL & ~C4_BCH) | C5_LDPC | C6_MAP | C7_BCH_PLACE,
    C8_BEST = C8_BEST_BASE | C7_TABLE3,
};

// In-memory GCS source: chronological payload byte j is (29*j+7) modulo256.
// Build <=max_words MSB-first BB words, including the GCS BBHEADER. No PV-SPI
// transfer, validation or wait is simulated. Returns the next word cursor.
uint32_t c3_gcs_slice(uint32_t *bb, const uint8_t *pattern, const dvbs2_code_t *code, uint32_t frame,
                      uint32_t cursor, uint32_t max_words);

typedef struct {
    uint32_t bb[DVBS2_FE_WORDS], frame[DVBS2_FE_WORDS], rows[C3_ROWS_WORDS];
    dvbs2_frontend_t front;
    dvbs2_ldpc_state_t ldpc;
    dvbs2_8psk_codes_stream_t stream;
    dvbs2_8psk_codes_state_t map;
    const dvbs2_8psk_cfg_t *cfg;
    uint32_t index, stage, cursor, options;
    void *ldpc_group; // optional mode1 leaf / mode2 PIC bundle; NULL = prepared default
    dvbs2_bch_leaf_t bch_leaf;
    const void *bch_table;
    dvbs2_transpose_fn transpose; // redundant on Arm with C5's private pack transpose
} c3_job_t;

// Caller provides credit for this frame's complete output interval before begin.
// pattern contains two identical256-byte periods, to permit unaligned word reads.
void c3_job_begin(c3_job_t *job, const dvbs2_8psk_cfg_t *cfg, uint32_t index,
                  uint32_t *i, uint32_t *q);
void c3_job_begin_options(c3_job_t *job, const dvbs2_8psk_cfg_t *cfg, uint32_t index,
                          uint32_t *i, uint32_t *q, uint32_t options);
// One bounded slice:64 GCS/BCH words, one LDPC unit, or eight map words
// (32 map words with C6_MAP). C5_LDPC and C6_MAP override C4_LDPC and C4_MAP.
// False means a configuration error; stage==C3_DONE means ready for publication.
bool c3_job_step(c3_job_t *job, const uint8_t *pattern);
// In-order publication merges the one shared boundary word and returns the
// complete-group cursor. carry holds the preceding frame's unpublished tail.
uint32_t c3_job_publish(const c3_job_t *job, uint32_t carry[2]);

// Device command, included only in IQ4HIST_EXPERIMENT builds.
void bench_c3_fec(int code_index, int ms, int slow_us, int cpw, int gap_us);
void bench_c3_fec_options(int code_index, int ms, int slow_us, int cpw, int gap_us, int options);
void bench_c3_pair(int code_index, int pairs, int layout, int who, int options);
