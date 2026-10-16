#include "c3_fec.h"

#include <stdio.h>
#include <string.h>
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "tx.h"

enum { IDLE, RUN, READY, STAGES = 5, DELAY = 4, ERR_DEADLINE = 1, ERR_JOB = 2,
       ERR_SERVICE = 4, SERVICE_US = 32, MAX_SERVICE_US = 128 };
// CPU-fit measurements may relax the core-0 service-gap fault; output-path runs keep 128 us.
static uint32_t max_service_us = MAX_SERVICE_US;

typedef struct {
    c3_job_t job;
    volatile uint32_t status, job_error;
    uint32_t frames, max_frame_cycles, max_slice_cycles, frame_work, max_frame_us;
    uint32_t stage_max_cycles[STAGES];
    uint64_t stage_cycles[STAGES], busy_cycles, frame_start_us, ready_us, due_us;
    uint32_t delay_left;
} worker_t;

typedef struct {
    worker_t core[2];
    dvbs2_8psk_cfg_t cfg;
    uint32_t gold[1384];
    uint32_t map_lut[DVBS2_8PSK_CODES_C6_WORDS], ldpc_code[2048];
    uint8_t pattern[512];
    volatile uint32_t stop, errors;
    uint32_t target;
    volatile uint32_t published, groups;
    uint32_t consumed, carry[2], slow_us, hz, cpw, options;
    uint32_t late_frames, max_lateness_us, max_service_gap_us;
    uint64_t epoch, supervise_cycles, retire_due;
} context_t;

// Only the unused64KiB tail of the OUT8 table reservation is borrowed. No new
// static SRAM allocation, output setup, PIO, DMA or interrupt handler is added.
static context_t *context(void) { return (context_t *)(ring_buf + 16384); }
#define live (*context())
_Static_assert(sizeof(context_t) <= 65536, "C3 must fit the unused OUT8 table tail");
_Static_assert(IN_WORDS == 2 * C3_RING_GROUPS, "paired axis rings");

static uint64_t deadline(uint32_t frame) {
    // Round each absolute deadline upward once: no fractional-us drift.
    return live.epoch + ((uint64_t)(frame + 2u) * C3_FRAME_CYCLES * live.cpw * 1000000u + live.hz - 1u) / live.hz;
}

static void release(unsigned core, uint32_t index) {
    worker_t *w = &live.core[core];
    c3_job_begin_options(&w->job, &live.cfg, index, in_buf, in_buf + C3_RING_GROUPS, live.options);
    w->frame_work = 0;
    w->delay_left = live.slow_us;
    w->frame_start_us = time_us_64();
    if (live.options & C4_DEADLINES) w->due_us = deadline(index);
    __dmb();
    w->status = RUN;
    __sev();
}

static void work(unsigned core) {
    worker_t *w = &live.core[core];
    const uint32_t begin = cycles();
    unsigned stage = w->job.stage;
    bool ok = true;
    if (stage != C3_DONE) ok = c3_job_step(&w->job, live.pattern);
    else {
        stage = DELAY;
        const uint32_t take = w->delay_left > 16u ? 16u : w->delay_left;
        busy_wait_us_32(take);
        w->delay_left -= take;
    }
    const uint32_t spent = cycles() - begin;
    w->stage_cycles[stage] += spent;
    if (spent > w->stage_max_cycles[stage]) w->stage_max_cycles[stage] = spent;
    w->busy_cycles += spent;
    w->frame_work += spent;
    if (spent > w->max_slice_cycles) w->max_slice_cycles = spent;
    if (!ok) {
        // Fixed initialization makes this unreachable in an admitted run.
        w->job_error = ERR_JOB;
        __dmb();
        w->status = IDLE;
        return;
    }
    if (w->job.stage == C3_DONE && !w->delay_left) {
        ++w->frames;
        if (w->frame_work > w->max_frame_cycles) w->max_frame_cycles = w->frame_work;
        w->ready_us = time_us_64();
        const uint32_t elapsed = (uint32_t)(w->ready_us - w->frame_start_us);
        if (elapsed > w->max_frame_us) w->max_frame_us = elapsed;
        __dmb();
        w->status = READY;
    }
}

static void core1(uint32_t unused) {
    (void)unused;
    enable_cyccnt();
    while (!live.stop) {
        if (live.core[1].status == RUN) {
            const uint32_t begin = cycles();
            const uint64_t before = live.core[1].busy_cycles;
            __dmb();
            work(1);
            live.core[1].busy_cycles = before + (uint32_t)(cycles() - begin);
        }
        else __wfe();
    }
    __dmb();
}

static void publish(void) {
    while (live.published < live.target) {
        worker_t *w = &live.core[live.published & 1u];
        if (w->status != READY) break;
        __dmb();
        if (w->job.index != live.published) break;
        const uint32_t groups = c3_job_publish(&w->job, live.carry);
        const uint64_t now = time_us_64();
        const uint64_t due = live.options & C4_DEADLINES ? w->due_us : deadline(live.published);
        const uint32_t late = now > due ? (uint32_t)(now - due) : 0;
        if (late > live.max_lateness_us) live.max_lateness_us = late;
        if (late && !(live.errors & ERR_DEADLINE)) {
            ++live.late_frames;
            live.errors |= ERR_DEADLINE;
        }
        // The producer's later stores and merged boundary precede publication.
        __dmb();
        live.groups = groups;
        ++live.published;
        w->status = IDLE;
    }
}

void bench_c3_fec(int code_index, int ms, int slow_us, int cpw, int gap_us) {
    bench_c3_fec_options(code_index, ms, slow_us, cpw, gap_us, 0);
}

void bench_c3_fec_options(int code_index, int ms, int slow_us, int cpw, int gap_us, int options) {
    const uint32_t hz = clock_get_hz(clk_sys);
    max_service_us = (uint32_t)gap_us;
    if ((code_index != S2_N2_3 && code_index != S2_N3_4 && code_index != S2_N5_6) ||
        ms < 100 || ms > 60000 || slow_us < 0 || slow_us > 10000 ||
        options < 0 || options > C8_ALL || gap_us < 128 || gap_us > 4096 ||
        (cpw != 1 && cpw != 2 && cpw != 4) || (hz != 128000000u && hz != 256000000u)) {
        printf("@{\"cmd\":\"c3fec\",\"error\":\"needs normal2/3(5),3/4(6),5/6(8),100..60000ms,slow0..10000us,cpw1/2/4,SYS128/256,options0..%d\"}\n", C8_ALL);
        return;
    }
    const dvbs2_bch_kernel_t saved_bch = dvbs2_bch_get_kernel();
    const unsigned saved_bank = dvbs2_frontend_get_bank();
    memset(context(), 0, sizeof live);
    memset(in_buf, 0, sizeof in_buf);
    live.hz = hz;
    live.cpw = (uint32_t)cpw;
    live.options = (uint32_t)options;
    live.slow_us = (uint32_t)slow_us;
    live.target = (uint32_t)((uint64_t)ms * hz / ((uint64_t)C3_FRAME_CYCLES * cpw * 1000u));
    for (unsigned j = 0; j < sizeof live.pattern; ++j) live.pattern[j] = (uint8_t)(29u * j + 7u);
    dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);
    dvbs2_frontend_set_bank(0); // C3 control stays in bank0; option4 selects read-only scratchY per job
    dvbs2_init(&DVBS2_CODES[code_index], true);
    pv_init(65536);
    if (!dvbs2_8psk_init(&live.cfg, &DVBS2_CODES[code_index], true, 0, live.gold, 1384) ||
        ((options & (C4_MAP | C6_MAP)) &&
         !dvbs2_8psk_codes_c6_init(&live.cfg, live.map_lut, DVBS2_8PSK_CODES_C6_WORDS)) ||
        ((options & (C4_LDPC | C5_LDPC)) && !dvbs2_ldpc_prepare_mode(live.ldpc_code,
            sizeof live.ldpc_code, options & C5_LDPC ? 2 : 1))) {
        dvbs2_bch_set_kernel(saved_bch);
        dvbs2_frontend_set_bank(saved_bank);
        printf("@{\"cmd\":\"c3fec\",\"error\":\"C3/C4 setup failed\"}\n");
        return;
    }
    enable_cyccnt();
    live.epoch = time_us_64();
    if (live.options & C4_DEADLINES) live.retire_due = deadline(0);
    release(0, 0);
    release(1, 1);
    tx_worker_start(core1, 0);
    uint64_t previous_service = live.epoch, next_service = live.epoch;
    for (;;) {
        const uint32_t loop_begin = cycles();
        const uint64_t work_before = live.core[0].busy_cycles;
        if (live.core[0].status == RUN) work(0);
        const uint64_t now = time_us_64();
        if (now < next_service) {
            live.supervise_cycles += (uint32_t)(cycles() - loop_begin) -
                (uint32_t)(live.core[0].busy_cycles - work_before);
            if (live.core[0].status != RUN) busy_wait_us_32((uint32_t)(next_service - now));
            continue;
        }
        const uint32_t gap = (uint32_t)(now - previous_service);
        if (gap > live.max_service_gap_us) live.max_service_gap_us = gap;
        previous_service = now;
        next_service = now + SERVICE_US;
        live.errors |= live.core[0].job_error | live.core[1].job_error;
        if (gap > max_service_us) live.errors |= ERR_SERVICE;
        publish();
        // Each parity slot is released only after its preceding frame retires.
        // At most44388 unconsumed symbols own the65536-symbol ring, including
        // complete output written ahead of its in-order publication counter.
        if (!live.errors && live.consumed < live.target &&
            now >= (live.options & C4_DEADLINES ? live.retire_due : deadline(live.consumed))) {
            if (live.published <= live.consumed) {
                ++live.late_frames;
                live.errors |= ERR_DEADLINE;
            } else {
                const uint32_t index = live.consumed++ + 2u;
                if (live.options & C4_DEADLINES) live.retire_due = deadline(live.consumed);
                if (index < live.target) release(index & 1u, index);
            }
        }
        live.supervise_cycles += (uint32_t)(cycles() - loop_begin) -
            (uint32_t)(live.core[0].busy_cycles - work_before);
        if (live.errors) {
            // Finish active frames after failure so the final CRC never includes
            // speculative stores from an unpublished partial frame.
            if (live.core[0].status != RUN && live.core[1].status != RUN) { publish(); break; }
        } else if (live.consumed == live.target) break;
    }
    live.stop = 1;
    __dmb();
    __sev();
    tx_worker_wait();
    const uint64_t elapsed = time_us_64() - live.epoch;
    const uint32_t crc_i = pv_crc32(0, in_buf, C3_RING_GROUPS * sizeof(uint32_t));
    const uint32_t crc_q = pv_crc32(0, in_buf + C3_RING_GROUPS, C3_RING_GROUPS * sizeof(uint32_t));
    const double budget = (double)elapsed * hz / 1000000.0;
    printf("@{\"cmd\":\"c3fec\",\"git\":\"%s\",\"code_index\":%d,\"s2_code\":\"%s\","
           "\"clk_hz\":%lu,\"cpw\":%d,\"ms\":%d,\"slow_us\":%d,\"options\":%d,\"pilots\":true,\"gold\":0,\"source\":\"memory_gcs\",\"source_period_bytes\":256,"
           "\"frame_symbols\":%u,\"frame_cycles\":%u,\"ring_groups\":%u,\"frames_target\":%lu,"
           "\"published_frames\":%lu,\"consumed_frames\":%lu,\"published_groups\":%lu,\"pending_fill\":%lu,"
           "\"code_crc\":[%lu,%lu],\"errors\":%lu,\"late_frames\":%lu,\"max_lateness_us\":%lu,"
           "\"service_gap_limit_us\":%lu,\"max_service_gap_us\":%lu,\"elapsed_us\":%llu,\"supervise_cycles\":%llu,"
           "\"supervise_busy\":%.6f,\"context_bytes\":%u,\"core\":[",
           GIT_REV, code_index, DVBS2_CODES[code_index].name, (unsigned long)hz, cpw, ms, slow_us, options,
           C3_FRAME_SYMBOLS, C3_FRAME_CYCLES * cpw, C3_RING_GROUPS, (unsigned long)live.target,
           (unsigned long)live.published, (unsigned long)live.consumed, (unsigned long)live.groups,
           (unsigned long)(live.published * C3_FRAME_SYMBOLS & 15u), (unsigned long)crc_i, (unsigned long)crc_q,
           (unsigned long)live.errors, (unsigned long)live.late_frames, (unsigned long)live.max_lateness_us,
           (unsigned long)max_service_us, (unsigned long)live.max_service_gap_us, (unsigned long long)elapsed,
           (unsigned long long)live.supervise_cycles, live.supervise_cycles / budget, (unsigned)sizeof live);
    for (unsigned c = 0; c < 2; ++c) {
        const worker_t *w = &live.core[c];
        const uint64_t busy = w->busy_cycles + (c ? 0 : live.supervise_cycles);
        printf("%s{\"frames\":%lu,\"busy_cycles\":%llu,\"busy\":%.6f,\"max_frame_cycles\":%lu,"
               "\"max_frame_us\":%lu,\"max_slice_cycles\":%lu,\"stage_cycles\":{"
               "\"gcs\":%llu,\"bch\":%llu,\"ldpc\":%llu,\"map\":%llu,\"delay\":%llu},"
               "\"stage_max_cycles\":{\"gcs\":%lu,\"bch\":%lu,\"ldpc\":%lu,\"map\":%lu,\"delay\":%lu}}",
               c ? "," : "", (unsigned long)w->frames, (unsigned long long)busy,
               busy / budget, (unsigned long)w->max_frame_cycles,
               (unsigned long)w->max_frame_us, (unsigned long)w->max_slice_cycles,
               (unsigned long long)w->stage_cycles[0], (unsigned long long)w->stage_cycles[1],
               (unsigned long long)w->stage_cycles[2], (unsigned long long)w->stage_cycles[3],
               (unsigned long long)w->stage_cycles[4],
               (unsigned long)w->stage_max_cycles[0], (unsigned long)w->stage_max_cycles[1],
               (unsigned long)w->stage_max_cycles[2], (unsigned long)w->stage_max_cycles[3],
               (unsigned long)w->stage_max_cycles[4]);
    }
    printf("]}\n");
    dvbs2_bch_set_kernel(saved_bch);
    dvbs2_frontend_set_bank(saved_bank);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
