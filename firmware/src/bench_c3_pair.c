// SRAM bank contention between the two C3/C4 FEC workers. Opt-in experiment.
//
// `c3pair code pairs layout who options` encodes frame 0 (job A) and frame 1 (job B) `pairs`
// times and reports cycles per stage for each job:
//   who 0: core 0 runs A then B (no concurrent FEC)      who 1: core 1 runs A then B
//   who 2: core 0 runs A while core 1 runs B, both released together
//   layout 0: both jobs, their code rings and one shared gold/LUT/LDPC-leaf set in SRAM0-3
//   layout 1: job B with its own rings, gold, LUT and LDPC leaf copy in SRAM4-7 (cap_buf)
// Shared read-only tables (BCH, BB PRBS, LDPC addresses) stay where the linker put them.
#include <stdio.h>
#include <string.h>

#include "c3_fec.h"
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "tx.h"

enum { RING = 2048, STAGES = 4, MAX_PAIRS = 200 };

typedef struct {
    c3_job_t job;
    dvbs2_8psk_cfg_t cfg;
    uint32_t gold[1384], lut[DVBS2_8PSK_CODES_LUT_WORDS], code[1792];
} lane_t;

typedef struct {
    lane_t a, b;            // a: job A and the shared tables; b: job B in layout 0
    uint8_t pattern[512];
} low_t;                    // SRAM0-3: ring_buf (free while no output engine runs)

typedef struct {
    lane_t b;
    uint32_t ring[2 * RING];
} high_t;                   // SRAM4-7: cap_buf

_Static_assert(sizeof(low_t) <= sizeof ring_buf, "low layout fits ring_buf");
_Static_assert(sizeof(high_t) <= CAP_WORDS_MAX * 4, "high layout fits cap_buf");
_Static_assert(4 * RING <= IN_WORDS, "two job rings fit in_buf");

static low_t *const low = (low_t *)ring_buf;
static high_t *const high = (high_t *)cap_buf;

static struct {
    volatile uint32_t go, done, stop;
    c3_job_t *job;
    uint32_t stage[STAGES], total, max;
} remote;

static uint32_t run_job(c3_job_t *job, const uint8_t *pattern, uint32_t stage[STAGES]) {
    const uint32_t start = cycles();
    while (job->stage != C3_DONE) {
        const unsigned s = job->stage;
        const uint32_t t = cycles();
        c3_job_step(job, pattern);
        stage[s] += cycles() - t;
    }
    return cycles() - start;
}

static void core1_task(uint32_t unused) {
    (void)unused;
    enable_cyccnt();
    uint32_t seen = 0;
    while (!remote.stop) {
        if (remote.go == seen) { __wfe(); continue; }
        seen = remote.go;
        __dmb();
        const uint32_t total = run_job(remote.job, low->pattern, remote.stage);
        remote.total += total;
        if (total > remote.max) remote.max = total;
        __dmb();
        remote.done = seen;
        __sev();
    }
}

static void begin(lane_t *lane, const lane_t *tables, uint32_t index, uint32_t *ring, uint32_t options) {
    c3_job_begin_options(&lane->job, &tables->cfg, index, ring, ring + RING, options);
    lane->job.stream.mask = RING - 1u;
    lane->job.ldpc_group = (options & C4_LDPC) ? (void *)((uintptr_t)tables->code | 1u) : NULL;
}

static bool tables(lane_t *lane, const dvbs2_code_t *code, uint32_t options) {
    return dvbs2_8psk_init(&lane->cfg, code, true, 0, lane->gold, 1384) &&
        (!(options & C4_MAP) || dvbs2_8psk_codes_lut_init(&lane->cfg, lane->lut, DVBS2_8PSK_CODES_LUT_WORDS)) &&
        (!(options & C4_LDPC) || dvbs2_ldpc_copy(lane->code, sizeof lane->code));
}

void bench_c3_pair(int code_index, int pairs, int layout, int who, int options) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((code_index != S2_N2_3 && code_index != S2_N3_4 && code_index != S2_N5_6) || pairs < 1 ||
        pairs > MAX_PAIRS || layout < 0 || layout > 1 || who < 0 || who > 2 || options < 0 || options > C4_ALL) {
        printf("@{\"cmd\":\"c3pair\",\"error\":\"needs code5/6/8, pairs1..200, layout0/1, who0/1/2, options0..31\"}\n");
        return;
    }
    const dvbs2_code_t *code = &DVBS2_CODES[code_index];
    const dvbs2_bch_kernel_t saved_bch = dvbs2_bch_get_kernel();
    const unsigned saved_bank = dvbs2_frontend_get_bank();
    memset(low, 0, sizeof *low);
    memset(high, 0, sizeof *high);
    memset(in_buf, 0, sizeof in_buf);
    for (unsigned j = 0; j < sizeof low->pattern; ++j) low->pattern[j] = (uint8_t)(29u * j + 7u);
    dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);
    dvbs2_frontend_set_bank(0);
    dvbs2_init(code, true);
    pv_init(65536);
    // Every option-1 job needs a prepared default; each lane also gets its own leaf copy.
    const bool ok = tables(&low->a, code, (uint32_t)options) && tables(&high->b, code, (uint32_t)options) &&
        (!(options & C4_LDPC) || dvbs2_ldpc_prepare(low->a.code, sizeof low->a.code));
    if (!ok) {
        dvbs2_bch_set_kernel(saved_bch);
        dvbs2_frontend_set_bank(saved_bank);
        printf("@{\"cmd\":\"c3pair\",\"error\":\"C3/C4 setup failed\"}\n");
        return;
    }
    lane_t *lane_b = layout ? &high->b : &low->b;
    const lane_t *tables_b = layout ? &high->b : &low->a;
    uint32_t *ring_a = in_buf, *ring_b = layout ? high->ring : in_buf + 2 * RING;
    uint32_t stage_a[STAGES] = {0}, stage_b[STAGES] = {0}, total_a = 0, total_b = 0, max_a = 0, max_b = 0;
    remote = (typeof(remote)){0};
    enable_cyccnt();
    if (who) tx_worker_start(core1_task, 0);
    for (int p = 0; p < pairs; ++p) {
        begin(&low->a, &low->a, 0, ring_a, (uint32_t)options);
        begin(lane_b, tables_b, 1, ring_b, (uint32_t)options);
        const uint32_t irq = save_and_disable_interrupts();
        if (who == 0) {
            uint32_t t = run_job(&low->a.job, low->pattern, stage_a);
            total_a += t; if (t > max_a) max_a = t;
            t = run_job(&lane_b->job, low->pattern, stage_b);
            total_b += t; if (t > max_b) max_b = t;
        } else if (who == 1) {
            for (unsigned k = 0; k < 2; ++k) {
                remote.job = k ? &lane_b->job : &low->a.job;
                __dmb();
                ++remote.go;
                __sev();
                while (remote.done != remote.go) __wfe();
                __dmb();
            }
        } else {
            remote.job = &lane_b->job;
            __dmb();
            ++remote.go;
            __sev();
            const uint32_t t = run_job(&low->a.job, low->pattern, stage_a);
            total_a += t; if (t > max_a) max_a = t;
            while (remote.done != remote.go) __wfe();
            __dmb();
        }
        restore_interrupts(irq);
    }
    if (who) {
        remote.stop = 1;
        __dmb();
        __sev();
        tx_worker_wait();
    }
    if (who == 1) {
        // Core 1 ran both jobs into one accumulator; report it as job A+B.
        memcpy(stage_a, remote.stage, sizeof stage_a);
        total_a = remote.total; max_a = remote.max;
    } else if (who == 2) {
        memcpy(stage_b, remote.stage, sizeof stage_b);
        total_b = remote.total; max_b = remote.max;
    }
    const uint32_t crc_a = pv_crc32(0, ring_a, 2 * RING * sizeof(uint32_t));
    const uint32_t crc_b = pv_crc32(0, ring_b, 2 * RING * sizeof(uint32_t));
    const unsigned runs_a = who == 1 ? 2u * (unsigned)pairs : (unsigned)pairs, runs_b = (unsigned)pairs;
    printf("@{\"cmd\":\"c3pair\",\"git\":\"%s\",\"clk_hz\":%lu,\"code_index\":%d,\"s2_code\":\"%s\",\"pairs\":%d,"
           "\"layout\":%d,\"who\":%d,\"options\":%d,\"ring_a\":%lu,\"ring_b\":%lu,\"job_b\":%lu,\"leaf_b\":%lu,"
           "\"crc_a\":%lu,\"crc_b\":%lu,\"jobs\":[",
           GIT_REV, (unsigned long)hz, code_index, code->name, pairs, layout, who, options,
           (unsigned long)(uintptr_t)ring_a, (unsigned long)(uintptr_t)ring_b,
           (unsigned long)(uintptr_t)&lane_b->job, (unsigned long)(uintptr_t)tables_b->code,
           (unsigned long)crc_a, (unsigned long)crc_b);
    for (unsigned j = 0; j < (who == 1 ? 1u : 2u); ++j) {
        const uint32_t *st = j ? stage_b : stage_a;
        const unsigned runs = j ? runs_b : runs_a;
        printf("%s{\"name\":\"%s\",\"core\":%d,\"runs\":%u,\"avg_cycles\":%lu,\"max_cycles\":%lu,"
               "\"avg_stage\":{\"gcs\":%lu,\"bch\":%lu,\"ldpc\":%lu,\"map\":%lu}}",
               j ? "," : "", who == 1 ? "A+B" : (j ? "B" : "A"),
               who == 1 ? 1 : (j && who == 2 ? 1 : 0), runs,
               (unsigned long)((j ? total_b : total_a) / runs), (unsigned long)(j ? max_b : max_a),
               (unsigned long)(st[0] / runs), (unsigned long)(st[1] / runs),
               (unsigned long)(st[2] / runs), (unsigned long)(st[3] / runs));
    }
    printf("]}\n");
    dvbs2_bch_set_kernel(saved_bch);
    dvbs2_frontend_set_bank(saved_bank);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
