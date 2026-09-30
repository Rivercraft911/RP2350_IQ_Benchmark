// RP2350 I/Q waveform benchmark. Line commands over USB CDC; results as JSON lines prefixed '@'.
//   info
//   clock <khz>                                   set clk_sys (stock max 150000)
//   bench <kernel> <sps> <L> [reps]               kernel throughput + output CRC
//   stream <kernel> <sps> <L> <cores> <cpw> <ms> [cap_words] [lanes half]
//        cores: 0 | 1 | 01 (alternate blocks); cpw: PIO system clocks per 16-bit bus word
//        lanes > 0: input arrives over the PIO link (emulated host), half = SCK half period
//   dvbs2 <code> <pilots> [reps]                  DVB-S2 encoder stage cycles + PLFRAME CRC
//   txs2 <code> <pilots> <kernel> <sps> <L> <cpw> <ms> [cap_words]
//        full transmitter: core 1 encodes DVB-S2 frames, core 0 shapes and streams
//        code: index into DVBS2_CODES (dvbs2_codes.h)
//   bootsel                                       reboot to the USB bootloader
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "coeffs.h"
#include "dvbs2_codes.h"
#include "config.h"
#include "hardware/clocks.h"
#include "hardware/structs/m33.h"
#include "hardware/sync.h"
#include "iqgen.h"
#include "iqout.h"
#include "link.h"
#include "pico/bootrom.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"

#include "git_rev.h"                               // regenerated on every build

#define SEED 0x1234ABCDu

static uint32_t ring_buf[N_BLOCKS * BLOCK_WORDS];            // 128 KiB
static uint32_t in_buf[IN_WORDS] __attribute__((aligned(4 * IN_WORDS)));   // 32 KiB PRBS
static uint32_t tab_i[TABLE_WORDS] SRAM_HI, tab_q[TABLE_WORDS] SRAM_HI;   // 32 KiB each
static uint32_t cap_buf[CAP_WORDS_MAX] SRAM_HI;                          // 64 KiB

static iq_cfg_t cfg = {.ti = tab_i, .tq = tab_q};
static const coef_set_t *cur_set;

static inline uint32_t cycles(void) { return m33_hw->dwt_cyccnt; }

static void enable_cyccnt(void) {
    m33_hw->demcr |= M33_DEMCR_TRCENA_BITS;
    m33_hw->dwt_ctrl |= M33_DWT_CTRL_CYCCNTENA_BITS;
}

// Select coefficients and rebuild tables; returns false if the variant is unknown.
static bool select_variant(int sps, int L, bool *tables_ok) {
    const coef_set_t *cs = 0;
    for (int i = 0; i < N_COEF_SETS; i++)
        if (COEF_SETS[i].sps == sps && COEF_SETS[i].L == L) cs = &COEF_SETS[i];
    if (!cs || ((size_t)1 << L) * sps / 2 > TABLE_WORDS) return false;
    if (cs != cur_set) {
        cfg.sps = sps, cfg.L = L, cfg.coef = cs->c;
        iq_build_tables(&cfg);
        cur_set = cs;
    }
    const size_t tb = ((size_t)1 << L) * sps / 2 * 4;
    *tables_ok = crc32_update(0, tab_i, tb) == cs->crc_i && crc32_update(0, tab_q, tb) == cs->crc_q;
    return true;
}

static void json_head(const char *cmd, const iq_kernel_info_t *k) {
    printf("@{\"cmd\":\"%s\",\"git\":\"%s\",\"clk_hz\":%lu,\"kernel\":\"%s\",\"layout\":%d,"
           "\"sps\":%d,\"L\":%d",
           cmd, GIT_REV, (unsigned long)clock_get_hz(clk_sys), k->name, k->layout, cfg.sps, cfg.L);
}

// ------------------------------------------------------------------ stage 2: kernel benchmark

static void cmd_bench(const iq_kernel_info_t *k, int reps, bool tables_ok) {
    const uint32_t bw = BLOCK_WORDS, biw = BLOCK_IN(cfg.sps), nblk = IN_WORDS / biw;
    uint32_t crc = 0, cyc = 0, worst = 0;
    for (uint32_t b = 0; b < nblk; b++) {   // correctness pass: whole input, CRC outside timing
        uint32_t *o = ring_buf + (b % N_BLOCKS) * bw;
        k->fn(o, in_buf + b * biw, biw, b ? in_buf[b * biw - 1] : 0, &cfg);
        crc = crc32_update(crc, o, bw * 4);
    }
    const uint32_t save = save_and_disable_interrupts();
    for (int r = 0; r < reps; r++)
        for (uint32_t b = 0; b < nblk; b++) {
            const uint32_t t0 = cycles();
            k->fn(ring_buf + (b % N_BLOCKS) * bw, in_buf + b * biw, biw,
                  in_buf[(b * biw + IN_WORDS - 1) % IN_WORDS], &cfg);
            const uint32_t dt = cycles() - t0;
            cyc += dt;
            if (dt > worst) worst = dt;
        }
    restore_interrupts(save);
    const double syms = (double)reps * IN_WORDS * 16, cps = cyc / syms;
    json_head("bench", k);
    printf(",\"seed\":%lu,\"in_words\":%d,\"reps\":%d,\"tables_ok\":%d,\"crc\":%lu,"
           "\"cyc_per_sym\":%.4f,\"worst_block_cyc_per_sym\":%.4f,\"msym_s_one_core\":%.4f}\n",
           (unsigned long)SEED, IN_WORDS, reps, tables_ok, (unsigned long)crc, cps,
           (double)worst / BLOCK_SYMS(cfg.sps), clock_get_hz(clk_sys) / cps / 1e6);
}

// ------------------------------------------------------------------ DVB-S2 encoder benchmark

static uint32_t bb_buf[64800 / 32 + 2] SRAM_HI, par_buf[64800 / 32 + 2] SRAM_HI;

static uint32_t time_min(void (*f)(void), int reps) {
    uint32_t best = ~0u;
    for (int r = 0; r < reps; r++) {
        const uint32_t save = save_and_disable_interrupts(), t0 = cycles();
        f();
        const uint32_t dt = cycles() - t0;
        restore_interrupts(save);
        if (dt < best) best = dt;
    }
    return best;
}

static const dvbs2_code_t *s2c;
static void t_bch(void) { dvbs2_bch(bb_buf, par_buf); }
static void t_bch_serial(void) { dvbs2_bch_serial(bb_buf, par_buf); }
static void t_ldpc(void) { dvbs2_ldpc(bb_buf, par_buf); }
static void t_ldpc_serial(void) { dvbs2_ldpc_serial(bb_buf, par_buf); }
static void t_prbs(void) {                                     // input fill, subtracted
    uint32_t st = 1;
    prbs_fill(bb_buf, (s2c->kbch + 31) / 32, &st);
}
static void t_frame(void) {
    uint32_t st = 0x9E3779B9u ^ (uint32_t)(s2c - DVBS2_CODES);
    prbs_fill(bb_buf, (s2c->kbch + 31) / 32, &st);           // fresh input each run (not timed apart)
    symstream_t s = {.out = cap_buf, .mask = ~0u};
    dvbs2_frame(bb_buf, &s);
}

// Configure a code and load its test BBFRAME (same input as host/native/dvbs2_test.c).
static uint32_t s2_setup(int ci, int pil) {
    s2c = &DVBS2_CODES[ci];
    const uint32_t t0 = time_us_32();
    dvbs2_init(s2c, pil);
    uint32_t st = 0x9E3779B9u ^ (uint32_t)ci, nw = (s2c->kbch + 31) / 32;
    memset(bb_buf, 0, sizeof bb_buf);
    prbs_fill(bb_buf, nw, &st);
    if (s2c->kbch % 32) bb_buf[nw - 1] &= ~(~0u >> (s2c->kbch % 32));
    return time_us_32() - t0;
}

static void cmd_dvbs2(int ci, int pil, int reps) {
    const uint32_t init_us = s2_setup(ci, pil);
    uint32_t st = 0;
    symstream_t s = {.out = cap_buf, .mask = ~0u};
    dvbs2_frame(bb_buf, &s);
    symstream_flush(&s);
    const uint32_t crc = crc32_update(0, cap_buf, s.n * 4);
    static uint32_t bb_save[64800 / 32 + 2] SRAM_HI;               // timing runs clobber bb_buf
    memcpy(bb_save, bb_buf, sizeof bb_save);
    for (uint32_t k = 0; k < 64800 / 32 + 2; k++) bb_buf[k] = st = st * 1664525u + 1013904223u;
    const uint32_t c_bch = time_min(t_bch, reps), c_bchs = time_min(t_bch_serial, 1);
    const uint32_t c_ldpc = time_min(t_ldpc, reps), c_ldpcs = time_min(t_ldpc_serial, 1);
    const uint32_t c_frame = time_min(t_frame, reps), c_prbs = time_min(t_prbs, reps);
    printf("@{\"cmd\":\"dvbs2\",\"git\":\"%s\",\"clk_hz\":%lu,\"code\":\"%s\",\"index\":%d,"
           "\"pilots\":%d,\"syms\":%lu,\"words\":%lu,\"crc\":%lu,\"init_us\":%lu,\"reps\":%d,"
           "\"cyc_bch\":%lu,\"cyc_bch_serial\":%lu,\"cyc_ldpc\":%lu,\"cyc_ldpc_serial\":%lu,"
           "\"cyc_frame\":%lu,\"prof\":{\"ldpc_groups\":%lu,\"ldpc_accum\":%lu,"
           "\"ldpc_transpose\":%lu,\"f_bb_bch\":%lu,\"f_ldpc\":%lu,\"f_map\":%lu}}\n",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), s2c->name, ci, pil,
           (unsigned long)dvbs2_plframe_symbols(), (unsigned long)s.n, (unsigned long)crc,
           (unsigned long)init_us, reps, (unsigned long)c_bch, (unsigned long)c_bchs,
           (unsigned long)c_ldpc, (unsigned long)c_ldpcs, (unsigned long)(c_frame - c_prbs),
           (unsigned long)(dvbs2_prof[1] - dvbs2_prof[0]), (unsigned long)(dvbs2_prof[2] - dvbs2_prof[1]),
           (unsigned long)(dvbs2_prof[3] - dvbs2_prof[2]), (unsigned long)(dvbs2_prof[5] - dvbs2_prof[4]),
           (unsigned long)(dvbs2_prof[6] - dvbs2_prof[5]), (unsigned long)(dvbs2_prof[7] - dvbs2_prof[6]));
    memcpy(bb_buf, bb_save, sizeof bb_save);
}

// ------------------------------------------------------------------ stage 3/5: streaming

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
    int lanes;
    uint32_t biw, stride;                          // input words per block; producer stride
    uint32_t end_us, cap_at_us, cap_words, txstalls;
    volatile bool cap_started;
    core_stats_t st[2];
} run;

static inline bool expired(void) { return (int32_t)(time_us_32() - run.end_us) >= 0; }

static void __not_in_flash_func(monitor)(void) {   // core 0 only
    if (iqout_take_txstall()) run.txstalls++;

    if (run.cap_words && !run.cap_started && time_us_32() >= run.cap_at_us) {
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
        if (run.avail && *run.avail < (s + 1) * run.biw) {
            st->input_waits++;
            while (*run.avail < (s + 1) * run.biw) {
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

static void task_producer1(uint32_t first) { producer(first, run.stride, false); }

// DVB-S2 source (txs2): core 1 encodes the test BBFRAME repeatedly into link_ring. It starts a
// frame only when a whole frame fits without overwriting the history word of block `done`.
static struct {
    volatile uint32_t total;                       // words published to the shaper
    uint64_t busy_cyc, t0_us, t1_us;
    uint32_t frames, fw;
    int32_t min_ahead;                             // untransmitted words when a frame completes
    symstream_t s;
} enc;

static void __not_in_flash_func(task_encoder)(uint32_t unused) {
    (void)unused;
    enc.t0_us = time_us_64();
    while (!expired()) {
        const uint32_t d = ring.done * run.biw, consumed = d ? d - 1 : 0;
        if (enc.s.n + enc.fw + 1 - consumed > IN_WORDS) continue;
        const uint32_t t = cycles();
        dvbs2_frame(bb_buf, &enc.s);
        enc.busy_cyc += cycles() - t;
        enc.frames++;
        __dmb();
        enc.total = enc.s.n;
        const int32_t ahead = (int32_t)(enc.s.n - ring.done * run.biw);
        if (ahead < enc.min_ahead) enc.min_ahead = ahead;
    }
    enc.t1_us = time_us_64();
}

static void cmd_stream(const iq_kernel_info_t *k, const char *cores, int cpw, int ms, int cap,
                       int lanes, int half, int s2, bool tables_ok) {
    const bool c0 = strchr(cores, '0'), c1 = strchr(cores, '1') && s2 < 0;
    run = (typeof(run)){.k = k, .src = lanes || s2 >= 0 ? link_ring : in_buf,
                        .avail = lanes ? &link_total : s2 >= 0 ? &enc.total : 0, .lanes = lanes,
                        .biw = BLOCK_IN(cfg.sps), .cap_words = (uint32_t)cap};
    iqout_init(ring_buf, BLOCK_WORDS, N_BLOCKS, cpw, k->layout);
    const uint32_t tl = time_us_32();
    if (lanes) {                                          // fill enough input for the prefill
        link_init(lanes, half, in_buf);
        link_start();
        link_autopoll(&ring.done, run.biw);            // ring.done = 0 until the output starts
        while (link_total < (N_BLOCKS + 1) * run.biw && time_us_32() - tl < 100000)
            tight_loop_contents();
    }
    if (s2 >= 0) {                                        // first frame on core 0, then core 1
        enc = (typeof(enc)){.s = {.out = link_ring, .mask = IN_WORDS - 1}, .min_ahead = 1 << 30,
                            .fw = (dvbs2_plframe_symbols() + 31) / 16};
        while (enc.s.n < (N_BLOCKS + 1) * run.biw) {      // short frames: ~511 words each
            dvbs2_frame(bb_buf, &enc.s);
            enc.frames++;
        }
        enc.total = enc.s.n;
    }
    for (uint32_t s = 0; s < N_BLOCKS; s++) produce(s);   // prefill
    const uint32_t t0 = time_us_32();
    run.end_us = t0 + (uint32_t)ms * 1000u;
    run.cap_at_us = t0 + (uint32_t)ms * 500u;              // capture mid-run
    iqout_start();
    const uint32_t stride = (c0 && c1) ? 2 : 1;
    run.stride = stride;
    if (c1) core1_run(task_producer1, c0 ? N_BLOCKS + 1 : N_BLOCKS);
    if (s2 >= 0) core1_run(task_encoder, 0);
    if (c0) producer(N_BLOCKS, stride, true);
    else while (!expired()) monitor();
    // Stop the output as soon as production ends, before waiting for core 1 (whose last DVB-S2
    // frame can take ~3 ms): otherwise the drained ring is counted as underruns.
    while (run.cap_started && iqout_capture_busy()) tight_loop_contents();
    iqout_stop();
    if (c1 || s2 >= 0) multicore_fifo_pop_blocking();
    const uint32_t link_words = link_total, t_run = time_us_32() - tl;
    if (lanes) link_stop();

    const double sym_rate = (double)clock_get_hz(clk_sys) / (2.0 * cfg.sps * cpw);
    const uint32_t period = (uint32_t)(2 * BLOCK_WORDS * cpw);            // cycles per block
    json_head("stream", k);
    printf(",\"cores\":\"%s\",\"cpw\":%d,\"sym_rate\":%.1f,\"ms\":%d,\"tables_ok\":%d,"
           "\"block_syms\":%d,\"n_blocks\":%d,\"block_period_cyc\":%lu,\"blocks_out\":%lu,"
           "\"underruns\":%lu,\"own_errors\":%lu,\"txstalls\":%lu,\"lanes\":%d,\"half\":%d,"
           "\"link_words\":%lu,\"link_mbps\":%.3f,\"link_overruns\":%lu,\"core\":[",
           cores, cpw, sym_rate, ms, tables_ok, BLOCK_SYMS(cfg.sps), N_BLOCKS, (unsigned long)period,
           (unsigned long)ring.done, (unsigned long)ring.underruns,
           (unsigned long)ring.own_errors, (unsigned long)run.txstalls, lanes, half,
           (unsigned long)(lanes ? link_words : 0), lanes ? 32.0 * link_words / t_run : 0.0,
           (unsigned long)(lanes ? link_overruns : 0));
    bool first = true;
    for (int c = 0; c < 2; c++) {
        if (!(c ? c1 : c0)) continue;
        const core_stats_t *st = &run.st[c];
        printf("%s{\"id\":%d,\"blocks\":%lu,\"busy\":%.4f,\"max_block_cyc\":%lu,\"min_lead\":%ld,"
               "\"input_waits\":%lu}",
               first ? "" : ",", c, (unsigned long)st->blocks,
               busy(st->gen_cyc, st->t_start_us, st->t_end_us),
               (unsigned long)st->max_block_cyc, (long)st->min_lead, (unsigned long)st->input_waits);
        first = false;
    }
    printf("]");
    if (s2 >= 0)
        printf(",\"s2_code\":\"%s\",\"s2_index\":%d,\"s2_frames\":%lu,\"s2_busy\":%.4f,"
               "\"s2_min_ahead_words\":%ld", s2c->name, s2, (unsigned long)enc.frames,
               busy(enc.busy_cyc, enc.t0_us, enc.t1_us), (long)enc.min_ahead);
    const uint32_t skip = 8, n = cap > (int)skip ? (uint32_t)cap - skip : 0;
    printf(",\"cap_words\":%lu,\"cap_crc\":%lu}\n", (unsigned long)n,
           (unsigned long)crc32_update(0, cap_buf + skip, n * 4));
    for (uint32_t i = 0; i < n; i += 16) {                 // raw capture for host alignment
        printf("@cap %lu", (unsigned long)i);
        for (uint32_t j = i; j < i + 16 && j < n; j++) printf(" %08lx", (unsigned long)cap_buf[skip + j]);
        printf("\n");
    }
    if (n) printf("@capend\n");
}

// ------------------------------------------------------------------ command loop

static void cmd_info(void) {
    printf("@{\"cmd\":\"info\",\"git\":\"%s\",\"built\":\"%s %s\",\"clk_hz\":%lu,\"board\":\"%s\","
           "\"pin_d0\":%d,\"pin_clkio\":%d,\"block_words\":%d,\"n_blocks\":%d,\"in_words\":%d,"
           "\"seed\":%lu,\"kernels\":[",
           GIT_REV, __DATE__, __TIME__, (unsigned long)clock_get_hz(clk_sys), PICO_BOARD,
           PIN_D0, PIN_CLKIO, BLOCK_WORDS, N_BLOCKS, IN_WORDS, (unsigned long)SEED);
    for (int i = 0; i < IQ_N_KERNELS; i++)
        printf("%s\"%s:%d:%d\"", i ? "," : "", IQ_KERNELS[i].name, IQ_KERNELS[i].sps, IQ_KERNELS[i].L);
    printf("]}\n");
}

static void error(const char *msg) { printf("@{\"error\":\"%s\"}\n", msg); }

static void dispatch(char *line) {
    char *argv[10];
    int argc = 0;
    for (char *t = strtok(line, " \t\r"); t && argc < 10; t = strtok(0, " \t\r")) argv[argc++] = t;
    if (!argc) return;
    const char *cmd = argv[0];
    if (!strcmp(cmd, "info")) return cmd_info();
    if (!strcmp(cmd, "bootsel")) { reset_usb_boot(0, 0); }
    if (!strcmp(cmd, "txs2") && argc >= 8) {           // txs2 code pilots kernel sps L cpw ms [cap]
        const int ci = atoi(argv[1]), sps = atoi(argv[4]), L = atoi(argv[5]);
        const int cpw = atoi(argv[6]), ms = atoi(argv[7]), cap = argc > 8 ? atoi(argv[8]) : 0;
        bool tables_ok;
        if (ci < 0 || ci >= N_DVBS2_CODES) return error("no such code");
        if (!select_variant(sps, L, &tables_ok)) return error("no coefficient set");
        const iq_kernel_info_t *k = iq_find_kernel(argv[3], sps, L);
        if (!k) return error("no such kernel");
        if (cpw < 2 || cpw > IQOUT_MAX_CPW || ms <= 0 || cap < 0 || cap > CAP_WORDS_MAX)
            return error("bad args");
        s2_setup(ci, atoi(argv[2]) != 0);
        return cmd_stream(k, "0", cpw, ms, cap, 0, 0, ci, tables_ok);
    }
    if (!strcmp(cmd, "dvbs2") && argc >= 3) {
        const int ci = atoi(argv[1]);
        if (ci < 0 || ci >= N_DVBS2_CODES) return error("no such code");
        return cmd_dvbs2(ci, atoi(argv[2]) != 0, argc > 3 ? atoi(argv[3]) : 4);
    }
    if (!strcmp(cmd, "clock") && argc == 2) {
        const uint32_t khz = (uint32_t)atoi(argv[1]);
        if (khz > 150000 || !set_sys_clock_khz(khz, false)) return error("clock not reachable");
        return cmd_info();
    }
    const bool bench = !strcmp(cmd, "bench"), stream = !strcmp(cmd, "stream");
    if (!(bench && argc >= 4) && !(stream && argc >= 7)) return error("bad command");
    const int sps = atoi(argv[2]), L = atoi(argv[3]);
    bool tables_ok;
    if (!select_variant(sps, L, &tables_ok)) return error("no coefficient set");
    const iq_kernel_info_t *k = iq_find_kernel(argv[1], sps, L);
    if (!k) return error("no such kernel");
    if (bench) return cmd_bench(k, argc > 4 ? atoi(argv[4]) : 4, tables_ok);
    const int cpw = atoi(argv[5]), ms = atoi(argv[6]), cap = argc > 7 ? atoi(argv[7]) : 0;
    const int lanes = argc > 8 ? atoi(argv[8]) : 0, half = argc > 9 ? atoi(argv[9]) : 6;
    if (cpw < 2 || cpw > IQOUT_MAX_CPW || ms <= 0 || cap < 0 || cap > CAP_WORDS_MAX)
        return error("bad args");
    if (lanes && ((lanes != 1 && lanes != 2 && lanes != 4) || half < 2 || half > 16))
        return error("bad link args");
    cmd_stream(k, argv[4], cpw, ms, cap, lanes, half, -1, tables_ok);
}

int main(void) {
    set_sys_clock_khz(DEFAULT_SYS_KHZ, true);
    stdio_init_all();
    enable_cyccnt();
    uint32_t st = SEED;
    prbs_fill(in_buf, IN_WORDS, &st);
    multicore_launch_core1(core1_entry);
    char line[128];
    size_t n = 0;
    for (;;) {
        const int c = getchar_timeout_us(100000);
        if (c == PICO_ERROR_TIMEOUT) continue;
        if (c == '\n' || c == '\r') {
            line[n] = 0;
            if (n) dispatch(line);
            n = 0;
        } else if (n < sizeof line - 1) {
            line[n++] = (char)c;
        }
    }
}
