#include "bench_quadptr.h"

#include <stdio.h>
#include <string.h>

#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "iqquadptr.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    INPUT_WORDS = IQQUADPTR_BATCH_WORDS, AXIS_POINTERS = 4 * INPUT_WORDS,
    CAPTURE_WORDS = 2 * AXIS_POINTERS, TIMEOUT_US = 100000, PREVIOUS = 0x716bc429,
    ERR_CONFIG = 1u << 0, ERR_TABLE = 1u << 1, ERR_TIMEOUT = 1u << 2,
    ERR_CAPTURE = 1u << 3, ERR_ADDRESS = 1u << 4, ERR_BOUNDARY = 1u << 5,
    ERR_POINTER = 1u << 6, ERR_CLEANUP = 1u << 7,
};
_Static_assert(IN_WORDS == 8192, "pointer test uses the aligned8K-word input ring");
_Static_assert(CAPTURE_WORDS <= CAP_WORDS_MAX, "pointer capture arena");
_Static_assert(2 * IQQUAD_L8_WORDS <= N_BLOCKS * BLOCK_WORDS, "aligned64KiB table arena");

static bool resources_available(void) {
    unsigned free_dma = 0, free_sm = 0;
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) free_dma += !dma_channel_is_claimed(ch);
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) free_sm += !pio_sm_is_claimed(pio2, sm);
    return free_dma >= 6 && free_sm >= 2;
}

static int source_channel(unsigned axis, unsigned *sm, uint32_t excluded) {
    const volatile uint32_t *rx = iqquadptr_rx_fifo(axis);
    for (*sm = 0; *sm < NUM_PIO_STATE_MACHINES; ++*sm) if (rx == &pio2->rxf[*sm]) break;
    if (*sm == NUM_PIO_STATE_MACHINES) return -1;
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch)
        if (!(excluded & (1u << ch)) && dma_channel_is_claimed(ch) && dma_hw->ch[ch].read_addr == (uintptr_t)in_buf + 2 * axis &&
            dma_hw->ch[ch].write_addr == (uintptr_t)&pio2->txf[*sm]) return (int)ch;
    return -1;
}

static void capture_configure(unsigned channel, unsigned axis, bool bswap) {
    dma_channel_cleanup(channel);
    dma_hw->ch[channel].al1_ctrl = DMA_CH0_CTRL_TRIG_READ_ERROR_BITS |
        DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS | channel << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB;
    for (unsigned irq = 0; irq < NUM_DMA_IRQS; ++irq) dma_irqn_set_channel_enabled(irq, channel, false);
    dma_hw->intr = 1u << channel;
    dma_channel_config c = dma_channel_get_default_config(channel);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_32);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, true);
    channel_config_set_dreq(&c, iqquadptr_rx_dreq(axis));
    channel_config_set_bswap(&c, bswap);
    channel_config_set_irq_quiet(&c, true);
    channel_config_set_high_priority(&c, true);
    dma_channel_configure(channel, &c, cap_buf + axis * AXIS_POINTERS,
                          iqquadptr_rx_fifo(axis), AXIS_POINTERS, false);
}

static bool capture_stop(const int channels[2]) {
    uint32_t mask = 0;
    for (unsigned axis = 0; axis < 2; ++axis) if (channels[axis] >= 0) {
        const unsigned ch = (unsigned)channels[axis];
        hw_write_masked(&dma_hw->ch[ch].al1_ctrl, ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                        DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS | DMA_CH0_CTRL_TRIG_EN_BITS);
        mask |= 1u << ch;
    }
    if (!mask) return true;
    dma_hw->abort = mask;
    const uint32_t start = time_us_32();
    while (dma_hw->abort & mask)
        if ((uint32_t)(time_us_32() - start) >= 1000) return false;
    for (unsigned axis = 0; axis < 2; ++axis) if (channels[axis] >= 0) {
        dma_channel_cleanup(channels[axis]);
        dma_channel_unclaim(channels[axis]);
    }
    return true;
}

static unsigned address_mismatches(unsigned axis, uint32_t base, bool bswap, uint32_t *expected_crc) {
    uint32_t history = (PREVIOUS >> (16 * axis + 9)) & 127u;
    unsigned mismatches = 0, row = 0;
    *expected_crc = 0;
    for (unsigned word = 0; word < INPUT_WORDS; ++word) {
        const uint32_t bits = in_buf[word] >> (16 * axis);
        for (unsigned group = 0; group < 4; ++group) {
            const uint32_t window = history | ((bits >> (4 * group)) & 15u) << 7;
            const uint32_t key = (window & 7u) | (((window >> 7) & 15u) << 3) |
                                 (((window >> 3) & 15u) << 7);
            uint32_t expected = base + 32 * key;
            if (!bswap) expected = __builtin_bswap32(expected);
            *expected_crc = crc32_update(*expected_crc, &expected, sizeof expected);
            mismatches += cap_buf[axis * AXIS_POINTERS + row++] != expected;
            history = window >> 4;
        }
    }
    return mismatches;
}

void bench_quadptr(int mode) {
    if ((mode != 0 && mode != 1) || !resources_available()) {
        printf("@{\"cmd\":\"benchquadptr\",\"error\":\"needs mode0/1, idle buffers, two PIO2 SMs and six free DMA channels\"}\n");
        return;
    }
    const iqquad_profile_t pulse = IQQUAD_PROFILE_L8_OPTIMIZED_BOTH;
    const iqquadptr_profile_info_t *profile = iqquadptr_profile_info(pulse);
    const uintptr_t arena = (uintptr_t)ring_buf, arena_end = arena + sizeof ring_buf;
    const uintptr_t base = (arena + profile->table_alignment - 1u) & ~(uintptr_t)(profile->table_alignment - 1u);
    if (base + 4 * IQQUAD_L8_WORDS > arena_end) {
        printf("@{\"cmd\":\"benchquadptr\",\"error\":\"L8 aligned64KiB table does not fit ring arena; L9 is unsupported\"}\n");
        return;
    }
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
    in_buf[0] = 0; in_buf[1] = UINT32_MAX; in_buf[2] = 0x5555aaaa; in_buf[3] = PREVIOUS;
    const uint32_t input_crc = crc32_update(0, in_buf, 4 * INPUT_WORDS);
    memset(cap_buf, 0xa5, 4 * CAPTURE_WORDS);
    uint32_t errors = 0, table_crc = 0, capture_mask = 0, input_mask = 0, sm_mask = 0;
    int capture[2] = {-1, -1}, input[2] = {-1, -1};
    unsigned sm[2] = {0, 0};
    uint32_t read_addr[2] = {0, 0}, remaining[2] = {0, 0}, input_busy = 0;
    uint32_t captured[2] = {0, 0}, capture_errors = 0, pio_other = 0, enabled = 0;
    uint32_t mismatches[2] = {0, 0}, expected_crc[2] = {0, 0}, axis_crc[2] = {0, 0};
    uint32_t elapsed = 0;
    bool attempted = false, prepared = false, complete = false, stable = false, cleanup_ok = true, restored = false;
    const char *reason = "complete";
    if (!iqquadptr_build_table_profile((uint32_t *)base, (arena_end - base) / 4, pulse)) {
        errors |= ERR_TABLE; reason = "table build failed"; goto cleanup;
    }
    table_crc = crc32_update(0, (const void *)base, 4 * IQQUAD_L8_WORDS);
    if (table_crc != profile->table_crc) {
        errors |= ERR_TABLE; reason = "table CRC mismatch"; goto cleanup;
    }
    for (unsigned axis = 0; axis < 2; ++axis) {
        capture[axis] = dma_claim_unused_channel(false);
        if (capture[axis] < 0) { errors |= ERR_CONFIG; reason = "capture DMA unavailable"; goto cleanup; }
        capture_mask |= 1u << capture[axis];
    }
    attempted = true;
    prepared = iqquadptr_prepare_profile(pio2, in_buf, 13, (const uint32_t *)base,
                                         PREVIOUS, INPUT_WORDS, pulse);
    if (!prepared) { errors |= ERR_POINTER; reason = iqquadptr_last_error(); goto cleanup; }
    for (unsigned axis = 0; axis < 2; ++axis) {
        input[axis] = source_channel(axis, &sm[axis], capture_mask);
        if (input[axis] < 0) { errors |= ERR_CONFIG; reason = "input DMA identification failed"; goto cleanup; }
        input_mask |= 1u << input[axis]; sm_mask |= 1u << sm[axis];
        capture_configure(capture[axis], axis, mode == 1);
    }
    dma_start_channel_mask(capture_mask);
    const uint32_t start = time_us_32();
    if (!iqquadptr_start()) { errors |= ERR_POINTER; reason = iqquadptr_last_error(); goto cleanup; }
    // A live progress poll would halt on the finite source marker before queued
    // raw words finish shaping. Drain the complete credited prefix first.
    for (;;) {
        bool busy = false;
        for (unsigned axis = 0; axis < 2; ++axis) {
            busy |= dma_channel_is_busy(capture[axis]);
            capture_errors |= dma_hw->ch[capture[axis]].ctrl_trig & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS;
        }
        if (capture_errors) { errors |= ERR_CAPTURE; reason = "capture DMA bus error"; break; }
        if (!busy && (dma_hw->intr & input_mask) == input_mask) { complete = true; break; }
        if ((uint32_t)(time_us_32() - start) >= TIMEOUT_US) {
            errors |= ERR_TIMEOUT; reason = "finite pointer capture timed out"; break;
        }
        tight_loop_contents();
    }
    elapsed = time_us_32() - start;
    for (unsigned axis = 0; axis < 2; ++axis) {
        captured[axis] = AXIS_POINTERS - dma_hw->ch[capture[axis]].transfer_count;
        read_addr[axis] = dma_hw->ch[input[axis]].read_addr;
        remaining[axis] = dma_hw->ch[input[axis]].transfer_count;
        if (dma_channel_is_busy(input[axis])) input_busy |= 1u << input[axis];
    }
    if (complete) {
        busy_wait_us_32(10);
        stable = true;
        for (unsigned axis = 0; axis < 2; ++axis)
            stable &= read_addr[axis] == dma_hw->ch[input[axis]].read_addr &&
                      remaining[axis] == dma_hw->ch[input[axis]].transfer_count;
    }
    enabled = pio2->ctrl & sm_mask;
    pio_other = pio2->fdebug & ((sm_mask << PIO_FDEBUG_TXOVER_LSB) | (sm_mask << PIO_FDEBUG_RXUNDER_LSB));
cleanup:
    if (prepared) iqquadptr_stop();
    const iqquadptr_progress_t progress = attempted ? iqquadptr_progress() : (iqquadptr_progress_t){0};
    cleanup_ok = capture_stop(capture) && !(progress.errors & IQQUADPTR_ERR_STOP);
    if (!cleanup_ok) { errors |= ERR_CLEANUP; reason = "DMA cleanup incomplete; claims retained"; }
    uint32_t expected_errors = IQQUADPTR_ERR_SOURCE_EMPTY;
    if (progress.txstall_mask && !pio_other && enabled == sm_mask) expected_errors |= IQQUADPTR_ERR_PIO;
    const uint32_t unexpected_errors = progress.errors & ~expected_errors;
    if (unexpected_errors) {
        if (!errors) reason = "unexpected pointer fault";
        errors |= ERR_POINTER;
    }
    complete = complete && captured[0] == AXIS_POINTERS && captured[1] == AXIS_POINTERS;
    if (!complete) {
        if (!errors) reason = "capture length mismatch";
        errors |= ERR_CAPTURE;
    }
    const bool boundary_ok = complete && stable && !input_busy && !remaining[0] && !remaining[1] &&
        (progress.null_trigger_mask & input_mask) == input_mask && input_mask &&
        progress.input_i_words == INPUT_WORDS && progress.input_q_words == INPUT_WORDS &&
        progress.granted_words == INPUT_WORDS && progress.published_words == INPUT_WORDS &&
        read_addr[0] == (uintptr_t)in_buf + 4 * INPUT_WORDS &&
        read_addr[1] == (uintptr_t)in_buf + 4 * INPUT_WORDS + 2;
    if (!boundary_ok || !(progress.errors & IQQUADPTR_ERR_SOURCE_EMPTY)) {
        if (!errors) reason = "source boundary mismatch";
        errors |= ERR_BOUNDARY;
    }
    if (complete && cleanup_ok) for (unsigned axis = 0; axis < 2; ++axis) {
        mismatches[axis] = address_mismatches(axis, base, mode == 1, &expected_crc[axis]);
        axis_crc[axis] = crc32_update(0, cap_buf + axis * AXIS_POINTERS, 4 * AXIS_POINTERS);
        if (mismatches[axis]) {
            if (!errors) reason = "numerical pointer mismatch";
            errors |= ERR_ADDRESS;
        }
    }
    const uint32_t cap_words = complete && cleanup_ok ? CAPTURE_WORDS : 0;
    const uint32_t cap_crc = cap_words ? crc32_update(0, cap_buf, 4 * cap_words) : 0;
    if (cleanup_ok) {
        if (cfg.coef) iq_build_tables(&cfg);
        seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); restored = true;
    }
    printf("@{\"cmd\":\"benchquadptr\",\"git\":\"%s\",\"clk_hz\":%lu,\"mode\":%d,"
           "\"profile\":\"qpsk_l8_n4_both_m010_far2\",\"gpio_configured\":false,"
           "\"seed\":%lu,\"previous\":%lu,\"input_words\":%u,\"input_ring_words\":%u,"
           "\"input_base\":%lu,\"input_crc\":%lu,\"ring_base\":%lu,\"ring_bytes\":%u,"
           "\"table_base\":%lu,\"table_bytes\":%u,\"table_crc\":%lu,\"elapsed_us\":%lu,"
           "\"ok\":%s,\"errors\":%lu,\"reason\":\"%s\",\"cleanup_ok\":%s,\"restored\":%s,"
           "\"pointer_errors\":%lu,\"expected_pointer_errors\":%lu,\"unexpected_pointer_errors\":%lu,"
           "\"input_dma_mask\":%lu,\"pointer_sm_mask\":%lu,\"sm_enabled_at_stop\":%lu,"
           "\"null_trigger_mask\":%lu,\"terminal_txstall_mask\":%lu,\"rxstall_mask\":%lu,"
           "\"other_pio_flags\":%lu,\"capture_dma_errors\":%lu,\"input_busy_mask\":%lu,"
           "\"input_read_addr\":[%lu,%lu],\"input_remaining\":[%lu,%lu],\"read_addr_stable\":%s,"
           "\"input_issue_words\":[%llu,%llu],\"published_words\":%llu,\"granted_words\":%llu,"
           "\"capture_axis_words\":[%lu,%lu],\"address_mismatches\":[%lu,%lu],"
           "\"axis_crc\":[%lu,%lu],\"expected_axis_crc\":[%lu,%lu],"
           "\"cap_words\":%lu,\"cap_crc\":%lu,\"cap_layout\":\"I_then_Q\",\"capture_bswap\":%s}\n",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), mode, (unsigned long)SEED,
           (unsigned long)PREVIOUS, INPUT_WORDS, IN_WORDS, (unsigned long)(uintptr_t)in_buf,
           (unsigned long)input_crc, (unsigned long)arena, (unsigned)sizeof ring_buf,
           (unsigned long)base, 4 * IQQUAD_L8_WORDS, (unsigned long)table_crc, (unsigned long)elapsed,
           errors ? "false" : "true", (unsigned long)errors, reason,
           cleanup_ok ? "true" : "false", restored ? "true" : "false",
           (unsigned long)progress.errors, (unsigned long)expected_errors, (unsigned long)unexpected_errors,
           (unsigned long)input_mask, (unsigned long)sm_mask, (unsigned long)enabled,
           (unsigned long)progress.null_trigger_mask, (unsigned long)progress.txstall_mask,
           (unsigned long)progress.rxstall_mask, (unsigned long)pio_other,
           (unsigned long)capture_errors, (unsigned long)input_busy,
           (unsigned long)read_addr[0], (unsigned long)read_addr[1],
           (unsigned long)remaining[0], (unsigned long)remaining[1], stable ? "true" : "false",
           (unsigned long long)progress.input_i_words, (unsigned long long)progress.input_q_words,
           (unsigned long long)progress.published_words, (unsigned long long)progress.granted_words,
           (unsigned long)captured[0], (unsigned long)captured[1],
           (unsigned long)mismatches[0], (unsigned long)mismatches[1],
           (unsigned long)axis_crc[0], (unsigned long)axis_crc[1],
           (unsigned long)expected_crc[0], (unsigned long)expected_crc[1],
           (unsigned long)cap_words, (unsigned long)cap_crc, mode ? "true" : "false");
    if (cap_words) {
        for (unsigned word = 0; word < cap_words; word += 16) {
            printf("@cap %u", word);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)cap_buf[word + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
}
