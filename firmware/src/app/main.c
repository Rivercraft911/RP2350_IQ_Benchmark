// RP2350 I/Q waveform benchmark. Line commands over USB CDC; results as JSON lines prefixed '@',
// errors as @{"error":"..."}. [exp] = only with -DIQ4HIST_EXPERIMENT=ON. See README.md for the
// best measured option per transmitter type and results/README.md for the evidence.
//
// System
//   info / stackcheck / bootsel                   identity / stack high-water and code integrity / bootloader
//   clock <khz>                                   set clk_sys (stock max 150000)
//   start / status / stop                         pvflight only: autonomous transmitter control
//
// QPSK (hardware shaping is the best measured path; CPU paths remain as controls)
//   dualptr cpw ms source fault [code] [pvkernel]   live SPI/FEC, PIO-generated LUT addresses (source 5/6);
//        pvkernel 1/2 (TS only): fused CRC-8/word TS adapter, Arm leaf in SRAM / C leaf in flash;
//        3: as 1, with core 0 building the BBFRAMEs in bounded steps (core 1 LDPC/map only)
//   dualptr4 cpw ms source fault [code] [pvkernel]  same with four router-fed writers (A2)
//   dualstream cpw ms kernel source cap fault [schedule] [div256] [row_symbols] [code]
//        CPU-generated descriptors; source: 0=cyclic, 1=PRBS, 2=core1 FEC, 3=one-core FEC,
//        4=split FEC, 5=SPI TS, 6=SPI GCS
//   pvtx <cpw> <ms> <selftest> [cap_words] [cap_ms] [single_core]
//        PigeonVision TX (the pvflight profile): TS over PV-SPI v1 (docs/pv-spi-spec.md) ->
//        DVB-S2 normal QPSK 2/3 with pilots -> CPU shaper (N = 4, L = 12)
//   txs2 <code> <pilots> <kernel> <sps> <L> <cpw> <ms> [cap_words] [single_core]
//        original CPU-shaped transmitter: core 1 encodes, core 0 shapes and streams
//   stream <kernel> <sps> <L> <cores> <cpw> <ms> [cap_words] [lanes half]   CPU shaper output only
//   benchquadptr mode / quadlive cpw ms fault     finite / cyclic PIO address generation
//
// 8PSK on the four-level table [exp]
//   bench4fec cpw ms invert [prio] [bits]        live 8PSK FEC -> hardware shaping; bits 8 = B7 transmitter
//   bench4prod8 / bench4prod8live cpw ms invert [prio] [starve_ms]   four PIO producers (B6, 16 Msym/s)
//   bench4quad8 / bench4quad cpw ms invert [prio] four SRAM-fed chains, cyclic (B5)
//   bench5quad8 cpw ms invert [prio]              N5 cyclic 8PSK, cpw1/2 = 12.8/6.4 Msym/s
//   bench4live / bench4live8 cpw ms invert [prio]   one chain per axis, cyclic (B1 / B4)
//
// CPU-shaped 8PSK, VCM and APSK
//   bench8live ms / benchvcm ms                   complete 5/6 8PSK / alternating QPSK+8PSK
//   bench8psk [reps] / bench8quad ms reps / bench8map code pilots reps   8PSK components
//   benchapskfec mod cpw ms [split] [options]   APSK with live SPI and FEC; split1 = BCH on core 0;
//                                                options 1 fast PL mapper, 2 fast 32APSK offsets, 4 mapper in flash
//   benchapsklive cpw ms schedule mapper mod placement    runtime APSK, 1/2 cores
//   bench16out / bench32out cpw ms kernel        16APSK / 32APSK output with capture, no FEC
//   bench16planar cpw ms                         experimental dual-axis planar output
//   bench16chunk / bench16native8 / bench32native8 / bench32map reps   APSK components
//
// FEC and encoder
//   dvbs2 <code> <pilots> [reps]                  encoder stage cycles + PLFRAME CRC
//   c3fec code ms [slow_us] [cpw] [gap_us] [options]   [exp] two-core frame-parallel 8PSK FEC, no output
//   b9 cpw ms invert prio slow_us options           [exp] complete GCS 8PSK2/3, options0/251
//   c3pair code pairs layout who options          [exp] FEC SRAM-bank contention, per-stage cycles
//        options=best: c3fec=251; c3pair=1275 requires layout4 (large BCH table)
//   bchkernel <0|1|2|3> / bchbank <0|1> / ldpcpack <0|1> / mapkernel <0|1>   encoder selectors
//
// Output settings (idle only)
//   dualwriters <2|4> / dualprio <0..15>          writer count / BUS_PRIORITY mask (PROC0 1, PROC1 2, DMA_R 4, DMA_W 8)
//   dualclock <0|1|2>                             0 HSTX CLK_IO, 1 GP28 PIO DDR clock, 2 inverted control
//   quadprofile <0|1>                             L9 / L8 QPSK pulse
//
// Diagnostics
//   dmaloop chains words sink load_div load_kind prio hammer   [exp] DMA row-loop latency
//   r2grant batches cpw [gap_us] [fault] [prio]  [exp] self-re-arming DMA input grants, pin-free echo
//   bench <kernel> <sps> <L> [reps]               shaping kernel throughput + output CRC
//   bench32bus mode group                        no-output bus contention diagnosis
//   pvstress ms irq_mask_us / pvbench [reps] [kernel] / ptrbench [reps]   SPI admission / packet prep / PIO pointers
//   pvwide ms width half fault [emulate]        [exp] PV messages on 1/4-bit source-clocked bus
//   snifftest / ddrtest [ms] / dualbench [reps]   DMA sniffer / PIO data + HSTX clock / descriptor generation
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bench.h"
#include "bench8live.h"
#include "bench16out.h"
#include "bench16planar.h"
#include "bench32out.h"
#include "bench_apsk_live.h"
#include "bench_apsk_fec.h"
#include "bench32map.h"
#include "bench32bus.h"
#include "bench_quadlive.h"
#include "bench_quadptr.h"
#ifdef IQ4HIST_EXPERIMENT
#include "bench4live.h"
#include "bench4fec.h"
#include "bench4quad.h"
#include "bench5quad.h"
#include "bench4prod.h"
#include "bench_b9.h"
#include "dmaloop.h"
#include "c3_fec.h"
#include "r2grant.h"
#include "pvwide.h"
#endif
#include "bench_ptr.h"
#include "dual_dma.h"
#include "dvbs2.h"
#include "dvbs2_codes.h"
#include "hardware/clocks.h"
#include "iqout.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"
#include "tx.h"
#ifdef PV_AUTONOMOUS
#include "tusb.h"
#include "hardware/sync.h"
#endif

#include "git_rev.h"

// The PigeonVision downlink: TS from the CM5, 8 Msym/s. pvtx overrides cpw, ms, pv and capture.
static const tx_run_t PVTX = {.kernel = "lut_asm_p", .sps = 4, .L = 12, .cores = "0", .cpw = 2,
                              .cap_ms = -1, .code = S2_N2_3, .pilots = 1, .pv = 0};

#ifdef PV_AUTONOMOUS
// Only stop/status are serviced during transmission. Bounded input work; no USB wait.
static void flight_service(void) {
    static char line[16];
    static unsigned n;
    static bool overflow;
    static uint64_t next_status;
    static char reply[512];
    static unsigned sent, length;
    // IRQ background USB task is the only other USB user during autonomous TX.
    // Copy only bytes that fit; retain the rest for later service calls.
    if (sent < length) {
        uint32_t irq = save_and_disable_interrupts();
        unsigned available = tud_cdc_write_available();
        unsigned remaining = length - sent;
        if (available > remaining) available = remaining;
        if (available) sent += tud_cdc_write(reply + sent, available);
        // Start short packets too; a busy endpoint returns immediately.
        tud_cdc_write_flush();
        restore_interrupts(irq);
    }
    for (unsigned i = 0; i < 16; ++i) {
        int c = getchar_timeout_us(0);
        if (c == PICO_ERROR_TIMEOUT) break;
        if (c == '\n' || c == '\r') {
            line[n] = 0;
            if (!overflow && !strcmp(line, "stop")) {
                sent = length;  // discard an unsent snapshot tail; final result starts a fresh line
                tx_request_stop();
            } else if (!overflow && !strcmp(line, "status") && time_us_64() >= next_status) {
                next_status = time_us_64() + 1000000;
                if (sent == length) {
                    int count = tx_status_json(reply, sizeof reply);
                    length = count > 0 && count < (int)sizeof reply ? (unsigned)count : 0;
                    sent = 0;
                }
            }
            n = 0;
            overflow = false;
        } else if (n < sizeof line - 1) line[n++] = (char)c;
        else overflow = true;
    }
}

static const char *flight_start(void) {
    tx_run_t r = PVTX;
    r.ms = 0;
    r.service = flight_service;
    return tx_run(&r);
}
#endif

static char err[64];

#define REQUIRED INT_MIN

// Parse argument s (NULL if absent) as an integer in [lo, hi]. An absent argument takes def,
// unless def is REQUIRED. On failure err holds the message.
static bool num(const char *s, const char *name, int def, int lo, int hi, int *out) {
    char *end = NULL;
    long v = def;
    if (s) v = strtol(s, &end, 10);
    if (s ? (end == s || *end || v < lo || v > hi) : def == REQUIRED) {
        snprintf(err, sizeof err, "%s: expected %d..%d", name, lo, hi);
        return false;
    }
    *out = (int)v;
    return true;
}

#ifdef IQ4HIST_EXPERIMENT
// Numeric masks keep their full meaning. The convenience spelling uses the best
// supported candidate in this command: c3pair needs layout4, c3fec has no TABLE3.
static bool c3_options(const char *s, int def, int best, int *out) {
    if (s && !strcmp(s, "best")) { *out = best; return true; }
    return num(s, "options", def, 0, C8_ALL, out);
}
#endif

static bool cores_ok(const char *s) {
    if (s && (!strcmp(s, "0") || !strcmp(s, "1") || !strcmp(s, "01"))) return true;
    snprintf(err, sizeof err, "cores: expected 0, 1 or 01");
    return false;
}

// The first IQOUT_CAP_SKIP captured words are discarded, so a shorter capture would be empty.
static bool cap_ok(int cap_words) {
    if (cap_words == 0 || cap_words > IQOUT_CAP_SKIP) return true;
    snprintf(err, sizeof err, "cap_words: expected 0 or more than %d", IQOUT_CAP_SKIP);
    return false;
}

static void cmd_info(void) {
    printf("@{\"cmd\":\"info\",\"git\":\"%s\",\"built\":\"%s %s\",\"clk_hz\":%lu,\"board\":\"%s\","
           "\"pin_d0\":%d,\"pin_clkio\":%d,\"block_words\":%d,\"n_blocks\":%d,\"in_words\":%d,"
           "\"pin_ready\":%d,\"seed\":%lu,\"cap_words_max\":%d,\"cap_words_max_selftest\":%d,"
           "\"n_codes\":%d,\"kernels\":[",
           GIT_REV, __DATE__, __TIME__, (unsigned long)clock_get_hz(clk_sys), PICO_BOARD,
           PIN_D0, PIN_CLKIO, BLOCK_WORDS, N_BLOCKS, IN_WORDS, PIN_IN_READY, (unsigned long)SEED,
           CAP_WORDS_MAX, TX_CAP_SELFTEST_MAX, N_DVBS2_CODES);
    for (int i = 0; i < IQ_N_KERNELS; i++)
        printf("%s\"%s:%d:%d\"", i ? "," : "", IQ_KERNELS[i].name, IQ_KERNELS[i].sps, IQ_KERNELS[i].L);
    printf("]}\n");
}

// Run one command line. Returns NULL, or an error message.
// The SDK places each core's 2 KiB stack directly above SRAM code in its 4 KiB scratch bank,
// so an overflow silently overwrites that code (CORE0_STACK_SRAM_HI moves core 0's stack).
// Paint each stack, plus any gap down to adjacent code, at boot before core 1 starts;
// stackcheck reports the deepest use and whether the scratch code still matches flash.
extern uint32_t __scratch_x_start__[], __scratch_x_end__[], __scratch_x_source__[];
extern uint32_t __scratch_y_start__[], __scratch_y_end__[], __scratch_y_source__[];
extern uint32_t __scratch_y_code_end__[];
extern uint32_t __StackBottom[], __StackTop[], __StackOneBottom[], __StackOneTop[];
#define STACK_PAINT 0x5aa5c33cu

static uint32_t *paint_floor(uint32_t *code_end, uint32_t *bottom) {
    return code_end <= bottom && (uintptr_t)bottom - (uintptr_t)code_end < 4096u ? code_end : bottom;
}

static void stack_paint(void) {
    uint32_t sp;
    __asm volatile("mov %0, sp" : "=r"(sp));
    for (uint32_t *w = paint_floor(__scratch_y_end__, __StackBottom); w < (uint32_t *)(sp - 256u); ++w)
        *w = STACK_PAINT;
    for (uint32_t *w = paint_floor(__scratch_x_end__, __StackOneBottom); w < __StackOneTop; ++w)
        *w = STACK_PAINT;
}

static void stack_report(const char *name, uint32_t *start, uint32_t *end, const uint32_t *source,
                         const uint32_t *code_end, uint32_t *bottom, uint32_t *top) {
    uint32_t *floor = paint_floor(end, bottom), *w = floor;
    while (w < top && *w == STACK_PAINT) ++w;
    int corrupted = -1;
    for (const uint32_t *c = start; c < code_end; ++c)
        if (*c != source[c - start]) { corrupted = (int)((uintptr_t)c - (uintptr_t)start); break; }
    printf("\"%s\":{\"code_bytes\":%u,\"code_matches_flash\":%s,\"first_corrupt_code_offset\":%d,"
           "\"stack_bytes\":%u,\"stack_in_code_bank\":%s,\"deepest_use_bytes\":%u,\"paint_exhausted\":%s}",
           name, (unsigned)((uintptr_t)code_end - (uintptr_t)start), corrupted < 0 ? "true" : "false", corrupted,
           (unsigned)((uintptr_t)top - (uintptr_t)bottom), floor == end ? "true" : "false",
           (unsigned)((uintptr_t)top - (uintptr_t)w), w == floor ? "true" : "false");
}

static const char *dispatch(char *line) {
    char *a[12] = {0};
    int n = 0;
    for (char *t = strtok(line, " \t\r"); t && n < (int)(sizeof a / sizeof a[0]);
         t = strtok(0, " \t\r")) a[n++] = t;
    if (!n) return NULL;
    const char *cmd = a[0];
    tx_run_t r = {.code = -1, .pv = -1, .cap_ms = -1};
    int code, pilots, reps, khz, single_core;

#ifdef PV_AUTONOMOUS
    if (!strcmp(cmd, "start")) return flight_start();
    if (!strcmp(cmd, "status")) { tx_status(); return NULL; }
    if (!strcmp(cmd, "stop")) return NULL;
#endif
    if (!strcmp(cmd, "stackcheck")) {
        // headroom 0 means the paint was exhausted: the stack reached the code (or beyond).
        printf("@{\"cmd\":\"stackcheck\",\"git\":\"%s\",", GIT_REV);
        stack_report("core0_scratch_y", __scratch_y_start__, __scratch_y_end__, __scratch_y_source__,
                     __scratch_y_code_end__, __StackBottom, __StackTop);
        putchar(',');
        stack_report("core1_scratch_x", __scratch_x_start__, __scratch_x_end__, __scratch_x_source__,
                     __scratch_x_end__, __StackOneBottom, __StackOneTop);
        printf("}\n");
        return NULL;
    }
    if (!strcmp(cmd, "info")) {
        cmd_info();
    } else if (!strcmp(cmd, "bootsel")) {
        reset_usb_boot(0, 0);
    } else if (!strcmp(cmd, "snifftest")) {
        bench_sniffer();
    } else if (!strcmp(cmd, "pvbench")) {
        int kernel;
        if (!num(a[1], "reps", 8, 1, 1000, &reps) || !num(a[2], "kernel", 0, 0, 2, &kernel)) return err;
        bench_pv(reps, kernel);
    } else if (!strcmp(cmd, "pvstress")) {
        int irq_mask_us;
        if (!num(a[1], "ms", 1000, 100, 60000, &r.ms) ||
            !num(a[2], "irq_mask_us", 0, 0, 200, &irq_mask_us)) return err;
        bench_pvstress(r.ms, irq_mask_us);
    } else if (!strcmp(cmd, "ptrbench")) {
        if (!num(a[1], "reps", 8, 1, 100, &reps)) return err;
        bench_pointers(reps);
    } else if (!strcmp(cmd, "bchkernel")) {
        if (!num(a[1], "kernel", REQUIRED, 0, 3, &code)) return err;
        if (!dvbs2_bch_set_kernel((dvbs2_bch_kernel_t)code)) return "unknown BCH kernel";
        printf("@{\"cmd\":\"bchkernel\",\"kernel\":%d}\n", (int)dvbs2_bch_get_kernel());
    } else if (!strcmp(cmd, "mapkernel")) {
        if (!num(a[1], "kernel", REQUIRED, 0, 1, &code)) return err;
        if (!dvbs2_map_set_kernel((unsigned)code)) return "unknown mapping kernel";
        printf("@{\"cmd\":\"mapkernel\",\"kernel\":%u}\n", dvbs2_map_get_kernel());
    } else if (!strcmp(cmd, "bchbank")) {
        if (!num(a[1], "bank", REQUIRED, 0, 1, &code)) return err;
        if (!dvbs2_frontend_set_bank((unsigned)code)) return "unknown BCH frontend bank";
        printf("@{\"cmd\":\"bchbank\",\"bank\":%u}\n", dvbs2_frontend_get_bank());
    } else if (!strcmp(cmd, "ldpcpack")) {
        if (!num(a[1], "pack", REQUIRED, 0, 1, &code)) return err;
        if (!dvbs2_ldpc_set_pack((unsigned)code)) return "unknown LDPC packer";
        printf("@{\"cmd\":\"ldpcpack\",\"pack\":%u}\n", dvbs2_ldpc_get_pack());
    } else if (!strcmp(cmd, "bench8psk")) {
        if (!num(a[1], "reps", 4, 1, 100, &reps)) return err;
        bench_8psk(reps);
    } else if (!strcmp(cmd, "bench8quad")) {
        if (!num(a[1], "ms", 3000, 0, 60000, &r.ms) ||
            !num(a[2], "reps", 4, 1, 100, &reps)) return err;
        bench_8quad(r.ms, reps);
    } else if (!strcmp(cmd, "bench8live")) {
        if (!num(a[1], "ms", 3000, 1000, 60000, &r.ms)) return err;
        bench_8live(r.ms);
    } else if (!strcmp(cmd, "benchvcm")) {
        if (!num(a[1], "ms", 3000, 1000, 60000, &r.ms)) return err;
        bench_vcm(r.ms);
    } else if (!strcmp(cmd, "bench8map")) {
        if (!num(a[1], "code", 8, 0, N_DVBS2_CODES - 1, &code) ||
            !num(a[2], "pilots", 1, 0, 1, &r.pilots) ||
            !num(a[3], "reps", 32, 1, 1000, &reps)) return err;
        bench_8map(code, r.pilots, reps);
    } else if (!strcmp(cmd, "benchquadptr")) {
        if (!num(a[1], "mode", 1, 0, 1, &code)) return err;
        bench_quadptr(code);
#ifdef IQ4HIST_EXPERIMENT
    } else if (!strcmp(cmd, "pvwide")) {
        int width, half, fault, emulate;
        if (!num(a[1], "ms", 1000, 100, 60000, &r.ms) ||
            !num(a[2], "width", 4, 1, 4, &width) ||
            !num(a[3], "half", 4, 3, 16, &half) ||
            !num(a[4], "fault", 0, 0, 2, &fault) ||
            !num(a[5], "emulate", 1, 0, 1, &emulate)) return err;
        bench_pvwide(r.ms, width, half, fault, emulate);
    } else if (!strcmp(cmd, "r2grant")) {
        int gap_us, fault;
        if (!num(a[1], "batches", 8, 1, 8, &reps) ||
            !num(a[2], "cpw", 1, 1, 4, &r.cpw) ||
            !num(a[3], "gap_us", 0, 0, 20000, &gap_us) ||
            !num(a[4], "fault", 0, 0, 1, &fault) ||
            !num(a[5], "prio", 13, 0, 15, &code)) return err;
        bench_r2grant(reps, r.cpw, gap_us, fault, code);
    } else if (!strcmp(cmd, "c3pair")) {
        int pairs, layout, who, options;
        if (!num(a[1], "code", REQUIRED, 0, N_DVBS2_CODES - 1, &code) || !num(a[2], "pairs", 40, 1, 200, &pairs) ||
            !num(a[3], "layout", 0, 0, 4, &layout) || !num(a[4], "who", 2, 0, 7, &who) ||
            !c3_options(a[5], C4_ALL, C8_BEST, &options)) return err;
        bench_c3_pair(code, pairs, layout, who, options);
    } else if (!strcmp(cmd, "b9")) {
        int invert, options, priority;
        if (!num(a[1], "cpw", 1, 1, 2, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert) ||
            !num(a[4], "prio", 13, 0, 15, &priority) ||
            !num(a[5], "slow_us", 0, 0, 10000, &reps) ||
            !c3_options(a[6], C8_BEST_BASE, C8_BEST_BASE, &options)) return err;
        bench_b9(r.cpw, r.ms, invert, priority, reps, options);
    } else if (!strcmp(cmd, "c3fec")) {
        int gap_us, options;
        if (!num(a[1], "code", REQUIRED, 0, N_DVBS2_CODES - 1, &code) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "slow_us", 0, 0, 10000, &reps) ||
            !num(a[4], "cpw", 1, 1, 4, &r.cpw) || !num(a[5], "gap_us", 128, 128, 4096, &gap_us) ||
            !c3_options(a[6], 0, C8_BEST_BASE, &options)) return err;
        bench_c3_fec_options(code, r.ms, reps, r.cpw, gap_us, options);
    } else if (!strcmp(cmd, "bench4live")) {
        int invert;
        if (!num(a[1], "cpw", 2, 2, 4, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert)) return err;
        bench4live(r.cpw, r.ms, invert);
    } else if (!strcmp(cmd, "bench4live8")) {
        int invert;
        if (!num(a[1], "cpw", 2, 1, 4, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert) ||
            !num(a[4], "prio", 13, 0, 15, &code)) return err;
        bench4live8(r.cpw, r.ms, invert, code);
    } else if (!strcmp(cmd, "bench4prod8") || !strcmp(cmd, "bench4prod8live")) {
        int invert, starve;
        if (!num(a[1], "cpw", 2, 1, 4, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert) ||
            !num(a[4], "prio", 13, 0, 15, &code) ||
            !num(a[5], "starve_ms", 0, 0, 59999, &starve)) return err;
        bench4prod8(r.cpw, r.ms, invert, code, !strcmp(cmd, "bench4prod8live"), starve);
    } else if (!strcmp(cmd, "bench5quad8")) {
        int invert;
        if (!num(a[1], "cpw", 2, 1, 2, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert) ||
            !num(a[4], "prio", 13, 0, 15, &code)) return err;
        bench5quad8(r.cpw, r.ms, invert, code);
    } else if (!strcmp(cmd, "bench4quad8") || !strcmp(cmd, "bench4quad")) {
        int invert;
        if (!num(a[1], "cpw", 2, 1, 4, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert) ||
            !num(a[4], "prio", 13, 0, 15, &code)) return err;
        bench4quad(r.cpw, r.ms, invert, code, !strcmp(cmd, "bench4quad8") ? 8 : 14);
    } else if (!strcmp(cmd, "dmaloop")) {
        int chains, words, sink, load_div, load_kind, hammer;
        if (!num(a[1], "chains", 1, 1, 4, &chains) || !num(a[2], "words", 1, 1, 2, &words) ||
            !num(a[3], "sink", 1, 0, 2, &sink) || !num(a[4], "load_div", 0, 0, 64, &load_div) ||
            !num(a[5], "load_kind", 0, 0, 1, &load_kind) || !num(a[6], "prio", 0, 0, 15, &code) ||
            !num(a[7], "hammer", 0, 0, 1, &hammer)) return err;
        if (load_div == 1) return "load_div must be 0 (off) or 2..64";
        dmaloop(chains, words, sink, load_div, load_kind, code, hammer);
    } else if (!strcmp(cmd, "bench4fec")) {
        int invert;
        if (!num(a[1], "cpw", 2, 2, 4, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert) ||
            !num(a[4], "prio", 0, 0, 15, &code)) return err;
        int bits;
        if (!num(a[5], "bits", 14, 8, 14, &bits)) return err;
        bench4fec(r.cpw, r.ms, invert, code, bits);
#endif
    } else if (!strcmp(cmd, "quadlive")) {
        int fault;
        if (!num(a[1], "cpw", REQUIRED, 1, 2, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "fault", 0, 0, 1, &fault)) return err;
        bench_quadlive(r.cpw, r.ms, fault);
    } else if (!strcmp(cmd, "bench16out")) {
        int kernel;
        if (!num(a[1], "cpw", 6, 4, 8, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "kernel", 3, 0, 3, &kernel)) return err;
        bench_16out(r.cpw, r.ms, kernel);
    } else if (!strcmp(cmd, "bench16planar")) {
        if (!num(a[1], "cpw", 6, 4, 8, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms)) return err;
        bench_16planar(r.cpw, r.ms);
    } else if (!strcmp(cmd, "bench16native8")) {
        if (!num(a[1], "reps", 8, 1, 100, &reps)) return err;
        bench_16native8(reps);
    } else if (!strcmp(cmd, "benchapsklive")) {
        int schedule, mapper, modulation, placement;
        if (!num(a[1], "cpw", 6, 3, 8, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "schedule", 2, 0, 2, &schedule) ||
            !num(a[4], "mapper", 0, 0, 3, &mapper) ||
            !num(a[5], "modulation", 32, 16, 32, &modulation) ||
            !num(a[6], "placement", 0, 0, 1, &placement)) return err;
        bench_apsk_live(r.cpw, r.ms, schedule, mapper, modulation, placement);
    } else if (!strcmp(cmd, "benchapskfec")) {
        int modulation, options;
        if (!num(a[1], "modulation", 16, 16, 32, &modulation) ||
            !num(a[2], "cpw", 12, 3, 32, &r.cpw) ||
            !num(a[3], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[4], "split", 0, 0, 1, &code) ||
            !num(a[5], "options", 0, 0, 7, &options)) return err;
        bench_apsk_fec(modulation, r.cpw, r.ms, code, options);
    } else if (!strcmp(cmd, "bench32bus")) {
        int mode, group;
        if (!num(a[1], "mode", 0, 0, 4, &mode) ||
            !num(a[2], "group", 0, 0, 1, &group)) return err;
        bench_32bus(mode, group);
    } else if (!strcmp(cmd, "bench32map")) {
        if (!num(a[1], "reps", 32, 1, 1000, &reps)) return err;
        bench_32map(reps);
    } else if (!strcmp(cmd, "bench32out")) {
        int kernel;
        if (!num(a[1], "cpw", 6, 4, 8, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "kernel", 1, 0, 1, &kernel)) return err;
        bench_32out(r.cpw, r.ms, kernel);
    } else if (!strcmp(cmd, "bench32native8")) {
        if (!num(a[1], "reps", 8, 1, 100, &reps)) return err;
        bench_32native8(reps);
    } else if (!strcmp(cmd, "bench16chunk")) {
        if (!num(a[1], "reps", 8, 1, 100, &reps)) return err;
        bench_16chunk(reps);
    } else if (!strcmp(cmd, "ddrtest")) {
        if (!num(a[1], "ms", 100, 1, 5000, &r.ms)) return err;
        bench_hstx_ddr(r.ms);
    } else if (!strcmp(cmd, "quadprofile")) {
        if (!num(a[1], "profile", REQUIRED, 0, 1, &code)) return err;
        if (!bench_dual_set_quad_profile((unsigned)code)) return "unknown quad profile";
        printf("@{\"cmd\":\"quadprofile\",\"profile\":%d}\n", code);
    } else if (!strcmp(cmd, "dualwriters")) {
        if (!num(a[1], "writers", REQUIRED, 2, 4, &code)) return err;
        if (!dual_dma_set_writers((unsigned)code)) return dual_dma_last_error();
        printf("@{\"cmd\":\"dualwriters\",\"writers\":%d}\n", code);
    } else if (!strcmp(cmd, "dualclock")) {
        if (!num(a[1], "mode", REQUIRED, 0, 2, &code)) return err;
        if (!dual_dma_set_dac_clock((unsigned)code)) return dual_dma_last_error();
        printf("@{\"cmd\":\"dualclock\",\"mode\":%d}\n", code);
    } else if (!strcmp(cmd, "dualprio")) {
        if (!num(a[1], "mask", REQUIRED, 0, 15, &code)) return err;
        if (!dual_dma_set_bus_priority((unsigned)code)) return dual_dma_last_error();
        printf("@{\"cmd\":\"dualprio\",\"mask\":%d}\n", code);
    } else if (!strcmp(cmd, "dualbench")) {
        if (!num(a[1], "reps", 4, 1, 100, &reps)) return err;
        bench_descriptors(reps);
    } else if (!strcmp(cmd, "dualptr") || !strcmp(cmd, "dualptr4")) {
        int source, fault, code_index, pv_kernel;
        if (!num(a[1], "cpw", REQUIRED, 1, 2, &r.cpw) ||
            !num(a[2], "ms", REQUIRED, 100, 60000, &r.ms) ||
            !num(a[3], "source", 6, 5, 6, &source) ||
            !num(a[4], "fault", 0, 0, 1, &fault) ||
            !num(a[5], "code", 8, 5, 10, &code_index) ||
            !num(a[6], "pvkernel", 0, 0, 3, &pv_kernel)) return err;
        if (code_index == 6 || code_index == 7)
            return "pointer trial codes are normal2/3(5),5/6(8),8/9(9),9/10(10)";
        if (pv_kernel && source != 5) return "pvkernel 1-3 is the TS adapter: source 5 only";
        if (!strcmp(cmd, "dualptr4")) bench_dual_pointer4(r.cpw, r.ms, source, fault, code_index, pv_kernel);
        else bench_dual_pointer(r.cpw, r.ms, source, fault, code_index, pv_kernel);
    } else if (!strcmp(cmd, "dualstream")) {
        int kernel, source, fault, schedule, divider256, row_symbols, code_index;
        if (!num(a[1], "cpw", REQUIRED, 1, 4, &r.cpw) ||
            !num(a[2], "ms", REQUIRED, 10, 60000, &r.ms) ||
            !num(a[3], "kernel", 1, 0, 2, &kernel) ||
            !num(a[4], "source", 1, 0, 6, &source) ||
            !num(a[5], "cap_words", 4096, 0, 4096, &r.cap_words) ||
            !num(a[6], "fault", 0, 0, 2, &fault) ||
            !num(a[7], "schedule", 0, 0, 3, &schedule) ||
            !num(a[8], "divider256", 0, 0, 1024, &divider256) ||
            !num(a[9], "row_symbols", 1, 1, 4, &row_symbols) ||
            !num(a[10], "code", 5, 5, 10, &code_index)) return err;
        if (code_index == 7)
            return "trial codes are normal2/3(5),3/4(6),5/6(8),8/9(9),9/10(10)";
        if (row_symbols != 1 && row_symbols != 2 && row_symbols != 4)
            return "row_symbols must be1,2 or4 (L9 beta0.8 trial)";
        if (divider256 && divider256 != 384) return "optional divider256 must be384 (1.5 clocks)";
        if (fault && !source) return "fault injection requires live descriptors";
        if (source >= 5 && row_symbols != 4 &&
            (row_symbols != 2 || dual_dma_get_writers() != 4))
            return "PV-SPI trial needs row2/four writers or opt-in L9 row4";
        bench_dual_stream(r.cpw, r.ms, kernel, source, r.cap_words, fault, schedule, divider256, row_symbols, code_index);
    } else if (!strcmp(cmd, "clock")) {
        if (!num(a[1], "khz", REQUIRED, 1, 150000, &khz)) return err;
        if (!set_sys_clock_khz((uint32_t)khz, false)) return "clock not reachable";
        cmd_info();
    } else if (!strcmp(cmd, "bench")) {
        const iq_kernel_info_t *k;
        bool tables_ok;
        if (!num(a[2], "sps", REQUIRED, 1, 16, &r.sps) || !num(a[3], "L", REQUIRED, 1, MAX_L, &r.L) ||
            !num(a[4], "reps", 4, 1, 1000, &reps))
            return err;
        const char *e = tx_shaper(a[1] ? a[1] : "", r.sps, r.L, &k, &tables_ok);
        if (e) return e;
        bench_kernel(k, reps, tables_ok);
    } else if (!strcmp(cmd, "dvbs2")) {
        if (!num(a[1], "code", REQUIRED, 0, N_DVBS2_CODES - 1, &code) ||
            !num(a[2], "pilots", REQUIRED, 0, 1, &pilots) || !num(a[3], "reps", 4, 1, 1000, &reps))
            return err;
        bench_dvbs2(code, pilots, reps);
    } else if (!strcmp(cmd, "stream")) {
        r.kernel = a[1] ? a[1] : "", r.cores = a[4];
        if (!num(a[2], "sps", REQUIRED, 1, 16, &r.sps) || !num(a[3], "L", REQUIRED, 1, MAX_L, &r.L) ||
            !cores_ok(a[4]) || !num(a[5], "cpw", REQUIRED, 2, IQOUT_MAX_CPW, &r.cpw) ||
            !num(a[6], "ms", REQUIRED, 1, INT_MAX, &r.ms) ||
            !num(a[7], "cap_words", 0, 0, CAP_WORDS_MAX, &r.cap_words) ||
            !num(a[8], "lanes", 0, 0, 4, &r.lanes) || !num(a[9], "half", 6, 2, 16, &r.half) ||
            !cap_ok(r.cap_words))
            return err;
        if (r.lanes == 3) return "lanes: expected 0, 1, 2 or 4";
        return tx_run(&r);
    } else if (!strcmp(cmd, "txs2")) {
        r.kernel = a[3] ? a[3] : "", r.cores = "0";
        if (!num(a[1], "code", REQUIRED, 0, N_DVBS2_CODES - 1, &r.code) ||
            !num(a[2], "pilots", REQUIRED, 0, 1, &r.pilots) ||
            !num(a[4], "sps", REQUIRED, 1, 16, &r.sps) || !num(a[5], "L", REQUIRED, 1, MAX_L, &r.L) ||
            !num(a[6], "cpw", REQUIRED, 2, IQOUT_MAX_CPW, &r.cpw) ||
            !num(a[7], "ms", REQUIRED, 1, INT_MAX, &r.ms) ||
            !num(a[8], "cap_words", 0, 0, CAP_WORDS_MAX, &r.cap_words) || !cap_ok(r.cap_words) ||
            !num(a[9], "single_core", 0, 0, 1, &single_core))
            return err;
        r.single_core = single_core;
        return tx_run(&r);
    } else if (!strcmp(cmd, "pvtx")) {
        r = PVTX;
        if (!num(a[1], "cpw", REQUIRED, 2, IQOUT_MAX_CPW, &r.cpw) ||
            !num(a[2], "ms", REQUIRED, 1, INT_MAX, &r.ms) ||
            !num(a[3], "selftest", REQUIRED, 0, 1, &r.pv) ||
            !num(a[4], "cap_words", 0, 0, r.pv ? TX_CAP_SELFTEST_MAX : CAP_WORDS_MAX, &r.cap_words) ||
            !num(a[5], "cap_ms", -1, -1, r.ms - 1, &r.cap_ms) ||
            !num(a[6], "single_core", 0, 0, 1, &single_core))
            return err;
        if (!cap_ok(r.cap_words)) return err;
        r.single_core = single_core;
        return tx_run(&r);
    } else {
        return "unknown command";
    }
    return NULL;
}

int main(void) {
    set_sys_clock_khz(DEFAULT_SYS_KHZ, true);
    stack_paint();
    stdio_init_all();
    tx_init();
#ifdef PV_AUTONOMOUS
    const char *startup_error = flight_start();
    if (startup_error) printf("@{\"error\":\"%s\"}\n", startup_error);
#endif
    char line[128];
    size_t n = 0;
    for (;;) {
        const int c = getchar_timeout_us(100000);
        if (c == PICO_ERROR_TIMEOUT) continue;
        if (c == '\n' || c == '\r') {
            line[n] = 0;
            const char *e = n ? dispatch(line) : NULL;
            if (e) printf("@{\"error\":\"%s\"}\n", e);
            n = 0;
        } else if (n < sizeof line - 1) {
            line[n++] = (char)c;
        }
    }
}
