#include "r2grant.h"

#include <stdio.h>
#include <string.h>

#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/structs/bus_ctrl.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "tx.h"

enum { STREAMS = 4, GUARD_WORDS = 4, STRIDE = R2_GRANT_RING_WORDS + GUARD_WORDS,
       ERR_CONFIG = 1u, ERR_DMA = 2u, ERR_TIMEOUT = 4u, ERR_ORDER = 8u,
       ERR_STOP = 16u, ERR_SEAM = 32u, ERR_SENTINEL = 64u, GUARD = 0xdeadbabeu };
_Static_assert(STREAMS * STRIDE <= N_BLOCKS * BLOCK_WORDS, "borrow output arena for exact echo");
_Static_assert(2 * R2_GRANT_RING_WORDS == IN_WORDS, "borrow paired source rings");
_Static_assert(STREAMS * (STRIDE + R2_GRANT_BATCHES + 1) <= N_BLOCKS * BLOCK_WORDS,
               "borrow immutable count lists after echo guards");

static void prepare_channel(unsigned ch) {
    dma_channel_cleanup(ch);
    dma_hw->ch[ch].al1_ctrl = DMA_CH0_CTRL_TRIG_READ_ERROR_BITS |
        DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS | ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB;
    for (unsigned irq = 0; irq < NUM_DMA_IRQS; ++irq)
        dma_irqn_set_channel_enabled(irq, ch, false);
    dma_hw->intr = 1u << ch;
}

static bool set_priority(uint32_t value) {
    busctrl_hw->priority = value;
    __dmb();
    const uint32_t start = time_us_32();
    while (!(busctrl_hw->priority_ack & BUSCTRL_BUS_PRIORITY_ACK_BITS))
        if ((uint32_t)(time_us_32() - start) > 1000u) return false;
    return true;
}

void bench_r2grant(int batches, int cpw, int gap_us, int fault, int priority) {
    if (batches < 1 || batches > R2_GRANT_BATCHES || (cpw != 1 && cpw != 2 && cpw != 4) ||
        gap_us < 0 || gap_us > 20000 || fault < 0 || fault > 1 || (fault && batches < 2) ||
        priority < 0 || priority > 15) {
        printf("@{\"cmd\":\"r2grant\",\"error\":\"needs batches1..8,cpw1/2/4,gap0..20000,fault0/1 (fault needs2batches),prio0..15\"}\n");
        return;
    }
    // No GPIO access: four independent SMs echo complete words at the B6 average
    // input rate, one word/16 symbols =128*cpw SYS cycles. X makes the terminal
    // halt explicit, so TXSTALL diagnoses a grant seam rather than normal EOF.
    const uint16_t instructions[] = {
        pio_encode_pull(false, true) | pio_encode_delay(31),
        pio_encode_mov(pio_isr, pio_osr) | pio_encode_delay(31),
        pio_encode_push(false, true) | pio_encode_delay(31),
        pio_encode_jmp_x_dec(0) | pio_encode_delay(31), pio_encode_jmp(4),
    };
    const pio_program_t program = {.instructions = instructions, .length = 5, .origin = -1};
    int sm[STREAMS] = {-1, -1, -1, -1};
    int input[STREAMS] = {-1, -1, -1, -1}, reload[STREAMS] = {-1, -1, -1, -1};
    int capture[STREAMS] = {-1, -1, -1, -1};
    uint32_t (*const counts)[R2_GRANT_BATCHES + 1] =
        (uint32_t (*)[R2_GRANT_BATCHES + 1])(ring_buf + STREAMS * STRIDE);
    uint32_t grants[STREAMS], issued[STREAMS] = {0}, captured[STREAMS] = {0};
    uint32_t crc[STREAMS] = {0}, expected_crc[STREAMS] = {0};
    unsigned offset = 0;
    uint32_t mask = 0, sm_mask = 0, input_mask = 0, reload_mask = 0, capture_mask = 0;
    uint32_t errors = 0, txstall = 0, elapsed = 0, null_mask = 0, mismatches = 0;
    const uint32_t words = (unsigned)batches * R2_GRANT_WORDS;
    const uint32_t hz = clock_get_hz(clk_sys), saved_priority = busctrl_hw->priority;
    bool added = false, borrowed = false, released = true, guards_ok = true;
    if (!pio_can_add_program(pio0, &program)) { errors = ERR_CONFIG; goto cleanup; }
    offset = pio_add_program(pio0, &program);
    added = true;
    for (unsigned stream = 0; stream < STREAMS; ++stream) {
        sm[stream] = pio_claim_unused_sm(pio0, false);
        if (sm[stream] < 0) { errors = ERR_CONFIG; goto cleanup; }
        sm_mask |= 1u << sm[stream];
        int *const channels[] = {&input[stream], &reload[stream], &capture[stream]};
        for (unsigned k = 0; k < 3; ++k) {
            *channels[k] = dma_claim_unused_channel(false);
            if (*channels[k] < 0) { errors = ERR_CONFIG; goto cleanup; }
            mask |= 1u << *channels[k];
            prepare_channel((unsigned)*channels[k]);
        }
        input_mask |= 1u << input[stream]; reload_mask |= 1u << reload[stream];
        capture_mask |= 1u << capture[stream];
    }
    borrowed = true;
    for (unsigned axis = 0; axis < 2; ++axis)
        for (unsigned i = 0; i < R2_GRANT_RING_WORDS; ++i)
            in_buf[axis * R2_GRANT_RING_WORDS + i] = r2grant_source(axis, i);
    for (unsigned i = 0; i < STREAMS * STRIDE; ++i) ring_buf[i] = GUARD;
    for (unsigned stream = 0; stream < STREAMS; ++stream) {
        const unsigned n = (unsigned)sm[stream], axis = stream & 1u;
        grants[stream] = words - ((fault && stream == 3) ? R2_GRANT_WORDS : 0);
        if (!r2grant_plan(counts[stream], R2_GRANT_BATCHES + 1, grants[stream])) {
            errors = ERR_CONFIG; goto cleanup;
        }
        pio_sm_config c = pio_get_default_sm_config();
        sm_config_set_wrap(&c, offset, offset + 4);
        sm_config_set_clkdiv_int_frac8(&c, (unsigned)cpw, 0);
        pio_sm_init(pio0, n, offset, &c);
        pio_sm_put(pio0, n, words - 1);
        pio_sm_exec_wait_blocking(pio0, n, pio_encode_pull(false, true));
        pio_sm_exec_wait_blocking(pio0, n, pio_encode_mov(pio_x, pio_osr));

        dma_channel_config d = dma_channel_get_default_config((unsigned)input[stream]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_dreq(&d, pio_get_dreq(pio0, n, true));
        channel_config_set_chain_to(&d, (unsigned)reload[stream]);
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure((unsigned)input[stream], &d, &pio0->txf[n],
                              in_buf + axis * R2_GRANT_RING_WORDS, 0, false);

        d = dma_channel_get_default_config((unsigned)reload[stream]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure((unsigned)reload[stream], &d,
                              &dma_hw->ch[input[stream]].al1_transfer_count_trig,
                              counts[stream], 1, false);

        d = dma_channel_get_default_config((unsigned)capture[stream]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, false);
        channel_config_set_write_increment(&d, true);
        channel_config_set_dreq(&d, pio_get_dreq(pio0, n, false));
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure((unsigned)capture[stream], &d, ring_buf + stream * STRIDE,
                              &pio0->rxf[n], words, false);
    }
    const uint32_t bus_priority = ((priority & 1u) << 0) | ((priority & 2u) << 3) |
        ((priority & 4u) << 6) | ((priority & 8u) << 9);
    if (!set_priority(bus_priority)) { errors |= ERR_CONFIG; goto cleanup; }
    __dmb(); // every source/count store precedes the first reload trigger
    dma_start_channel_mask(capture_mask | reload_mask);
    {
        const uint32_t start = time_us_32();
        for (unsigned stream = 0; stream < STREAMS; ++stream)
            while (!pio_sm_is_tx_fifo_full(pio0, (unsigned)sm[stream]))
                if ((uint32_t)(time_us_32() - start) > 1000u) {
                    errors |= ERR_TIMEOUT; goto cleanup;
                }
    }
    pio0->fdebug = sm_mask << PIO_FDEBUG_TXSTALL_LSB;
    {
        const uint32_t start = time_us_32();
        const uint32_t limit = (uint64_t)words * 128u * (unsigned)cpw * 1000000u / hz + 2000u;
        pio_enable_sm_mask_in_sync(pio0, sm_mask);
        // Deliberate foreground service gap; core 1 receives no task. The chain
        // runs during this interval without any CPU reads/writes of DMA CSRs.
        busy_wait_us_32((unsigned)gap_us);
        while ((dma_hw->intr & input_mask) != input_mask) {
            if ((uint32_t)(time_us_32() - start) > limit + (unsigned)gap_us) {
                errors |= ERR_TIMEOUT; break;
            }
            tight_loop_contents();
        }
        while (!(errors & ERR_TIMEOUT)) {
            bool active = false;
            for (unsigned stream = 0; stream < STREAMS; ++stream)
                active |= dma_channel_is_busy((unsigned)capture[stream]);
            if (!active) break;
            if ((uint32_t)(time_us_32() - start) > limit + (unsigned)gap_us) {
                errors |= ERR_TIMEOUT; break;
            }
            tight_loop_contents();
        }
        elapsed = time_us_32() - start;
    }
    null_mask = dma_hw->intr & input_mask;
    if (null_mask != input_mask) errors |= ERR_SENTINEL;
    txstall = (pio0->fdebug >> PIO_FDEBUG_TXSTALL_LSB) & sm_mask;
    if (txstall) errors |= ERR_SEAM;
    for (unsigned stream = 0; stream < STREAMS; ++stream) {
        issued[stream] = (dma_hw->ch[input[stream]].read_addr -
            (uintptr_t)(in_buf + (stream & 1u) * R2_GRANT_RING_WORDS)) / 4;
        captured[stream] = words - dma_hw->ch[capture[stream]].transfer_count;
        if (issued[stream] != grants[stream]) errors |= ERR_ORDER;
    }
cleanup:
    hw_clear_bits(&pio0->ctrl, sm_mask);
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) if (mask & (1u << ch)) {
        if (dma_hw->ch[ch].ctrl_trig & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS) errors |= ERR_DMA;
        hw_write_masked(&dma_hw->ch[ch].al1_ctrl, ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                        DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS | DMA_CH0_CTRL_TRIG_EN_BITS);
    }
    dma_hw->abort = mask;
    {
        const uint32_t start = time_us_32();
        while (dma_hw->abort & mask)
            if ((uint32_t)(time_us_32() - start) > 1000u) {
                errors |= ERR_STOP; released = false; break;
            }
    }
    if (released) {
        for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) if (mask & (1u << ch)) {
            dma_channel_cleanup(ch); dma_channel_unclaim(ch);
        }
        for (unsigned stream = 0; stream < STREAMS; ++stream) if (sm[stream] >= 0) {
            pio_sm_clear_fifos(pio0, (unsigned)sm[stream]);
            pio_sm_unclaim(pio0, (unsigned)sm[stream]);
        }
        if (added) pio_remove_program(pio0, &program, offset);
    }
    if (!set_priority(saved_priority)) errors |= ERR_STOP;
    if (borrowed && released) {
        for (unsigned stream = 0; stream < STREAMS; ++stream) {
            for (unsigned i = 0; i < captured[stream]; ++i)
                if (ring_buf[stream * STRIDE + i] != r2grant_source(stream & 1u, i)) mismatches++;
            for (unsigned i = captured[stream]; i < STRIDE; ++i)
                if (ring_buf[stream * STRIDE + i] != GUARD) guards_ok = false;
            crc[stream] = crc32_update(0, ring_buf + stream * STRIDE, captured[stream] * 4);
            expected_crc[stream] = crc32_update(0, in_buf + (stream & 1u) * R2_GRANT_RING_WORDS,
                                               captured[stream] * 4);
        }
        if (mismatches || !guards_ok) errors |= ERR_ORDER;
        uint32_t seed = SEED;
        prbs_fill(in_buf, IN_WORDS, &seed);
    }
    printf("@{\"cmd\":\"r2grant\",\"git\":\"%s\",\"sys_hz\":%lu,\"batches\":%d,\"cpw\":%d,"
           "\"gap_us\":%d,\"fault\":%d,\"priority\":%d,\"published_words\":%lu,\"elapsed_us\":%lu,"
           "\"errors\":%lu,\"txstall_mask\":%lu,\"null_mask\":%lu,\"input_mask\":%lu,"
           "\"mismatches\":%lu,\"guards_ok\":%s,\"released\":%s,\"core1_tasks\":0,"
           "\"dma_channels\":12,\"pio_sms\":4,\"pio_words\":5,\"issued\":[%lu,%lu,%lu,%lu],"
           "\"captured\":[%lu,%lu,%lu,%lu],\"crc\":[%lu,%lu,%lu,%lu],\"expected_crc\":[%lu,%lu,%lu,%lu]}\n",
           GIT_REV, (unsigned long)hz, batches, cpw, gap_us, fault, priority,
           (unsigned long)words, (unsigned long)elapsed, (unsigned long)errors, (unsigned long)txstall,
           (unsigned long)null_mask, (unsigned long)input_mask, (unsigned long)mismatches,
           guards_ok ? "true" : "false", released ? "true" : "false",
           (unsigned long)issued[0], (unsigned long)issued[1], (unsigned long)issued[2], (unsigned long)issued[3],
           (unsigned long)captured[0], (unsigned long)captured[1], (unsigned long)captured[2], (unsigned long)captured[3],
           (unsigned long)crc[0], (unsigned long)crc[1], (unsigned long)crc[2], (unsigned long)crc[3],
           (unsigned long)expected_crc[0], (unsigned long)expected_crc[1],
           (unsigned long)expected_crc[2], (unsigned long)expected_crc[3]);
}
