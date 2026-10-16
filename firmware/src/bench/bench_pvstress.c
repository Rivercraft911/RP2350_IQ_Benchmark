#include "bench.h"
#include "tx.h"

#include <stdio.h>
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"

static void drain_packets(uint32_t *count, uint32_t *crc) {
    uint8_t *packet;
    while ((packet = pvspi_next_packet()) != NULL) {
        *crc = pv_crc32(*crc, packet, PV_TS);
        ++*count;
    }
}

void bench_pvstress(int ms, int irq_mask_us) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if (hz != 128000000u && hz != 150000000u) {
        printf("@{\"error\":\"pvstress requires128 or150MHz\"}\n");
        return;
    }
    const unsigned half = hz == 128000000u ? 2 : 3;
    pv_init(PV_EMU_MSGS);
    pvspi_start(pio2, in_buf, true, cap_buf + CAP_WORDS_MAX - PV_EMU_WORDS, half);
    uint32_t packets = 0, packet_crc = 0, windows = 0, max_mask = 0;
    const uint64_t start = time_us_64(), end = start + (uint64_t)ms * 1000;
    while (time_us_64() < end) {
        drain_packets(&packets, &packet_crc);
        if (irq_mask_us) {
            const uint32_t saved = save_and_disable_interrupts(), t = cycles();
            busy_wait_us_32((uint32_t)irq_mask_us);
            const uint32_t elapsed = cycles() - t;
            restore_interrupts(saved);
            if (elapsed > max_mask) max_mask = elapsed;
            ++windows;
        }
    }
    const uint64_t elapsed = time_us_64() - start;
    pvspi_pause();
    // READY is now low. Let the one admitted message finish with IRQs enabled.
    busy_wait_us_32(1000);
    drain_packets(&packets, &packet_crc);
    pvspi_stop();
    printf("@{\"cmd\":\"pvstress\",\"git\":\"%s\",\"clk_hz\":%lu,\"ms\":%d,"
           "\"elapsed_us\":%llu,\"irq_mask_us\":%d,\"mask_windows\":%lu,\"max_mask_cycles\":%lu,"
           "\"sck_hz\":%lu,\"packets\":%lu,\"packet_crc\":%lu,\"ok\":%lu,\"lost\":%lu,"
           "\"bad_hdr\":%lu,\"bad_crc\":%lu,\"bad_sync\":%lu,\"crc_chain\":%lu,"
           "\"short_msgs\":%lu,\"long_msgs\":%lu,\"overflows\":%lu,\"deferred_msgs\":%lu,"
           "\"drain_timeouts\":%lu,\"late_closes\":%lu,\"last_short_words\":%lu,"
           "\"last_short_fifo\":%lu,\"last_short_ctrl\":%lu,\"last_short_pc\":%lu,\"last_short_cs\":%lu,\"errors\":%lu}\n",
           GIT_REV, (unsigned long)hz, ms, (unsigned long long)elapsed, irq_mask_us,
           (unsigned long)windows, (unsigned long)max_mask, (unsigned long)(hz / (2 * half)),
           (unsigned long)packets, (unsigned long)packet_crc, (unsigned long)pv_stats.ok,
           (unsigned long)pv_stats.lost, (unsigned long)pv_stats.bad_hdr,
           (unsigned long)pv_stats.bad_crc, (unsigned long)pv_stats.bad_sync,
           (unsigned long)pv_stats.crc_chain, (unsigned long)pvspi.short_msgs,
           (unsigned long)pvspi.long_msgs, (unsigned long)pvspi.overflows,
           (unsigned long)pvspi.deferred_msgs, (unsigned long)pvspi.drain_timeouts,
           (unsigned long)pvspi.late_closes, (unsigned long)pvspi.last_short_words,
           (unsigned long)pvspi.last_short_fifo, (unsigned long)pvspi.last_short_ctrl,
           (unsigned long)pvspi.last_short_pc, (unsigned long)pvspi.last_short_cs,
           (unsigned long)pvspi.errors);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
