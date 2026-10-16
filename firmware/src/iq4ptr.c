#include "iq4ptr.h"
#include <stddef.h>

uint32_t iq4ptr_seed(uint32_t previous) {
    uint32_t history = 0;
    for (unsigned age = 0; age < 6; ++age)
        history |= ((previous >> (30 - 2 * age)) & 3u) << (2 * age);
    return history;
}

#ifdef IQ_ON_DEVICE
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"

typedef struct {
    PIO pio;
    const uint32_t *input[2];
    int sm[2], dma[2];
    unsigned offset, mask;
    uint32_t sm_mask, dma_mask, last[2], last_poll_us, poll_limit_us;
    bool owned, added;
    iq4ptr_progress_t result;
} pointer_state_t;

static pointer_state_t state;
static const char *last_error = "not prepared";
static const pio_program_t program = {
    .instructions = IQ4PTR_PROGRAM, .length = IQ4PTR_PROGRAM_LENGTH, .origin = -1,
};

static void halt(void) {
    if (!state.owned) return;
    pio_set_sm_mask_enabled(state.pio, state.sm_mask, false);
    for (unsigned axis = 0; axis < 2; ++axis) if (state.dma[axis] >= 0) {
        const unsigned ch = (unsigned)state.dma[axis];
        // Break chains before abort (RP2350-E5), and stop new DREQs.
        hw_write_masked(&dma_hw->ch[ch].al1_ctrl, ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                        DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS | DMA_CH0_CTRL_TRIG_EN_BITS);
    }
    state.result.running = false;
}

static void sample_cursors(bool check_gap) {
    const uint32_t begin = time_us_32();
    for (unsigned axis = 0; axis < 2; ++axis) {
        const uint32_t address = dma_hw->ch[state.dma[axis]].read_addr;
        const uint32_t cursor = ((address - (uintptr_t)state.input[axis]) >> 2) & state.mask;
        state.result.input_words[axis] += (cursor - state.last[axis]) & state.mask;
        state.last[axis] = cursor;
    }
    const uint32_t end = time_us_32();
    if (check_gap && (uint32_t)(end - state.last_poll_us) >= state.poll_limit_us)
        state.result.errors |= IQ4PTR_ERR_POLL_GAP;
    state.last_poll_us = begin;
}

static void sample_faults(void) {
    const uint32_t flags = state.pio->fdebug;
    const uint32_t tx = (flags >> PIO_FDEBUG_TXSTALL_LSB) & state.sm_mask;
    const uint32_t rx = (flags >> PIO_FDEBUG_RXSTALL_LSB) & state.sm_mask;
    state.result.txstall_mask |= tx;
    state.result.rxstall_mask |= rx; // PUSH backpressure is normal.
    if (tx || (flags & (state.sm_mask << PIO_FDEBUG_TXOVER_LSB)) ||
        (flags & (state.sm_mask << PIO_FDEBUG_RXUNDER_LSB)) ||
        (state.pio->ctrl & state.sm_mask) != state.sm_mask)
        state.result.errors |= IQ4PTR_ERR_PIO;
    for (unsigned axis = 0; axis < 2; ++axis)
        if (dma_hw->ch[state.dma[axis]].ctrl_trig & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS)
            state.result.errors |= IQ4PTR_ERR_DMA;
}

static bool failed(const char *error, uint32_t flag) {
    last_error = error;
    state.result.errors |= flag;
    iq4ptr_stop();
    return false;
}

bool iq4ptr_prepare(PIO pio, const uint32_t *input_i, const uint32_t *input_q,
                   unsigned input_log2, const uint16_t *table,
                   uint32_t previous_i, uint32_t previous_q) {
    if (state.owned) { last_error = "cyclic pointer producer is already owned"; return false; }
    state = (pointer_state_t){.pio = pio, .input = {input_i, input_q},
        .sm = {-1, -1}, .dma = {-1, -1}};
    const uintptr_t base = (uintptr_t)table;
    if ((pio != pio0 && pio != pio1 && pio != pio2) || !input_i || !input_q || !table ||
        input_log2 < 8 || input_log2 > 13 ||
        ((uintptr_t)input_i & ((4u << input_log2) - 1u)) ||
        ((uintptr_t)input_q & ((4u << input_log2) - 1u)) ||
        (base & (IQ4PTR_TABLE_BYTES - 1u)) ||
        base < 0x20000000u || base > 0x20080000u - IQ4PTR_TABLE_BYTES)
        return failed("cyclic input needs aligned256..8192-word rings and128KiB-aligned SRAM table",
                      IQ4PTR_ERR_CONFIG);
    state.mask = (1u << input_log2) - 1u;
    // Maximum issue rate follows the PIO program, even with an unpaced consumer.
    // Reserve eight words for DMA issue/prefetch and timestamp observation.
    state.poll_limit_us = (uint64_t)(state.mask + 1u - 8u) * IQ4PTR_CYCLES_PER_WORD *
        1000000u / clock_get_hz(clk_sys);
    if (!pio_can_add_program(pio, &program))
        return failed("pointer PIO instruction space unavailable", IQ4PTR_ERR_CONFIG);
    state.owned = true;
    for (unsigned axis = 0; axis < 2; ++axis) {
        state.sm[axis] = pio_claim_unused_sm(pio, false);
        if (state.sm[axis] < 0) return failed("two pointer SMs unavailable", IQ4PTR_ERR_CONFIG);
        state.sm_mask |= 1u << state.sm[axis];
        state.dma[axis] = dma_claim_unused_channel(false);
        if (state.dma[axis] < 0) return failed("pointer input DMA unavailable", IQ4PTR_ERR_CONFIG);
        const unsigned ch = (unsigned)state.dma[axis];
        state.dma_mask |= 1u << ch;
        dma_channel_cleanup(ch);
        dma_hw->ch[ch].al1_ctrl = DMA_CH0_CTRL_TRIG_READ_ERROR_BITS |
            DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS | ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB;
        for (unsigned irq = 0; irq < NUM_DMA_IRQS; ++irq)
            dma_irqn_set_channel_enabled(irq, ch, false);
        dma_hw->intr = 1u << ch;
    }
    state.offset = pio_add_program(pio, &program);
    state.added = true;
    const uint32_t previous[] = {previous_i, previous_q};
    for (unsigned axis = 0; axis < 2; ++axis) {
        const unsigned sm = (unsigned)state.sm[axis];
        pio_sm_config c = pio_get_default_sm_config();
        sm_config_set_wrap(&c, state.offset, state.offset + IQ4PTR_PROGRAM_LENGTH - 1);
        sm_config_set_in_shift(&c, false, false, 32);
        sm_config_set_out_shift(&c, true, false, 32);
        pio_sm_init(pio, sm, state.offset, &c);
        pio_sm_put(pio, sm, base >> 17);
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_pull(false, true));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_mov(pio_y, pio_osr));
        pio_sm_put(pio, sm, iq4ptr_seed(previous[axis]));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_pull(false, true));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_mov(pio_x, pio_osr));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_out(pio_null, 32));
        dma_channel_config d = dma_channel_get_default_config(state.dma[axis]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_write_increment(&d, false);
        channel_config_set_ring(&d, false, input_log2 + 2);
        channel_config_set_dreq(&d, pio_get_dreq(pio, sm, true));
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure(state.dma[axis], &d, &pio->txf[sm], state.input[axis],
                              dma_encode_endless_transfer_count(), false);
    }
    last_error = "prepared";
    return true;
}

bool iq4ptr_start(void) {
    if (!state.owned || state.result.running || state.result.errors) {
        last_error = "start requires a prepared idle cyclic pointer producer";
        return false;
    }
    __dmb();
    dma_start_channel_mask(state.dma_mask);
    const uint32_t start = time_us_32();
    while (!pio_sm_is_tx_fifo_full(state.pio, state.sm[0]) ||
           !pio_sm_is_tx_fifo_full(state.pio, state.sm[1]))
        if ((uint32_t)(time_us_32() - start) >= 1000u)
            return failed("cyclic input DMA did not prefill both TX FIFOs", IQ4PTR_ERR_STARTUP);
    state.pio->fdebug = (state.sm_mask << PIO_FDEBUG_TXSTALL_LSB) |
        (state.sm_mask << PIO_FDEBUG_RXSTALL_LSB) | (state.sm_mask << PIO_FDEBUG_TXOVER_LSB) |
        (state.sm_mask << PIO_FDEBUG_RXUNDER_LSB);
    sample_cursors(false);
    pio_enable_sm_mask_in_sync(state.pio, state.sm_mask);
    state.result.running = true;
    last_error = "running";
    return true;
}

iq4ptr_progress_t iq4ptr_progress(void) {
    if (state.owned && state.result.running) {
        sample_cursors(true);
        sample_faults();
        if (state.result.errors) {
            last_error = "cyclic pointer producer stopped; inspect sticky error flags";
            halt();
        }
    }
    return state.result;
}

const volatile uint32_t *iq4ptr_rx_fifo(unsigned axis) {
    return state.owned && axis < 2 ? &state.pio->rxf[state.sm[axis]] : NULL;
}

unsigned iq4ptr_rx_dreq(unsigned axis) {
    return state.owned && axis < 2 ? pio_get_dreq(state.pio, state.sm[axis], false) : UINT32_MAX;
}

int iq4ptr_input_dma(unsigned axis) { return state.owned && axis < 2 ? state.dma[axis] : -1; }

void iq4ptr_stop(void) {
    if (!state.owned) return;
    if (state.result.running) {
        sample_cursors(false);
        sample_faults();
    }
    halt();
    dma_hw->abort = state.dma_mask;
    const uint32_t start = time_us_32();
    while (dma_hw->abort & state.dma_mask)
        if ((uint32_t)(time_us_32() - start) >= 1000u) {
            state.result.errors |= IQ4PTR_ERR_STOP;
            last_error = "cyclic pointer DMA abort did not retire; claims retained";
            return;
        }
    if (state.added) sample_cursors(false); // outstanding source reads can retire during ABORT
    for (unsigned axis = 0; axis < 2; ++axis) {
        if (state.dma[axis] >= 0) {
            dma_channel_cleanup(state.dma[axis]);
            dma_channel_unclaim(state.dma[axis]);
        }
        if (state.sm[axis] >= 0) {
            pio_sm_clear_fifos(state.pio, state.sm[axis]);
            pio_sm_unclaim(state.pio, state.sm[axis]);
        }
    }
    if (state.added) pio_remove_program(state.pio, &program, state.offset);
    state.owned = false;
}

const char *iq4ptr_last_error(void) { return last_error; }
#endif
