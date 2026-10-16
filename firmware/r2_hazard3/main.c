#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/clocks.h"
#include "dvbs2_8psk.h"
#include "dvbs2_codes.h"

void r2_base_codes_begin(dvbs2_8psk_codes_state_t *state, const dvbs2_8psk_cfg_t *cfg,
                         const uint32_t *frame, dvbs2_8psk_codes_stream_t *stream);
bool r2_base_codes_step(dvbs2_8psk_codes_state_t *state, uint32_t max_words);

enum { WORDS = 2048, FE_WORDS = 2027, GOLD_WORDS = 1384 };
static uint32_t frame[FE_WORDS], gold[GOLD_WORDS], lanes[2][2][WORDS];
static dvbs2_8psk_cfg_t cfg;

static inline uint32_t cycles(void) {
#ifdef __riscv
    uint32_t result;
    __asm__ volatile("csrr %0, mcycle" : "=r"(result) : : "memory");
    return result;
#else
    __asm__ volatile("" : : : "memory");
    return *(volatile uint32_t *)0xe0001004u;
#endif
}

static void counter_init(void) {
#ifdef __riscv
    __asm__ volatile("csrci mcountinhibit, 1" : : : "memory");
#else
    *(volatile uint32_t *)0xe000edfcu |= 1u << 24;
    *(volatile uint32_t *)0xe0001000u |= 1u;
#endif
}

static uint32_t checksum(const uint32_t *p, unsigned count) {
    uint32_t result = 2166136261u;
    for (unsigned i = 0; i < count; i++) result = (result ^ p[i]) * 16777619u;
    return result;
}

static void run(unsigned code, unsigned count, unsigned fault) {
    if ((code != 5 && code != 6 && code != 8) || !count || count > 10000 || fault > 1) {
        puts("@{\"bench\":\"r2map\",\"error\":\"r2map code(5|6|8) frames(1..10000) fault(0|1)\"}");
        return;
    }
    if (!dvbs2_8psk_init(&cfg, &DVBS2_CODES[code], true, 0, gold, GOLD_WORDS)) {
        puts("@{\"bench\":\"r2map\",\"error\":\"init\"}"); return;
    }
    uint32_t random = 0xd1b54a35u;
    for (unsigned i = 0; i < FE_WORDS; i++) {
        random ^= random << 13; random ^= random >> 17; random ^= random << 5;
        frame[i] = i < 2025 ? random : 0;
    }
    memset(lanes, 0, sizeof lanes);
    uint64_t total[2] = {0, 0};
    uint32_t max_frame[2] = {0, 0}, max_slice[2] = {0, 0};
    dvbs2_8psk_codes_stream_t streams[2] = {
        {.i = lanes[0][0], .q = lanes[0][1], .mask = WORDS - 1},
        {.i = lanes[1][0], .q = lanes[1][1], .mask = WORDS - 1}};
    // Alternate order per frame to avoid charging all cold-cache work to one.
    for (unsigned f = 0; f < count; f++) {
        for (unsigned k = 0; k < 2; k++) {
            const unsigned kernel = k ^ (f & 1u);
            dvbs2_8psk_codes_state_t state;
            if (kernel) dvbs2_8psk_codes_begin(&state, &cfg, frame, &streams[kernel]);
            else r2_base_codes_begin(&state, &cfg, frame, &streams[kernel]);
            bool done;
            uint32_t elapsed = 0;
            do {
                const uint32_t start = cycles();
                done = kernel ? dvbs2_8psk_codes_step(&state, 8) : r2_base_codes_step(&state, 8);
                const uint32_t delta = cycles() - start;
                elapsed += delta;
                if (delta > max_slice[kernel]) max_slice[kernel] = delta;
            } while (!done);
            total[kernel] += elapsed;
            if (elapsed > max_frame[kernel]) max_frame[kernel] = elapsed;
        }
    }
    if (fault) lanes[1][0][(streams[1].n - 1u) & (WORDS - 1u)] ^= 1u;
    const bool equal = !memcmp(lanes[0], lanes[1], sizeof lanes[0]) &&
        streams[0].n == streams[1].n && streams[0].fill == streams[1].fill &&
        streams[0].acc_i == streams[1].acc_i && streams[0].acc_q == streams[1].acc_q;
#if defined(__riscv) && !defined(IQ_R2_HAZARD3_EMULATE)
    const char *arch = "hazard3", *candidate = "zbkb";
#else
    const char *arch = "arm", *candidate = "emulated-not-hazard3";
#endif
    printf("@{\"bench\":\"r2map\",\"arch\":\"%s\",\"candidate\":\"%s\","
           "\"sys_hz\":%" PRIu32 ",\"code\":%u,\"frames\":%u,\"fault\":%u,\"equal\":%s,"
           "\"symbols_per_frame\":%" PRIu32 ",\"slice_words\":8,"
           "\"ring_groups\":%u,\"published_groups\":%" PRIu32 ",\"pending_fill\":%" PRIu32 ","
           "\"pending_codes\":[%" PRIu32 ",%" PRIu32 "],"
           "\"cycles\":[%" PRIu64 ",%" PRIu64 "],\"max_frame_cycles\":[%" PRIu32 ",%" PRIu32 "],"
           "\"max_slice_cycles\":[%" PRIu32 ",%" PRIu32 "],"
           "\"hash_i\":[%" PRIu32 ",%" PRIu32 "],\"hash_q\":[%" PRIu32 ",%" PRIu32 "]}\n",
           arch, candidate, clock_get_hz(clk_sys), code, count, fault, equal ? "true" : "false",
           cfg.frame_symbols, WORDS, streams[0].n, streams[0].fill, streams[0].acc_i, streams[0].acc_q,
           total[0], total[1], max_frame[0], max_frame[1],
           max_slice[0], max_slice[1], checksum(lanes[0][0], WORDS), checksum(lanes[1][0], WORDS),
           checksum(lanes[0][1], WORDS), checksum(lanes[1][1], WORDS));
}

int main(void) {
    stdio_init_all();
    counter_init();
    char line[96];
    unsigned used = 0;
    for (;;) {
        const int c = getchar_timeout_us(1000);
        if (c == PICO_ERROR_TIMEOUT) continue;
        if (c == '\r') continue;
        if (c == '\n') {
            line[used] = 0; used = 0;
            unsigned code = 5, count = 100, fault = 0;
            if (!strncmp(line, "r2map", 5) && (!line[5] || line[5] == ' ')) {
                if (line[5]) sscanf(line + 5, "%u %u %u", &code, &count, &fault);
                run(code, count, fault);
            } else puts("r2map code(5|6|8) frames(1..10000) fault(0|1)");
        } else if (used + 1 < sizeof line) line[used++] = (char)c;
    }
}
