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
//   layout 3: as layout 0, but both jobs run the C6 mapper leaf in SRAM4-7 (requires bit64)
//   layout 4: both jobs and shared LUT/LDPC in SRAM4-7; max BCH table and Gold in ring_buf
// Shared read-only tables (BCH, BB PRBS, LDPC addresses) stay where the linker put them.
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "c3_fec.h"
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    RING = 2048, STAGES = 4, SUB = 6, MAX_PAIRS = 200,
    GOLD_WORDS = 1384, LDPC_CODE_WORDS = 2432, PLACE_WORDS = 256,
    WIDE_TABLE_BYTES = 120 * 1024,
    WIDE_GOLD_OFFSET = WIDE_TABLE_BYTES,
    SHARED_OFFSET = WIDE_GOLD_OFFSET + 4 * GOLD_WORDS,
    SHARED_RESERVE = 128,
    WIDE_PLACE_OFFSET = SHARED_OFFSET + SHARED_RESERVE,
    WIDE_END = WIDE_PLACE_OFFSET + 4 * PLACE_WORDS,
    TRANSPOSE_TILE_WORD = 4096
};

typedef struct {
    c3_job_t job;
    dvbs2_8psk_cfg_t cfg;
    uint32_t gold[GOLD_WORDS], lut[DVBS2_8PSK_CODES_C6_WORDS], code[LDPC_CODE_WORDS];
    uint32_t place_code[PLACE_WORDS]; // BCH512 + transpose512, reserved for all masks
} lane_t;

typedef struct {
    lane_t a, b;            // a: job A and the shared tables; b: job B in layout 0
    uint8_t pattern[512];
} low_t;                    // SRAM0-3: ring_buf (free while no output engine runs)

typedef struct {
    lane_t b;
    uint32_t ring[2 * RING];
} high_t;                   // SRAM4-7: cap_buf

// Layout4 has no embedded Gold arena: it follows the maximum table in ring_buf.
// The C5/C6 maximum arenas remain reserved for the off and on controls alike.
typedef struct {
    c3_job_t a;
    dvbs2_8psk_cfg_t cfg;
    uint32_t lut[DVBS2_8PSK_CODES_C6_WORDS], code[LDPC_CODE_WORDS];
    uint32_t place_code[PLACE_WORDS];
    c3_job_t b;
    uint8_t pattern[512];
} wide_t;
_Static_assert(sizeof(wide_t) <= sizeof cap_buf, "wide layout fits cap_buf");
_Static_assert(24576 * 4 + 24 * 1024 <= WIDE_GOLD_OFFSET, "compact table avoids shared state");
_Static_assert(sizeof(low_t) <= 4 * 24576, "low layout ends below the SRAM0-3 hammer");
_Static_assert(sizeof(high_t) <= sizeof cap_buf, "high layout fits cap_buf");
_Static_assert(4 * RING <= IN_WORDS, "two job rings fit in_buf");
_Static_assert(WIDE_GOLD_OFFSET >= WIDE_TABLE_BYTES, "Gold follows the maximum BCH table");
_Static_assert(SHARED_OFFSET >= WIDE_GOLD_OFFSET + 4 * GOLD_WORDS, "shared state follows Gold");
_Static_assert(WIDE_PLACE_OFFSET >= SHARED_OFFSET + SHARED_RESERVE, "private code follows shared state");
_Static_assert(WIDE_END <= sizeof ring_buf, "table, Gold, shared state and code fit ring_buf");
_Static_assert(SHARED_OFFSET % _Alignof(uint64_t) == 0, "shared state is naturally aligned");
// Hammers never use layout1/4. All active high-bank arenas follow its unused job.
_Static_assert((TRANSPOSE_TILE_WORD + 32) * 4 <= offsetof(lane_t, cfg),
               "transpose hammer stays inside the unused high job");
_Static_assert(4096 * 4 <= offsetof(lane_t, cfg), "copy hammer avoids active high code");

static low_t *const low = (low_t *)ring_buf;
static high_t *const high = (high_t *)cap_buf;

// Core-to-core state and counters live above layout4's external Gold arena.
typedef struct {
    volatile uint32_t go, done, stop;
    c3_job_t *job;
    const uint8_t *pattern;
    uint32_t stage[STAGES], sub[SUB], total, max;
} remote_t;
// LDPC sub-steps: 0 clear, 1 group rotate-XOR leaf, 2 row accumulation, 3 prefix,
// 4 apply prefix, 5 transpose/pack.
typedef struct { remote_t remote; uint32_t sub_a[SUB]; } shared_t;
_Static_assert(sizeof(shared_t) <= SHARED_RESERVE, "shared counters fit their reservation");
_Static_assert(SHARED_OFFSET % _Alignof(shared_t) == 0, "shared counters are aligned");
#define shared ((shared_t *)((uint8_t *)ring_buf + SHARED_OFFSET))
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
        const uint32_t total = run_job(remote.job, remote.pattern, remote.stage, remote.sub);
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
    // Layout0/2/3 never runs the high job. Its storage is below every active
    // C5/C6/C7 arena, unlike the old +8192 tile which overlapped C5 instructions.
    uint32_t *tile = cap_buf + TRANSPOSE_TILE_WORD;
    while (!remote.stop) dvbs2_transpose32(tile);
}
static void hammer_task(uint32_t kind) {
    if (kind == 7) hammer_transpose();
    else if (kind == 3) hammer_flash();
    else if (kind == 4) hammer_copy(ring_buf + 24576, 2048);            // SRAM0-3, outside low_t
    else if (kind == 5) hammer_copy(cap_buf, 2048);                     // SRAM4-7
    else hammer_sram_code();
}

static void begin(c3_job_t *job, const dvbs2_8psk_cfg_t *cfg, const uint32_t *code,
                  uint32_t index, uint32_t *ring, uint32_t options) {
    c3_job_begin_options(job, cfg, index, ring, ring + RING, options);
    job->stream.mask = RING - 1u;
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    job->ldpc_group = (options & (C4_LDPC | C5_LDPC | C9_LDPC_PACK)) ? (void *)((uintptr_t)code | 1u) : NULL;
#else
    (void)code;
#endif
}

static bool tables(dvbs2_8psk_cfg_t *cfg, uint32_t *gold, uint32_t *lut, uint32_t *ldpc_code,
                   const dvbs2_code_t *code, uint32_t options) {
    return dvbs2_8psk_init(cfg, code, true, 0, gold, GOLD_WORDS) &&
        (!(options & (C4_MAP | C6_MAP)) ||
         dvbs2_8psk_codes_c6_init(cfg, lut, DVBS2_8PSK_CODES_C6_WORDS)) &&
        (!(options & (C4_LDPC | C5_LDPC | C9_LDPC_PACK)) || dvbs2_ldpc_copy_mode(ldpc_code,
            4 * LDPC_CODE_WORDS, options & C9_LDPC_PACK ? 3 : options & C5_LDPC ? 2 : 1));
}

void bench_c3_pair(int code_index, int pairs, int layout, int who, int options) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((code_index != S2_N2_3 && code_index != S2_N3_4 && code_index != S2_N5_6 &&
         code_index != S2_N8_9 && code_index != S2_N9_10) || pairs < 1 ||
        pairs > MAX_PAIRS || layout < 0 || layout > 4 || who < 0 || who > 7 ||
        (who >= 3 && (layout == 1 || layout == 4)) ||
        (layout == 3 && !(options & C6_MAP)) || options < 0 || options > C9_ALL ||
        ((options & C7_TABLE3) && layout != 4) ||
        ((options & C7_TABLE3) && (options & C7_BCH_LDM)) ||
        ((options & C9_BCH_TABLE4) && ((options & (C7_TABLE3 | C7_BCH_LDM)) || who == 4))) {
        printf("@{\"cmd\":\"c3pair\",\"error\":\"needs code5/6/8/9/10, pairs1..200, layout0..4 (3 needs bit64), "
               "who0..7 (hammers exclude layout1/4), options0..%d; table3=1024 needs layout4 and no LDM=256; table4=2048 excludes1024/256; best=%d needs layout4\"}\n",
               C9_ALL, C8_BEST);
        return;
    }
    const dvbs2_code_t *code = &DVBS2_CODES[code_index];
    const dvbs2_bch_kernel_t saved_bch = dvbs2_bch_get_kernel();
    const unsigned saved_bank = dvbs2_frontend_get_bank();
    memset(low, 0, sizeof *low);
    memset(high, 0, sizeof *high);
    memset(in_buf, 0, sizeof in_buf);
    wide_t *const wide = (wide_t *)cap_buf;
    if (layout == 4) memset(wide, 0, sizeof *wide);
    c3_job_t *const job_a = layout == 4 ? &wide->a : &low->a.job;
    c3_job_t *const job_b = layout == 4 ? &wide->b : layout == 1 ? &high->b.job : &low->b.job;
    dvbs2_8psk_cfg_t *const cfg_a = layout == 4 ? &wide->cfg : &low->a.cfg;
    const dvbs2_8psk_cfg_t *const cfg_b = layout == 1 ? &high->b.cfg : cfg_a;
    uint32_t *const gold_a = layout == 4 ? ring_buf + WIDE_GOLD_OFFSET / 4 : low->a.gold;
    uint32_t *const lut_a = layout == 4 ? wide->lut : low->a.lut;
    const uint32_t *const lut_b = layout == 1 ? high->b.lut : lut_a;
    uint32_t *const code_a = layout == 4 ? wide->code : low->a.code;
    const uint32_t *const code_b = layout == 1 ? high->b.code : code_a;
    uint8_t *const pattern = layout == 4 ? wide->pattern : low->pattern;
    for (unsigned j = 0; j < 512; ++j) pattern[j] = (uint8_t)(29u * j + 7u);
    dvbs2_bch_set_kernel(code->t == 8 ? S2_BCH_T8_ARM : S2_BCH_STREAM_ARM);
    dvbs2_frontend_set_bank(0);
    dvbs2_init(code, true);
    pv_init(65536);
    // Every SRAM LDPC job needs a prepared default; each lane also gets its own copy.
    // Layout4 holds Gold outside cap_buf, above the full table even for its off control.
    bool ok = tables(cfg_a, gold_a, lut_a, code_a, code, (uint32_t)options) &&
        (layout == 4 || tables(&high->b.cfg, high->b.gold, high->b.lut, high->b.code,
                              code, (uint32_t)options)) &&
        (!(options & (C4_LDPC | C5_LDPC | C9_LDPC_PACK)) || dvbs2_ldpc_prepare_mode(code_a,
            4 * LDPC_CODE_WORDS, options & C9_LDPC_PACK ? 3 : options & C5_LDPC ? 2 : 1));
    const unsigned variant = options & C9_BCH_TABLE4 ? 3u : options & C7_TABLE3 ? 2u : !!(options & C7_BCH_LDM);
    uint32_t *const arena_a = layout == 4 ? ring_buf + WIDE_PLACE_OFFSET / 4 : low->a.place_code;
    uint32_t *const arena_b = layout == 4 ? wide->place_code : high->b.place_code;
    dvbs2_bch_leaf_t leaves[2] = {NULL, NULL};
    dvbs2_transpose_fn transposes[2] = {NULL, NULL};
    uint32_t *arenas[2] = {arena_a, arena_b};
    for (unsigned k = 0; k < 2; ++k) {
        if (options & (C7_BCH_PLACE | C7_TABLE3 | C9_BCH_TABLE4)) {
            leaves[k] = dvbs2_bch_copy(arenas[k], 512, variant);
            ok = ok && leaves[k];
        }
        if (options & C7_TRANSPOSE) {
            transposes[k] = dvbs2_transpose_copy(arenas[k] + 128, 512);
            ok = ok && transposes[k];
        }
    }
    // Fixed reserve outside low_t, below layout4 Gold/shared state.
    uint32_t *const compact_table = ring_buf + 24576;
    if (options & C9_BCH_TABLE4) ok = ok && dvbs2_bch_table4_init(compact_table,
        24 * 1024);
    if (options & C7_TABLE3) ok = ok && dvbs2_bch_table3_init(ring_buf, WIDE_TABLE_BYTES);
    if (!ok) {
        dvbs2_bch_set_kernel(saved_bch);
        dvbs2_frontend_set_bank(saved_bank);
        printf("@{\"cmd\":\"c3pair\",\"error\":\"C3/C7 setup failed\"}\n");
        return;
    }
    if (layout == 3) {
        // Change only the C6 instruction pointer. Gold, dilation/constants,
        // job data, LDPC code and rings retain layout0 addresses.
        memcpy(lut_a + 226, high->b.lut + 226, sizeof(uintptr_t));
    }
    uint32_t *ring_a = in_buf, *ring_b = layout == 1 ? high->ring : in_buf + 2 * RING;
    // Layout2 changes only the LDPC leaf; C7 placement stays per worker.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    void *const leaf_hi = (options & (C4_LDPC | C5_LDPC | C9_LDPC_PACK)) ? (void *)((uintptr_t)high->b.code | 1u) : NULL;
#else
    void *const leaf_hi = NULL;
#endif
    uint32_t stage_a[STAGES] = {0}, stage_b[STAGES] = {0}, total_a = 0, total_b = 0, max_a = 0, max_b = 0;
    remote = (remote_t){.pattern = pattern};
    memset(sub_a, 0, sizeof sub_a);
    enable_cyccnt();
    const int hammer = who >= 3 ? who : 0;
    if (hammer) { tx_worker_start(hammer_task, (uint32_t)hammer); who = 0; }
    else if (who) tx_worker_start(core1_task, 0);
    for (int p = 0; p < pairs; ++p) {
        begin(job_a, cfg_a, code_a, 0, ring_a, (uint32_t)options);
        begin(job_b, cfg_b, code_b, 1, ring_b, (uint32_t)options);
        job_a->bch_leaf = leaves[0]; job_b->bch_leaf = leaves[1];
        // C7's callback is used by modes0/1; C5 mode2's pack bundle includes
        // its own private transpose, so bit512 is redundant on that ARM path.
        job_a->transpose = transposes[0]; job_b->transpose = transposes[1];
        if (options & C9_BCH_TABLE4) job_a->bch_table = job_b->bch_table = compact_table;
        if (options & C7_TABLE3) job_a->bch_table = job_b->bch_table = ring_buf;
        if (layout == 2) job_a->ldpc_group = job_b->ldpc_group = leaf_hi;
        const uint32_t irq = save_and_disable_interrupts();
        if (who == 0) {
            uint32_t t = run_job(job_a, pattern, stage_a, sub_a);
            total_a += t; if (t > max_a) max_a = t;
            t = run_job(job_b, pattern, stage_b, remote.sub);
            total_b += t; if (t > max_b) max_b = t;
        } else if (who == 1) {
            for (unsigned k = 0; k < 2; ++k) {
                remote.job = k ? job_b : job_a;
                __dmb();
                ++remote.go;
                __sev();
                while (remote.done != remote.go) __wfe();
                __dmb();
            }
        } else {
            remote.job = job_b;
            __dmb();
            ++remote.go;
            __sev();
            const uint32_t t = run_job(job_a, pattern, stage_a, sub_a);
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
        memcpy(&map_a, lut_a + slot, sizeof map_a);
        memcpy(&map_b, lut_b + slot, sizeof map_b);
    }
    const unsigned runs_a = who == 1 ? 2u * (unsigned)pairs : (unsigned)pairs, runs_b = (unsigned)pairs;
    printf("@{\"cmd\":\"c3pair\",\"git\":\"%s\",\"clk_hz\":%lu,\"code_index\":%d,\"s2_code\":\"%s\",\"pairs\":%d,"
           "\"layout\":%d,\"who\":%d,\"hammer\":%d,\"options\":%d,\"ring_a\":%lu,\"ring_b\":%lu,\"job_b\":%lu,\"leaf_b\":%lu,"
           "\"map_lut_a\":%lu,\"map_lut_b\":%lu,\"map_leaf_a\":%lu,\"map_leaf_b\":%lu,"
           "\"crc_a\":%lu,\"crc_b\":%lu,\"jobs\":[",
           GIT_REV, (unsigned long)hz, code_index, code->name, pairs, layout, hammer ? hammer : who, hammer, options,
           (unsigned long)(uintptr_t)ring_a, (unsigned long)(uintptr_t)ring_b,
           (unsigned long)(uintptr_t)job_b, (unsigned long)(uintptr_t)code_b,
           (unsigned long)(uintptr_t)lut_a, (unsigned long)(uintptr_t)lut_b,
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
    printf("],\"bch_leaf\":[%lu,%lu],\"transpose_leaf\":[%lu,%lu],\"table3\":%lu,\"table3_bytes\":%lu,"
           "\"table4\":%lu,\"table4_bytes\":%lu,\"gold_a\":%lu,\"gold_b\":%lu,\"shared_state\":%lu,\"shared_bytes\":%lu,"
           "\"place_a\":%lu,\"place_b\":%lu,\"wide_bytes\":%lu}\n",
           (unsigned long)(uintptr_t)leaves[0], (unsigned long)(uintptr_t)leaves[1],
           (unsigned long)(uintptr_t)transposes[0], (unsigned long)(uintptr_t)transposes[1],
           (unsigned long)((options & C7_TABLE3) ? (uintptr_t)ring_buf : 0),
           (unsigned long)((options & C7_TABLE3) ? dvbs2_bch_table3_bytes() : 0),
           (unsigned long)((options & C9_BCH_TABLE4) ? (uintptr_t)compact_table : 0),
           (unsigned long)((options & C9_BCH_TABLE4) ? dvbs2_bch_table4_bytes() : 0),
           (unsigned long)(uintptr_t)cfg_a->gold, (unsigned long)(uintptr_t)cfg_b->gold,
           (unsigned long)(uintptr_t)shared, (unsigned long)sizeof *shared,
           (unsigned long)(uintptr_t)arena_a, (unsigned long)(uintptr_t)arena_b,
           (unsigned long)(layout == 4 ? sizeof *wide : 0));
    dvbs2_bch_set_kernel(saved_bch);
    dvbs2_frontend_set_bank(saved_bank);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
