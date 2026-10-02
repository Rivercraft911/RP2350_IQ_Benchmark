#include "tx.h"

#include <stdio.h>
#include <string.h>

#include "coeffs.h"
#include "dvbs2.h"
#include "dvbs2_codes.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "iqout.h"
#include "link.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"

#include "git_rev.h"                               // regenerated on every build

uint32_t ring_buf[N_BLOCKS * BLOCK_WORDS];                                 // 128 KiB
uint32_t in_buf[IN_WORDS] __attribute__((aligned(4 * IN_WORDS)));         // 32 KiB
uint32_t cap_buf[CAP_WORDS_MAX] SRAM_HI;                                   // 56 KiB
uint32_t bb_buf[BB_WORDS] SRAM_HI;
static uint32_t tab_i[TABLE_WORDS] SRAM_HI, tab_q[TABLE_WORDS] SRAM_HI;   // 32 KiB each

_Static_assert(PV_SLOTS * PV_MSG_WORDS <= IN_WORDS, "PV-SPI slots must fit in in_buf");
_Static_assert(PV_EMU_WORDS <= CAP_WORDS_MAX, "PV-SPI emulator must fit in cap_buf");

iq_cfg_t cfg = {.ti = tab_i, .tq = tab_q};
static const coef_set_t *cur_set;
static const dvbs2_code_t *s2c;

static void fill_prbs(void) {
    uint32_t st = SEED;
    prbs_fill(in_buf, IN_WORDS, &st);
}

// Core 1 runs tasks sent as (function, argument) through the SIO FIFO and replies when done.
static void core1_entry(void) {
    enable_cyccnt();
    for (;;) {
        void (*fn)(uint32_t) = (void (*)(uint32_t))multicore_fifo_pop_blocking();
        fn(multicore_fifo_pop_blocking());
        multicore_fifo_push_blocking(1);
    }
}

static void core1_run(void (*fn)(uint32_t), uint32_t arg) {
    multicore_fifo_push_blocking((uint32_t)fn);
    multicore_fifo_push_blocking(arg);
}

void tx_init(void) {
    enable_cyccnt();
    fill_prbs();
    multicore_launch_core1(core1_entry);
}

const char *tx_shaper(const char *kernel, int sps, int L, const iq_kernel_info_t **k,
                      bool *tables_ok) {
    const coef_set_t *cs = 0;
    for (int i = 0; i < N_COEF_SETS; i++)
        if (COEF_SETS[i].sps == sps && COEF_SETS[i].L == L) cs = &COEF_SETS[i];
    if (!cs || ((size_t)1 << L) * sps / 2 > TABLE_WORDS) return "no coefficient set";
    if (cs != cur_set) {
        cfg.sps = sps, cfg.L = L, cfg.coef = cs->c;
        iq_build_tables(&cfg);
        cur_set = cs;
    }
    const size_t tb = ((size_t)1 << L) * sps / 2 * 4;
    *tables_ok = crc32_update(0, tab_i, tb) == cs->crc_i && crc32_update(0, tab_q, tb) == cs->crc_q;
    *k = iq_find_kernel(kernel, sps, L);
    return *k ? NULL : "no such kernel";
}

// The test BBFRAME is the same input as host/native/dvbs2_test.c.
uint32_t tx_code(int code, bool pilots) {
    s2c = &DVBS2_CODES[code];
    const uint32_t t0 = time_us_32();
    dvbs2_init(s2c, pilots);
    uint32_t st = 0x9E3779B9u ^ (uint32_t)code, nw = (s2c->kbch + 31) / 32;
    memset(bb_buf, 0, sizeof bb_buf);
    prbs_fill(bb_buf, nw, &st);
    if (s2c->kbch % 32) bb_buf[nw - 1] &= ~(~0u >> (s2c->kbch % 32));
    return time_us_32() - t0;
}

void tx_json_head(const char *cmd, const iq_kernel_info_t *k) {
    printf("@{\"cmd\":\"%s\",\"git\":\"%s\",\"clk_hz\":%lu,\"kernel\":\"%s\",\"layout\":%d,"
           "\"sps\":%d,\"L\":%d",
           cmd, GIT_REV, (unsigned long)clock_get_hz(clk_sys), k->name, k->layout, cfg.sps, cfg.L);
}

// ------------------------------------------------------------------ shaping and output

typedef struct {
    uint64_t gen_cyc, t_start_us, t_end_us;        // 64-bit: DWT wraps every 33.6 s at 128 MHz
    uint32_t blocks, max_block_cyc, input_waits;
    int32_t min_lead;
} core_stats_t;

static double busy(uint64_t cyc, uint64_t t0_us, uint64_t t1_us) {
    return (double)cyc / ((double)(t1_us - t0_us) * 1e-6 * clock_get_hz(clk_sys));
}

static struct {
    const iq_kernel_info_t *k;
    const uint32_t *src;                           // input ring: in_buf or link_ring
    volatile uint32_t *avail;                      // words available in src (0 = always full)
    uint32_t biw, stride;                          // input words per block; producer stride
    uint64_t end_us, cap_at_us;                    // 64-bit: time_us_32 wraps every 71.6 min
    uint32_t cap_words, txstalls;
    volatile bool cap_started;
    core_stats_t st[2];
    bool c0, c1, tables_ok;                        // shaping cores; table CRCs match
    uint32_t link_words;                           // for the result
    uint64_t t_run_us;
    bool ready_output, ready_latch, ready_input;   // READY pin state at the end of a pvtx run
    uint ready_function;
} run;

// time_us_64() runs from flash; the loops below stay in SRAM, so read the 64-bit timer inline.
static inline __attribute__((always_inline)) uint64_t now_us(void) {
    timer_hw_t *t = PICO_DEFAULT_TIMER_INSTANCE();
    for (uint32_t hi = t->timerawh;;) {
        const uint32_t lo = t->timerawl, hi2 = t->timerawh;
        if (hi2 == hi) return (uint64_t)hi << 32 | lo;
        hi = hi2;
    }
}

static inline bool expired(void) { return now_us() >= run.end_us; }

// Input for block s is complete. Word counters wrap (after about 2.4 h at 8 Msym/s), so compare the
// signed distance, never the raw values.
static inline bool input_ready(uint32_t s) { return (int32_t)(*run.avail - (s + 1) * run.biw) >= 0; }

static void __not_in_flash_func(monitor)(void) {   // core 0 only
    if (iqout_take_txstall()) run.txstalls++;

    if (run.cap_words && !run.cap_started && now_us() >= run.cap_at_us) {
        iqout_capture_start(cap_buf, run.cap_words);
        run.cap_started = true;
    }
}

static void __not_in_flash_func(produce)(uint32_t s) {
    const uint32_t slot = s % N_BLOCKS, off = (s * run.biw) % IN_WORDS;
    run.k->fn(ring.buf + slot * ring.block_words, run.src + off, run.biw,
              run.src[(off + IN_WORDS - 1) % IN_WORDS], &cfg);
    __dmb();
    ring.ready[slot] = s + 1;
}

// Produce blocks first, first+stride, ... until end_us. Ring slot s%N is free once the DMA
// has finished block s-N, i.e. while s - done < N.
static void __not_in_flash_func(producer)(uint32_t first, uint32_t stride, bool mon) {
    core_stats_t *st = &run.st[get_core_num()];
    *st = (core_stats_t){.min_lead = 1 << 30, .t_start_us = time_us_64()};
    for (uint32_t s = first;; s += stride) {
        while (s - ring.done >= N_BLOCKS) {
            if (mon) monitor();
            if (expired()) goto out;
        }
        if (run.avail && !input_ready(s)) {
            st->input_waits++;
            while (!input_ready(s)) {
                if (mon) monitor();
                if (expired()) goto out;
            }
        }
        if (expired()) break;
        const uint32_t t0 = cycles();
        produce(s);
        const uint32_t dt = cycles() - t0;
        const int32_t lead = (int32_t)(s - ring.next_arm);   // blocks ahead of the DMA arm pointer
        st->gen_cyc += dt;
        st->blocks++;
        if (dt > st->max_block_cyc) st->max_block_cyc = dt;
        if (lead < st->min_lead) st->min_lead = lead;
        if (mon) monitor();
    }
out:
    st->t_end_us = time_us_64();
}

static void task_producer1(uint32_t first) { producer(first, run.stride, false); }

// ------------------------------------------------------------------ DVB-S2 source

// Core 1 encodes BBFRAMEs into link_ring: the test BBFRAME repeatedly (txs2), or frames built
// from the PV-SPI TS queue (pvtx). It starts a frame only when a whole frame fits without
// overwriting the history word of block `done`.
static struct {
    volatile uint32_t total;                       // words published to the shaper
    uint64_t busy_cyc, t0_us, t1_us;
    uint32_t frames, fw;
    int32_t min_ahead;                             // untransmitted words when a frame completes
    bool ts;                                       // pvtx: BBFRAMEs from the PV-SPI TS queue
    uint32_t bb_crc[4];                            // CRC-32 of the first four TS BBFRAMEs
    uint64_t bb_cyc;                               // cycles in TS BBFRAME building
    symstream_t s;
} enc;

// One PLFRAME. In TS mode the BBFRAME is built from queued packets (nulls when none), as bytes,
// then turned into the encoder's MSB-first words.
static void __not_in_flash_func(enc_frame)(void) {
    if (enc.ts) {
        const uint32_t t0 = cycles();
        uint8_t *b = (uint8_t *)bb_buf;
        const uint32_t nb = s2c->kbch / 8u, nw = (nb + 3) / 4;
        pv_bbframe(b, s2c->kbch, 0xF2, pvspi_next_packet);   // MATYPE-1: TS, SIS, CCM, RO 0.20
        for (uint32_t i = nb; i < 4 * nw; i++) b[i] = 0;
        if (enc.frames < 4) enc.bb_crc[enc.frames] = pv_crc32(0, b, nb);
        for (uint32_t i = 0; i < nw; i++) bb_buf[i] = __builtin_bswap32(bb_buf[i]);
        enc.bb_cyc += cycles() - t0;
    }
    dvbs2_frame(bb_buf, &enc.s);
}

static void __not_in_flash_func(task_encoder)(uint32_t unused) {
    (void)unused;
    enc.t0_us = time_us_64();
    while (!expired()) {
        const uint32_t d = ring.done * run.biw, consumed = d ? d - 1 : 0;
        if (enc.s.n + enc.fw + 1 - consumed > IN_WORDS) continue;
        const uint32_t t = cycles();
        enc_frame();
        enc.busy_cyc += cycles() - t;
        enc.frames++;
        __dmb();
        enc.total = enc.s.n;
        const int32_t ahead = (int32_t)(enc.s.n - ring.done * run.biw);
        if (ahead < enc.min_ahead) enc.min_ahead = ahead;
    }
    enc.t1_us = time_us_64();
}

// ------------------------------------------------------------------ one run

static void print_result(const tx_run_t *r);

const char *tx_run(const tx_run_t *r) {
    const iq_kernel_info_t *k;
    bool tables_ok;
    const char *e = tx_shaper(r->kernel, r->sps, r->L, &k, &tables_ok);
    if (e) return e;
    if (r->code >= 0) tx_code(r->code, r->pilots);
    const bool c0 = strchr(r->cores, '0'), c1 = strchr(r->cores, '1') && r->code < 0;
    run = (typeof(run)){.k = k, .src = r->lanes || r->code >= 0 ? link_ring : in_buf,
                        .avail = r->lanes ? &link_total : r->code >= 0 ? &enc.total : 0,
                        .biw = BLOCK_IN(cfg.sps), .cap_words = (uint32_t)r->cap_words,
                        .c0 = c0, .c1 = c1, .tables_ok = tables_ok};
    iqout_init(ring_buf, BLOCK_WORDS, N_BLOCKS, r->cpw, k->layout);
    const uint64_t tl = time_us_64();
    if (r->lanes) {                                       // fill enough input for the prefill
        link_init(r->lanes, r->half, in_buf);
        link_start();
        link_autopoll(&ring.done, run.biw);            // ring.done = 0 until the output starts
        while (link_total < (N_BLOCKS + 1) * run.biw && time_us_64() - tl < 100000)
            tight_loop_contents();
    }
    if (r->pv >= 0) {
        pv_init(r->pv ? PV_EMU_MSGS : 65536);
        pvspi_start(in_buf, r->pv == 1, cap_buf + CAP_WORDS_MAX - PV_EMU_WORDS, 3);
        // self-test: the first BBFRAME needs 28.6 packets (> 4 messages); queue 8 so no null
        // packet makes frame 0 differ from the reference
        while (r->pv == 1 && pvspi.head < 8 && time_us_64() - tl < 100000) tight_loop_contents();
    }
    if (r->code >= 0) {                                   // first frames on core 0, then core 1
        enc = (typeof(enc)){.s = {.out = link_ring, .mask = IN_WORDS - 1}, .min_ahead = 1 << 30,
                            .fw = (dvbs2_plframe_symbols() + 31) / 16, .ts = r->pv >= 0};
        while (enc.s.n < (N_BLOCKS + 1) * run.biw) {      // short frames: ~511 words each
            enc_frame();
            enc.frames++;
        }
        enc.total = enc.s.n;
    }
    for (uint32_t s = 0; s < N_BLOCKS; s++) produce(s);   // prefill
    const uint64_t t0 = time_us_64();
    run.end_us = t0 + (uint64_t)r->ms * 1000u;
    run.cap_at_us = t0 + (r->cap_ms >= 0 ? (uint64_t)r->cap_ms * 1000u : (uint64_t)r->ms * 500u);
    iqout_start();
    const uint32_t stride = (c0 && c1) ? 2 : 1;
    run.stride = stride;
    if (c1) core1_run(task_producer1, c0 ? N_BLOCKS + 1 : N_BLOCKS);
    if (r->code >= 0) core1_run(task_encoder, 0);
    if (c0) producer(N_BLOCKS, stride, true);
    else while (!expired()) monitor();
    // Stop the output as soon as production ends, before waiting for core 1 (whose last DVB-S2
    // frame can take ~3 ms): otherwise the drained ring is counted as underruns.
    while (run.cap_started && iqout_capture_busy()) tight_loop_contents();
    iqout_stop();
    if (c1 || r->code >= 0) multicore_fifo_pop_blocking();
    run.link_words = link_total, run.t_run_us = time_us_64() - tl;
    if (r->pv >= 0) {                     // sample READY before pvspi_stop() drives it low
        run.ready_output = gpio_get_dir(PIN_IN_READY) == GPIO_OUT;
        run.ready_latch = gpio_get_out_level(PIN_IN_READY);
        run.ready_input = gpio_get(PIN_IN_READY);
        run.ready_function = gpio_get_function(PIN_IN_READY);
    }
    if (r->lanes) link_stop();
    if (r->pv >= 0) {
        pvspi_stop();
        fill_prbs();                                      // PV-SPI used in_buf for its slots
    }
    print_result(r);
    return NULL;
}

static void print_result(const tx_run_t *r) {
    const double sym_rate = (double)clock_get_hz(clk_sys) / (2.0 * cfg.sps * r->cpw);
    const uint32_t period = (uint32_t)(2 * BLOCK_WORDS * r->cpw);         // cycles per block
    tx_json_head("stream", run.k);
    printf(",\"cores\":\"%s\",\"cpw\":%d,\"sym_rate\":%.1f,\"ms\":%d,\"tables_ok\":%d,"
           "\"block_syms\":%d,\"n_blocks\":%d,\"block_period_cyc\":%lu,\"blocks_out\":%lu,"
           "\"underruns\":%lu,\"own_errors\":%lu,\"txstalls\":%lu,\"lanes\":%d,\"half\":%d,"
           "\"link_words\":%lu,\"link_mbps\":%.3f,\"link_overruns\":%lu,\"core\":[",
           r->cores, r->cpw, sym_rate, r->ms, run.tables_ok, BLOCK_SYMS(cfg.sps), N_BLOCKS,
           (unsigned long)period, (unsigned long)ring.done, (unsigned long)ring.underruns,
           (unsigned long)ring.own_errors, (unsigned long)run.txstalls, r->lanes, r->half,
           (unsigned long)(r->lanes ? run.link_words : 0),
           r->lanes ? 32.0 * run.link_words / run.t_run_us : 0.0,
           (unsigned long)(r->lanes ? link_overruns : 0));
    bool first = true;
    for (int c = 0; c < 2; c++) {
        if (!(c ? run.c1 : run.c0)) continue;
        const core_stats_t *st = &run.st[c];
        printf("%s{\"id\":%d,\"blocks\":%lu,\"busy\":%.4f,\"max_block_cyc\":%lu,\"min_lead\":%ld,"
               "\"input_waits\":%lu}",
               first ? "" : ",", c, (unsigned long)st->blocks,
               busy(st->gen_cyc, st->t_start_us, st->t_end_us),
               (unsigned long)st->max_block_cyc, (long)st->min_lead, (unsigned long)st->input_waits);
        first = false;
    }
    printf("]");
    if (r->code >= 0)
        printf(",\"s2_code\":\"%s\",\"s2_index\":%d,\"s2_frames\":%lu,\"s2_busy\":%.4f,"
               "\"s2_min_ahead_words\":%ld", s2c->name, r->code, (unsigned long)enc.frames,
               busy(enc.busy_cyc, enc.t0_us, enc.t1_us), (long)enc.min_ahead);
    if (r->pv >= 0)
        printf(",\"pv\":{\"selftest\":%d,\"msgs_ok\":%lu,\"nop\":%lu,\"bad_hdr\":%lu,\"bad_crc\":%lu,"
               "\"bad_sync\":%lu,\"lost\":%lu,\"short\":%lu,\"long\":%lu,\"overflows\":%lu,"
               "\"ts_packets\":%lu,\"null_packets\":%lu,\"bbframes\":%lu,\"crc_chain\":%lu,"
               "\"bb_crc\":[%lu,%lu,%lu,%lu],\"bb_cyc_per_frame\":%lu,\"first_bad_at\":%ld,"
               "\"first_bad_info\":%lu,\"ready_pin\":%u,\"ready_function\":%u,"
               "\"ready_output\":%u,\"ready_latch\":%u,\"ready_input\":%u}", r->pv,
               (unsigned long)pv_stats.ok, (unsigned long)pv_stats.nop, (unsigned long)pv_stats.bad_hdr,
               (unsigned long)pv_stats.bad_crc, (unsigned long)pv_stats.bad_sync,
               (unsigned long)pv_stats.lost, (unsigned long)pvspi.short_msgs,
               (unsigned long)pvspi.long_msgs, (unsigned long)pvspi.overflows,
               (unsigned long)pv_stats.ts_packets, (unsigned long)pv_stats.null_packets,
               (unsigned long)pv_stats.bbframes, (unsigned long)pv_stats.crc_chain,
               (unsigned long)enc.bb_crc[0], (unsigned long)enc.bb_crc[1],
               (unsigned long)enc.bb_crc[2], (unsigned long)enc.bb_crc[3],
               (unsigned long)(enc.bb_cyc / (enc.frames ? enc.frames : 1)),
               (long)pv_stats.first_bad_at, (unsigned long)pv_stats.first_bad_info,
               PIN_IN_READY, run.ready_function, run.ready_output, run.ready_latch,
               run.ready_input);
    // No words if the capture never started (cap_ms past the end of the run).
    const uint32_t skip = IQOUT_CAP_SKIP;
    const uint32_t n = run.cap_started && r->cap_words > (int)skip ? (uint32_t)r->cap_words - skip : 0;
    printf(",\"cap_words\":%lu,\"cap_crc\":%lu}\n", (unsigned long)n,
           (unsigned long)crc32_update(0, cap_buf + skip, n * 4));
    for (uint32_t i = 0; i < n; i += 16) {                 // raw capture for host alignment
        printf("@cap %lu", (unsigned long)i);
        for (uint32_t j = i; j < i + 16 && j < n; j++) printf(" %08lx", (unsigned long)cap_buf[skip + j]);
        printf("\n");
    }
    if (n) printf("@capend\n");
}
