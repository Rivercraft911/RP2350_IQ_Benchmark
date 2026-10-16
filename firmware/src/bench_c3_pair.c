// SRAM bank contention between the two C3/C4 FEC workers. Opt-in experiment.
//
// `c3pair code pairs layout who options` encodes frame 0 (job A) and frame 1 (job B) `pairs`
// times and reports cycles per stage for each job:
//   who 0: core 0 runs A then B (no concurrent FEC)      who 1: core 1 runs A then B
//   who 2: core 0 runs A while core 1 runs B, both released together
//   who 3..6: core 0 runs A then B while core 1 hammers one shared resource: 3 flash (XIP) code
//   fetches, 4 SRAM0-3 copies, 5 SRAM4-7 copies, 6 an SRAM-resident ALU loop (fetch only),
//   7 dvbs2_transpose32 in a loop (SCRATCH_X code fetch, the LDPC transpose leaf)
//   layout 0: both jobs, their code rings and one shared gold/LUT/LDPC-leaf set in SRAM0-3
//   layout 1: job B with its own rings, gold, LUT and LDPC leaf copy in SRAM4-7 (cap_buf)
//   layout 2: as layout 0, but both jobs run the LDPC leaf copy in SRAM4-7 (code and data split)
//   layout 3: as layout 0, but both jobs run the C6 mapper leaf in SRAM4-7 (requires bit32)
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

enum { RING = 2048, STAGES = 4, SUB = 6, MAX_PAIRS = 200 };

typedef struct {
    c3_job_t job;
    dvbs2_8psk_cfg_t cfg;
    uint32_t gold[1384], lut[DVBS2_8PSK_CODES_C6_WORDS], code[1792];
} lane_t;

typedef struct {
    lane_t a, b;            // a: job A and the shared tables; b: job B in layout 0
    uint8_t pattern[512];
} low_t;                    // SRAM0-3: ring_buf (free while no output engine runs)

typedef struct {
    lane_t b;
    uint32_t ring[2 * RING];
} high_t;                   // SRAM4-7: cap_buf

_Static_assert(sizeof(low_t) <= 4 * 24576, "low layout ends below the SRAM0-3 hammer");
_Static_assert(sizeof(high_t) <= CAP_WORDS_MAX * 4, "high layout fits cap_buf");
_Static_assert(4 * RING <= IN_WORDS, "two job rings fit in_buf");

static low_t *const low = (low_t *)ring_buf;
static high_t *const high = (high_t *)cap_buf;

// Core-to-core state and counters live in ring_buf above low_t: lower SRAM is full.
typedef struct {
    volatile uint32_t go, done, stop;
    c3_job_t *job;
    uint32_t stage[STAGES], sub[SUB], total, max;
} remote_t;
// LDPC sub-steps: 0 clear, 1 group rotate-XOR leaf, 2 row accumulation, 3 prefix,
// 4 apply prefix, 5 transpose/pack.
typedef struct { remote_t remote; uint32_t sub_a[SUB]; } shared_t;
#define shared ((shared_t *)(ring_buf + 30720))
#define remote (shared->remote)
#define sub_a (shared->sub_a)

static uint32_t run_job(c3_job_t *job, const uint8_t *pattern, uint32_t stage[STAGES], uint32_t sub[SUB]) {
    const uint32_t start = cycles();
    while (job->stage != C3_DONE) {
        const unsigned s = job->stage, k = job->ldpc.stage < SUB ? job->ldpc.stage : SUB - 1;
        const uint32_t t = cycles();
        c3_job_step(job, pattern);
        const uint32_t spent = cycles() - t;
        stage[s] += spent;
        if (s == C3_LDPC) sub[k] += spent;
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
        const uint32_t total = run_job(remote.job, low->pattern, remote.stage, remote.sub);
        remote.total += total;
        if (total > remote.max) remote.max = total;
        __dmb();
        remote.done = seen;
        __sev();
    }
}

// Hammers: each runs until remote.stop. The flash loop is deliberately left in XIP.
static void __attribute__((noinline)) hammer_flash(void) {
    uint32_t x = 1;
    while (!remote.stop)
        for (unsigned k = 0; k < 64; ++k) x = x * 1664525u + 1013904223u;
    remote.total = x;
}
static void __attribute__((noinline)) __not_in_flash_func(hammer_sram_code)(void) {
    uint32_t x = 1;
    while (!remote.stop)
        for (unsigned k = 0; k < 64; ++k) x = x * 1664525u + 1013904223u;
    remote.total = x;
}
static void hammer_copy(uint32_t *buf, uint32_t words) {
    while (!remote.stop) memcpy(buf + words, buf, words * 4u);
}
void dvbs2_transpose32(uint32_t *a); // dvbs2_asm.S
static void hammer_transpose(void) {
    // dvbs2_transpose32 is linked in SCRATCH_X: both LDPC workers fetch it from that one bank.
    uint32_t *tile = cap_buf + 8192;
    while (!remote.stop) dvbs2_transpose32(tile);
}
static void hammer_task(uint32_t kind) {
    if (kind == 7) hammer_transpose();
    else if (kind == 3) hammer_flash();
    else if (kind == 4) hammer_copy(ring_buf + 24576, 2048);            // SRAM0-3, outside low_t
    else if (kind == 5) hammer_copy(cap_buf, 2048);                     // SRAM4-7
    else hammer_sram_code();
}

static void begin(lane_t *lane, const lane_t *tables, uint32_t index, uint32_t *ring, uint32_t options) {
    c3_job_begin_options(&lane->job, &tables->cfg, index, ring, ring + RING, options);
    lane->job.stream.mask = RING - 1u;
    lane->job.ldpc_group = (options & C4_LDPC) ? (void *)((uintptr_t)tables->code | 1u) : NULL;
}

static bool tables(lane_t *lane, const dvbs2_code_t *code, uint32_t options) {
    return dvbs2_8psk_init(&lane->cfg, code, true, 0, lane->gold, 1384) &&
        (!(options & (C4_MAP | C6_MAP)) ||
         dvbs2_8psk_codes_c6_init(&lane->cfg, lane->lut, DVBS2_8PSK_CODES_C6_WORDS)) &&
        (!(options & C4_LDPC) || dvbs2_ldpc_copy(lane->code, sizeof lane->code));
}

void bench_c3_pair(int code_index, int pairs, int layout, int who, int options) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((code_index != S2_N2_3 && code_index != S2_N3_4 && code_index != S2_N5_6) || pairs < 1 ||
        pairs > MAX_PAIRS || layout < 0 || layout > 3 || who < 0 || who > 7 || (who >= 3 && layout == 1) ||
        (layout == 3 && !(options & C6_MAP)) ||
        options < 0 || options > C6_ALL) {
        printf("@{\"cmd\":\"c3pair\",\"error\":\"needs code5/6/8, pairs1..200, layout0..3 (3 needs bit32), who0..7 (3..7 excludes layout1), options0..63\"}\n");
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
    if (layout == 3) {
        // Change only the C6 instruction pointer. Gold, dilation/constants,
        // job data, LDPC code and rings retain layout0 addresses.
        memcpy(low->a.lut + 226, high->b.lut + 226, sizeof(uintptr_t));
    }
    lane_t *lane_b = layout == 1 ? &high->b : &low->b;
    const lane_t *tables_b = layout == 1 ? &high->b : &low->a;
    uint32_t *ring_a = in_buf, *ring_b = layout == 1 ? high->ring : in_buf + 2 * RING;
    // Layout 2 keeps every table and buffer in SRAM0-3 and only fetches the LDPC leaf from SRAM4-7.
    void *const leaf_hi = (options & C4_LDPC) ? (void *)((uintptr_t)high->b.code | 1u) : NULL;
    uint32_t stage_a[STAGES] = {0}, stage_b[STAGES] = {0}, total_a = 0, total_b = 0, max_a = 0, max_b = 0;
    remote = (remote_t){0};
    memset(sub_a, 0, sizeof sub_a);
    enable_cyccnt();
    const int hammer = who >= 3 ? who : 0;
    if (hammer) { tx_worker_start(hammer_task, (uint32_t)hammer); who = 0; }
    else if (who) tx_worker_start(core1_task, 0);
    for (int p = 0; p < pairs; ++p) {
        begin(&low->a, &low->a, 0, ring_a, (uint32_t)options);
        begin(lane_b, tables_b, 1, ring_b, (uint32_t)options);
        if (layout == 2) low->a.job.ldpc_group = lane_b->job.ldpc_group = leaf_hi;
        const uint32_t irq = save_and_disable_interrupts();
        if (who == 0) {
            uint32_t t = run_job(&low->a.job, low->pattern, stage_a, sub_a);
            total_a += t; if (t > max_a) max_a = t;
            t = run_job(&lane_b->job, low->pattern, stage_b, remote.sub);
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
            const uint32_t t = run_job(&low->a.job, low->pattern, stage_a, sub_a);
            total_a += t; if (t > max_a) max_a = t;
            while (remote.done != remote.go) __wfe();
            __dmb();
        }
        restore_interrupts(irq);
    }
    if (who || hammer) {
        remote.stop = 1;
        __dmb();
        __sev();
        tx_worker_wait();
    }
    if (who == 1) {
        // Core 1 ran both jobs into one accumulator; report it as job A+B.
        memcpy(stage_a, remote.stage, sizeof stage_a);
        memcpy(sub_a, remote.sub, sizeof sub_a);
        total_a = remote.total; max_a = remote.max;
    } else if (who == 2) {
        memcpy(stage_b, remote.stage, sizeof stage_b);
        total_b = remote.total; max_b = remote.max;
    }
    const uint32_t crc_a = pv_crc32(0, ring_a, 2 * RING * sizeof(uint32_t));
    const uint32_t crc_b = pv_crc32(0, ring_b, 2 * RING * sizeof(uint32_t));
    uintptr_t map_a = 0, map_b = 0;
    if (options & (C4_MAP | C6_MAP)) {
        const unsigned slot = options & C6_MAP ? 226 : 224;
        memcpy(&map_a, low->a.lut + slot, sizeof map_a);
        memcpy(&map_b, tables_b->lut + slot, sizeof map_b);
    }
    const unsigned runs_a = who == 1 ? 2u * (unsigned)pairs : (unsigned)pairs, runs_b = (unsigned)pairs;
    printf("@{\"cmd\":\"c3pair\",\"git\":\"%s\",\"clk_hz\":%lu,\"code_index\":%d,\"s2_code\":\"%s\",\"pairs\":%d,"
           "\"layout\":%d,\"who\":%d,\"hammer\":%d,\"options\":%d,\"ring_a\":%lu,\"ring_b\":%lu,\"job_b\":%lu,\"leaf_b\":%lu,"
           "\"map_lut_a\":%lu,\"map_lut_b\":%lu,\"map_leaf_a\":%lu,\"map_leaf_b\":%lu,"
           "\"crc_a\":%lu,\"crc_b\":%lu,\"jobs\":[",
           GIT_REV, (unsigned long)hz, code_index, code->name, pairs, layout, hammer ? hammer : who, hammer, options,
           (unsigned long)(uintptr_t)ring_a, (unsigned long)(uintptr_t)ring_b,
           (unsigned long)(uintptr_t)&lane_b->job, (unsigned long)(uintptr_t)tables_b->code,
           (unsigned long)(uintptr_t)low->a.lut, (unsigned long)(uintptr_t)tables_b->lut,
           (unsigned long)map_a, (unsigned long)map_b,
           (unsigned long)crc_a, (unsigned long)crc_b);
    for (unsigned j = 0; j < (who == 1 ? 1u : 2u); ++j) {
        const uint32_t *st = j ? stage_b : stage_a, *sb = j ? remote.sub : sub_a;
        const unsigned runs = j ? runs_b : runs_a;
        printf("%s{\"name\":\"%s\",\"core\":%d,\"runs\":%u,\"avg_cycles\":%lu,\"max_cycles\":%lu,"
               "\"avg_stage\":{\"gcs\":%lu,\"bch\":%lu,\"ldpc\":%lu,\"map\":%lu},"
               "\"ldpc_sub\":[%lu,%lu,%lu,%lu,%lu,%lu]}",
               j ? "," : "", who == 1 ? "A+B" : (j ? "B" : "A"),
               who == 1 ? 1 : (j && who == 2 ? 1 : 0), runs,
               (unsigned long)((j ? total_b : total_a) / runs), (unsigned long)(j ? max_b : max_a),
               (unsigned long)(st[0] / runs), (unsigned long)(st[1] / runs),
               (unsigned long)(st[2] / runs), (unsigned long)(st[3] / runs),
               (unsigned long)(sb[0] / runs), (unsigned long)(sb[1] / runs), (unsigned long)(sb[2] / runs),
               (unsigned long)(sb[3] / runs), (unsigned long)(sb[4] / runs), (unsigned long)(sb[5] / runs));
    }
    printf("]}\n");
    dvbs2_bch_set_kernel(saved_bch);
    dvbs2_frontend_set_bank(saved_bank);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
