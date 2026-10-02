#include "bench.h"

#include <stdio.h>
#include <string.h>

#include "dvbs2.h"
#include "dvbs2_codes.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "pvproto.h"
#include "tx.h"

#include "git_rev.h"

// Kernel throughput over the whole PRBS input. The first pass computes the output CRC, outside
// the timing; the timed passes run with interrupts off.
void bench_kernel(const iq_kernel_info_t *k, int reps, bool tables_ok) {
    const uint32_t bw = BLOCK_WORDS, biw = BLOCK_IN(cfg.sps), nblk = IN_WORDS / biw;
    uint32_t crc = 0, worst = 0;
    uint64_t cyc = 0;                                // conv overflows 32 bits after 36 reps
    for (uint32_t b = 0; b < nblk; b++) {
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
    tx_json_head("bench", k);
    printf(",\"seed\":%lu,\"in_words\":%d,\"reps\":%d,\"tables_ok\":%d,\"crc\":%lu,"
           "\"cyc_per_sym\":%.4f,\"worst_block_cyc_per_sym\":%.4f,\"msym_s_one_core\":%.4f}\n",
           (unsigned long)SEED, IN_WORDS, reps, tables_ok, (unsigned long)crc, cps,
           (double)worst / BLOCK_SYMS(cfg.sps), clock_get_hz(clk_sys) / cps / 1e6);
}

// ------------------------------------------------------------------ DVB-S2 encoder stages

static uint32_t par_buf[BB_WORDS] SRAM_HI, bb_save[BB_WORDS] SRAM_HI;
static const dvbs2_code_t *c;
static int ci;

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

static void t_bch(void) { dvbs2_bch(bb_buf, par_buf); }
static void t_bch_serial(void) { dvbs2_bch_serial(bb_buf, par_buf); }
static void t_ldpc(void) { dvbs2_ldpc(bb_buf, par_buf); }
static void t_ldpc_serial(void) { dvbs2_ldpc_serial(bb_buf, par_buf); }
static void t_prbs(void) {                                     // input fill, subtracted
    uint32_t st = 1;
    prbs_fill(bb_buf, (c->kbch + 31) / 32, &st);
}
static void t_frame(void) {
    uint32_t st = 0x9E3779B9u ^ (uint32_t)ci;
    prbs_fill(bb_buf, (c->kbch + 31) / 32, &st);              // fresh input each run (not timed apart)
    symstream_t s = {.out = cap_buf, .mask = ~0u};
    dvbs2_frame(bb_buf, &s);
}

// One PLFRAME of the test BBFRAME into cap_buf (CRC'd against the host reference), then the
// minimum cycles of each stage over reps runs.
void bench_dvbs2(int code, bool pilots, int reps) {
    ci = code, c = &DVBS2_CODES[code];
    const uint32_t init_us = tx_code(code, pilots);
    uint32_t st = 0;
    symstream_t s = {.out = cap_buf, .mask = ~0u};
    dvbs2_frame(bb_buf, &s);
    symstream_flush(&s);
    const uint32_t crc = crc32_update(0, cap_buf, s.n * 4);
    memcpy(bb_save, bb_buf, sizeof bb_save);                   // timing runs clobber bb_buf
    for (uint32_t k = 0; k < BB_WORDS; k++) bb_buf[k] = st = st * 1664525u + 1013904223u;
    const uint32_t c_bch = time_min(t_bch, reps), c_bchs = time_min(t_bch_serial, 1);
    const uint32_t c_ldpc = time_min(t_ldpc, reps), c_ldpcs = time_min(t_ldpc_serial, 1);
    const uint32_t c_frame = time_min(t_frame, reps), c_prbs = time_min(t_prbs, reps);
    printf("@{\"cmd\":\"dvbs2\",\"git\":\"%s\",\"clk_hz\":%lu,\"code\":\"%s\",\"index\":%d,"
           "\"pilots\":%d,\"syms\":%lu,\"words\":%lu,\"crc\":%lu,\"init_us\":%lu,\"reps\":%d,"
           "\"cyc_bch\":%lu,\"cyc_bch_serial\":%lu,\"cyc_ldpc\":%lu,\"cyc_ldpc_serial\":%lu,"
           "\"cyc_frame\":%lu,\"prof\":{\"ldpc_groups\":%lu,\"ldpc_accum\":%lu,"
           "\"ldpc_transpose\":%lu,\"f_bb_bch\":%lu,\"f_ldpc\":%lu,\"f_map\":%lu}}\n",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), c->name, code, pilots,
           (unsigned long)dvbs2_plframe_symbols(), (unsigned long)s.n, (unsigned long)crc,
           (unsigned long)init_us, reps, (unsigned long)c_bch, (unsigned long)c_bchs,
           (unsigned long)c_ldpc, (unsigned long)c_ldpcs, (unsigned long)(c_frame - c_prbs),
           (unsigned long)(dvbs2_prof[1] - dvbs2_prof[0]), (unsigned long)(dvbs2_prof[2] - dvbs2_prof[1]),
           (unsigned long)(dvbs2_prof[3] - dvbs2_prof[2]), (unsigned long)(dvbs2_prof[5] - dvbs2_prof[4]),
           (unsigned long)(dvbs2_prof[6] - dvbs2_prof[5]), (unsigned long)(dvbs2_prof[7] - dvbs2_prof[6]));
    memcpy(bb_buf, bb_save, sizeof bb_save);
}

// ------------------------------------------------------------------ DMA sniffer modes

// CRC of pattern message 0 (1328 data bytes, and all 1332 incl. its CRC) for every mode /
// byte-swap / output-reverse / output-invert combination, seed 0xFFFFFFFF.
void bench_sniffer(void) {
    uint32_t *msgw = cap_buf, *dst = cap_buf + 512;
    pv_init(65536);
    pv_pattern_message((uint8_t *)msgw, 0, 65536);
    const int ch = dma_claim_unused_channel(true);
    printf("@{\"cmd\":\"snifftest\",\"sw_crc\":%lu,\"r\":[", (unsigned long)pv_crc32(0, msgw, 1328));
    for (int m = 0; m < 16; m++) {
        uint32_t out[2];
        for (int n = 0; n < 2; n++) {
            dma_channel_config d = dma_channel_get_default_config(ch);
            channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
            channel_config_set_sniff_enable(&d, true);
            dma_sniffer_enable(ch, m & 1 ? DMA_SNIFF_CTRL_CALC_VALUE_CRC32R : DMA_SNIFF_CTRL_CALC_VALUE_CRC32, true);
            dma_sniffer_set_byte_swap_enabled(m & 2);
            dma_sniffer_set_output_reverse_enabled(m & 4);
            dma_sniffer_set_output_invert_enabled(m & 8);
            dma_sniffer_set_data_accumulator(0xFFFFFFFFu);
            dma_channel_configure(ch, &d, dst, msgw, n ? 333 : 332, true);
            dma_channel_wait_for_finish_blocking(ch);
            out[n] = dma_sniffer_get_data_accumulator();
        }
        printf("%s[%d,%lu,%lu]", m ? "," : "", m, (unsigned long)out[0], (unsigned long)out[1]);
    }
    dma_sniffer_disable();
    dma_channel_unclaim(ch);
    printf("]}\n");
}
