#include "bench_ptr.h"

#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "ptrprog.h"
#include "tx.h"
#include "git_rev.h"

enum { AXIS_WORDS = 256, PAD_WORDS = 8, FEED_WORDS = AXIS_WORDS + PAD_WORDS,
       ADDRESSES = AXIS_WORDS * 4, STREAM_SYMBOLS = AXIS_WORDS * 16,
       ERR_CONFIG = 1, ERR_TIMEOUT = 2, ERR_MISMATCH = 4, ERR_TX = 8, ERR_DMA = 16,
       ERR_RXUNDER = 32 };
static const uint32_t BASE = 0x20020000u;
static const uint32_t SEEDS[2] = {0x1234abcdu, 0x9e3779b9u};
_Static_assert(FEED_WORDS + 4 * ADDRESSES <= N_BLOCKS * BLOCK_WORDS, "pointer bench fits ring_buf");

static uint32_t next_word(uint32_t *state) {
    uint32_t x = *state;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return *state = x;
}

static uint32_t reference_address(const uint16_t *axis, unsigned symbol) {
    uint32_t key = 0;
    // Independent serial-bit definition: the bit of age a is key bit a.
    // Pair-swap is undone only when reading a chronological source bit.
    for (unsigned age = 0; age < 13; age++) if (symbol >= age) {
        const unsigned n = symbol - age;
        key |= ((axis[n / 16] >> ((n % 16) ^ 1u)) & 1u) << age;
    }
    return BASE + 16u * key;
}

static void prepare_dma(int channel) {
    dma_hw->ch[channel].al1_ctrl = DMA_CH0_CTRL_TRIG_READ_ERROR_BITS |
        DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS | (uint32_t)channel << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB;
    for (uint irq = 0; irq < NUM_DMA_IRQS; irq++) dma_irqn_set_channel_enabled(irq, channel, false);
    dma_hw->intr = 1u << channel;
}

void bench_pointers(int reps) {
    if (reps < 1 || reps > 100) { printf("@{\"error\":\"ptrbench reps1..100\"}\n"); return; }
    PIO pio = pio1;
    const pio_program_t program = {.instructions = PTRPROG_CODE, .length = PTRPROG_LENGTH, .origin = -1};
    int sm[4] = {-1, -1, -1, -1}, input_dma[4] = {-1, -1, -1, -1}, output_dma[4] = {-1, -1, -1, -1};
    uint offset = 0;
    bool added = false;
    uint32_t errors = 0, mismatches = 0, txstalls = 0, rxstalls = 0, dma_errors = 0;
    uint32_t mask = 0, input_mask = 0, output_mask = 0, minimum = UINT32_MAX, maximum = 0, completed = 0;
    uint64_t elapsed = 0;
    uint32_t crc[4] = {0}, expected_crc[4] = {0};
    uint16_t *axis[2] = {(uint16_t *)ring_buf, (uint16_t *)ring_buf + FEED_WORDS};
    uint32_t *out[4];
    for (unsigned lane = 0; lane < 4; lane++) out[lane] = ring_buf + FEED_WORDS + lane * ADDRESSES;
    for (unsigned a = 0; a < 2; a++) {
        uint32_t state = SEEDS[a];
        for (unsigned w = 0; w < FEED_WORDS; w++) axis[a][w] = ptrprog_pair_swap((uint16_t)next_word(&state));
    }
    for (unsigned lane = 0; lane < 4; lane++)
        for (unsigned j = 0; j < ADDRESSES; j++) {
            const uint32_t want = reference_address(axis[lane / 2], 1u + 2u * (lane & 1u) + 4u * j);
            expected_crc[lane] = crc32_update(expected_crc[lane], &want, sizeof want);
        }
    if (!pio_can_add_program(pio, &program)) { errors |= ERR_CONFIG; goto cleanup; }
    for (unsigned lane = 0; lane < 4; lane++) {
        sm[lane] = pio_claim_unused_sm(pio, false);
        if (sm[lane] < 0) { errors |= ERR_CONFIG; goto cleanup; }
        mask |= 1u << sm[lane];
        input_dma[lane] = dma_claim_unused_channel(false);
        output_dma[lane] = dma_claim_unused_channel(false);
        if (input_dma[lane] < 0 || output_dma[lane] < 0) { errors |= ERR_CONFIG; goto cleanup; }
        prepare_dma(input_dma[lane]); prepare_dma(output_dma[lane]);
        input_mask |= 1u << input_dma[lane]; output_mask |= 1u << output_dma[lane];
    }
    offset = pio_add_program(pio, &program); added = true;
    for (int rep = 0; rep < reps && !errors; rep++) {
        for (unsigned lane = 0; lane < 4; lane++) {
            pio_sm_config config = pio_get_default_sm_config();
            sm_config_set_wrap(&config, offset, offset + PTRPROG_LENGTH - 1);
            sm_config_set_in_shift(&config, false, false, 32);
            sm_config_set_out_shift(&config, true, false, PTRPROG_PULL_BITS);
            pio_sm_init(pio, sm[lane], offset + ((lane & 1u) ? PTRPROG_SKIP : PTRPROG_PUBLISH), &config);
            // Preload the constant prefix and empty OSR. X remains reset/all-positive history.
            pio_sm_put(pio, sm[lane], BASE >> 17);
            pio_sm_exec_wait_blocking(pio, sm[lane], pio_encode_pull(false, true));
            pio_sm_exec_wait_blocking(pio, sm[lane], pio_encode_mov(pio_y, pio_osr));
            pio_sm_exec_wait_blocking(pio, sm[lane], pio_encode_set(pio_x, 0));
            pio_sm_exec_wait_blocking(pio, sm[lane], pio_encode_out(pio_null, 32));
            dma_channel_config dc = dma_channel_get_default_config(input_dma[lane]);
            channel_config_set_transfer_data_size(&dc, DMA_SIZE_16);
            channel_config_set_read_increment(&dc, true);
            channel_config_set_write_increment(&dc, false);
            channel_config_set_dreq(&dc, pio_get_dreq(pio, sm[lane], true));
            channel_config_set_irq_quiet(&dc, true);
            dma_channel_configure(input_dma[lane], &dc, &pio->txf[sm[lane]], axis[lane / 2], FEED_WORDS, false);
            dc = dma_channel_get_default_config(output_dma[lane]);
            channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
            channel_config_set_read_increment(&dc, false);
            channel_config_set_write_increment(&dc, true);
            channel_config_set_dreq(&dc, pio_get_dreq(pio, sm[lane], false));
            // Completion raises raw INTR; no IRQ enable/handler is installed.
            channel_config_set_irq_quiet(&dc, false);
            dma_channel_configure(output_dma[lane], &dc, out[lane], &pio->rxf[sm[lane]], ADDRESSES, false);
        }
        dma_hw->intr = input_mask | output_mask;
        __dmb();
        dma_start_channel_mask(output_mask);
        dma_start_channel_mask(input_mask);
        const uint32_t prefill_start = time_us_32();
        for (;;) {
            bool full = true;
            for (unsigned lane = 0; lane < 4; lane++) full &= pio_sm_is_tx_fifo_full(pio, sm[lane]);
            if (full) break;
            if ((uint32_t)(time_us_32() - prefill_start) >= 1000u) { errors |= ERR_TIMEOUT; break; }
        }
        pio->fdebug = (mask << PIO_FDEBUG_TXSTALL_LSB) | (mask << PIO_FDEBUG_RXSTALL_LSB) |
                      (mask << PIO_FDEBUG_TXOVER_LSB) | (mask << PIO_FDEBUG_RXUNDER_LSB);
        if (!errors) {
            const uint32_t save = save_and_disable_interrupts();
            const uint32_t t0 = cycles(), wall = time_us_32();
            pio_enable_sm_mask_in_sync(pio, mask);
            while ((dma_hw->intr & output_mask) != output_mask) {
                busy_wait_at_least_cycles(64); // bounded observation overhead, not FASTPERI busy-polling
                if ((uint32_t)(time_us_32() - wall) >= 20000u) { errors |= ERR_TIMEOUT; break; }
            }
            const uint32_t dt = cycles() - t0;
            pio_set_sm_mask_enabled(pio, mask, false);
            restore_interrupts(save);
            if (!errors) {
                elapsed += dt; completed++;
                if (dt < minimum) minimum = dt;
                if (dt > maximum) maximum = dt;
            }
        }
        pio_set_sm_mask_enabled(pio, mask, false);
        const uint32_t flags = pio->fdebug;
        txstalls |= (flags >> PIO_FDEBUG_TXSTALL_LSB) & mask;
        rxstalls |= (flags >> PIO_FDEBUG_RXSTALL_LSB) & mask;
        // Extra source words prevent end-of-input PULL stalls. RX backpressure after a
        // finite output capture is expected and reported; it is not output data loss.
        if (flags & ((mask << PIO_FDEBUG_TXSTALL_LSB) | (mask << PIO_FDEBUG_TXOVER_LSB))) errors |= ERR_TX;
        if (flags & (mask << PIO_FDEBUG_RXUNDER_LSB)) errors |= ERR_RXUNDER;
        for (unsigned lane = 0; lane < 4; lane++) {
            for (unsigned n = 0; n < 2; n++) {
                const int channel = n ? output_dma[lane] : input_dma[lane];
                if (dma_hw->ch[channel].ctrl_trig & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS) {
                    dma_errors |= 1u << channel; errors |= ERR_DMA;
                }
                dma_channel_abort(channel);
            }
            if (!(errors & ERR_TIMEOUT)) {
                crc[lane] = crc32_update(0, out[lane], ADDRESSES * sizeof(uint32_t));
                for (unsigned j = 0; j < ADDRESSES; j++)
                    mismatches += out[lane][j] != reference_address(axis[lane / 2], 1u + 2u * (lane & 1u) + 4u * j);
            }
        }
        if (mismatches) errors |= ERR_MISMATCH;
    }
cleanup:
    if (mask) pio_set_sm_mask_enabled(pio, mask, false);
    for (unsigned lane = 0; lane < 4; lane++) {
        if (input_dma[lane] >= 0) { dma_channel_cleanup(input_dma[lane]); dma_channel_unclaim(input_dma[lane]); }
        if (output_dma[lane] >= 0) { dma_channel_cleanup(output_dma[lane]); dma_channel_unclaim(output_dma[lane]); }
        if (sm[lane] >= 0) pio_sm_unclaim(pio, sm[lane]);
    }
    if (added) pio_remove_program(pio, &program, offset);
    const double cps = completed ? (double)elapsed / completed / STREAM_SYMBOLS : 0;
    printf("@{\"cmd\":\"ptrbench\",\"git\":\"%s\",\"clk_hz\":%lu,\"reps\":%d,\"completed\":%lu,"
           "\"errors\":%lu,\"mismatches\":%lu,\"pio_sms\":4,\"dma_channels\":8,\"base\":%lu,"
           "\"seed_i\":%lu,\"seed_q\":%lu,\"axis_halfwords\":%u,\"addresses_per_sm\":%u,\"symbols\":%u,"
           "\"pair_swapped_input\":true,\"cyc_min\":%lu,\"cyc_max\":%lu,\"cyc_per_sym\":%.5f,"
           "\"component_sym_rate\":%.1f,\"model_cyc_per_sym\":3.5625,\"txstall_mask\":%lu,"
           "\"rxstall_mask\":%lu,\"dma_error_mask\":%lu,\"crc\":[%lu,%lu,%lu,%lu],"
           "\"expected_crc\":[%lu,%lu,%lu,%lu]}\n", GIT_REV, (unsigned long)clock_get_hz(clk_sys), reps,
           (unsigned long)completed, (unsigned long)errors, (unsigned long)mismatches, (unsigned long)BASE,
           (unsigned long)SEEDS[0], (unsigned long)SEEDS[1], AXIS_WORDS, ADDRESSES, STREAM_SYMBOLS,
           (unsigned long)(completed ? minimum : 0), (unsigned long)maximum, cps,
           cps ? clock_get_hz(clk_sys) / cps : 0, (unsigned long)txstalls, (unsigned long)rxstalls,
           (unsigned long)dma_errors, (unsigned long)crc[0], (unsigned long)crc[1],
           (unsigned long)crc[2], (unsigned long)crc[3], (unsigned long)expected_crc[0],
           (unsigned long)expected_crc[1], (unsigned long)expected_crc[2], (unsigned long)expected_crc[3]);
}
