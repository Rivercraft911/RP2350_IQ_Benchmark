// RP2350 I/Q waveform benchmark. Line commands over USB CDC; results as JSON lines prefixed '@',
// errors as @{"error":"..."}.
//   info
//   clock <khz>                                   set clk_sys (stock max 150000)
//   bench <kernel> <sps> <L> [reps]               kernel throughput + output CRC
//   stream <kernel> <sps> <L> <cores> <cpw> <ms> [cap_words] [lanes half]
//        cores: 0 | 1 | 01 (alternate blocks); cpw: PIO system clocks per 16-bit bus word
//        lanes > 0: input arrives over the PIO link (emulated host), half = SCK half period
//   dvbs2 <code> <pilots> [reps]                  DVB-S2 encoder stage cycles + PLFRAME CRC
//   txs2 <code> <pilots> <kernel> <sps> <L> <cpw> <ms> [cap_words] [single_core]
//        full transmitter: core 1 encodes DVB-S2 frames, core 0 shapes and streams
//        code: index into DVBS2_CODES (dvbs2_codes.h)
//        single_core 1 = cooperative encoder + shaper on core 0; default 0 = two cores
//   pvtx <cpw> <ms> <selftest> [cap_words] [cap_ms] [single_core]
//        PigeonVision TX: TS over PV-SPI v1 (docs/pv-spi-spec.md) -> DVB-S2 normal QPSK 2/3 with
//        pilots -> shaper (N = 4, L = 12). selftest 1 = on-chip emulated CM5 master.
//   snifftest                                     DMA sniffer CRC modes
//   pvbench [reps] / ptrbench [reps]              packet preparation / pin-free PIO pointers
//   bchkernel <0|1|2|3>                           baseline, C words, ARM stream, t8 ARM stream
//   bench8psk [reps]                              experimental split-LUT shaping
//   bench8quad ms reps / bench8map code pilots reps   8PSK shaper / mapper components
//   bench8live ms / benchvcm ms                   complete 5/6 8PSK / alternating QPSK+8PSK
//   bench16chunk reps / bench16native8 reps       16APSK shaper components
//   bench16out cpw ms kernel                     16APSK output with capture, no FEC
//   bench16planar cpw ms                         experimental dual-axis planar output
//   bench32native8 reps                          32APSK shaper components
//   bench32out cpw ms kernel                     32APSK rolling-key output, no FEC
//   benchapsklive cpw ms schedule mapper mod placement    runtimeAPSK, 1/2 cores
//   benchapskfec mod cpw ms [split]             APSK with live SPI and FEC; split1 = BCH on core 0
//   bench32map reps                              matched32APSK mapper components
//   bench32bus mode group                        no-output bus contention diagnosis
//   benchquadptr mode / quadlive cpw ms fault     finite / live PIO address generation
//   pvstress ms irq_mask_us                      on-chip SPI admission test
//   ddrtest [ms]                                  experimental PIO data + HSTX DDR clock
//   dualbench [reps]                             descriptor generation experiment
//   dualwriters <2|4> / dualprio <0..15>          idle experimental DMA settings
//   dualclock <0|1|2>                             0 HSTX CLK_IO, 1 GP28 PIO DDR clock, 2 inverted control
//   quadprofile <0|1> / mapkernel <0|1>          L9/L8 pulse / baseline/packed mapper
//   ldpcpack <0|1>                               LDPC parity: baseline / streaming in-place pack
//   bchbank <0|1>                                frontend BCH steps: striped SRAM / scratch-Y copy
//   dualstream cpw ms kernel source cap fault [schedule] [div256] [row_symbols] [code]
//   dualptr cpw ms source fault [code]           live SPI/FEC, PIO pointers (source5/6)
//        source: 0=cyclic, 1=PRBS, 2=core1 FEC, 3=one-core FEC, 4=split FEC, 5=SPI TS, 6=SPI GCS
//   bootsel                                       reboot to the USB bootloader
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
    if (!strcmp(cmd, "info")) {
        cmd_info();
    } else if (!strcmp(cmd, "bootsel")) {
        reset_usb_boot(0, 0);
    } else if (!strcmp(cmd, "snifftest")) {
        bench_sniffer();
    } else if (!strcmp(cmd, "pvbench")) {
        if (!num(a[1], "reps", 8, 1, 1000, &reps)) return err;
        bench_pv(reps);
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
    } else if (!strcmp(cmd, "bench4live")) {
        int invert;
        if (!num(a[1], "cpw", 2, 2, 4, &r.cpw) ||
            !num(a[2], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[3], "invert", 0, 0, 1, &invert)) return err;
        bench4live(r.cpw, r.ms, invert);
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
        int modulation;
        if (!num(a[1], "modulation", 16, 16, 32, &modulation) ||
            !num(a[2], "cpw", 12, 3, 32, &r.cpw) ||
            !num(a[3], "ms", 3000, 100, 60000, &r.ms) ||
            !num(a[4], "split", 0, 0, 1, &code)) return err;
        bench_apsk_fec(modulation, r.cpw, r.ms, code);
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
    } else if (!strcmp(cmd, "dualptr")) {
        int source, fault, code_index;
        if (!num(a[1], "cpw", REQUIRED, 1, 2, &r.cpw) ||
            !num(a[2], "ms", REQUIRED, 100, 60000, &r.ms) ||
            !num(a[3], "source", 6, 5, 6, &source) ||
            !num(a[4], "fault", 0, 0, 1, &fault) ||
            !num(a[5], "code", 8, 5, 10, &code_index)) return err;
        if (code_index == 6 || code_index == 7)
            return "pointer trial codes are normal2/3(5),5/6(8),8/9(9),9/10(10)";
        bench_dual_pointer(r.cpw, r.ms, source, fault, code_index);
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
