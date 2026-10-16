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
#include "iqquadptr.h"
#include "pico/stdlib.h"

typedef struct {
    PIO pio;
    const uint32_t *input[2];
    int sm[2], dma[2], credit_dma[2];
    unsigned offset, mask;
    uint32_t sm_mask, dma_mask, input_mask, last[2], last_poll_us, poll_limit_us;
    bool owned, added, live;
    iq4ptr_progress_t result;
} pointer_state_t;

static pointer_state_t state;
// Live grants use the iqquadptr leaf and batch size; separate storage, same ring rule.
// SRAM4-7 (NOLOAD, lower SRAM is full): prepare() zeroes every entry before any DMA use.
static uint32_t credits[IQQUADPTR_GRANT_ENTRIES] __attribute__((aligned(64), section(".sram_hi")));
static const char *last_error = "not prepared";
static const pio_program_t program = {
    .instructions = IQ4PTR_PROGRAM, .length = IQ4PTR_PROGRAM_LENGTH, .origin = -1,
};

static void halt(void) {
    if (!state.owned) return;
    pio_set_sm_mask_enabled(state.pio, state.sm_mask, false);
    for (unsigned axis = 0; axis < 2; ++axis) {
        const int channels[] = {state.dma[axis], state.credit_dma[axis]};
        for (unsigned i = 0; i < 2; ++i) if (channels[i] >= 0) {
            const unsigned ch = (unsigned)channels[i];
            // Break chains before abort (RP2350-E5), and stop new DREQs.
            hw_write_masked(&dma_hw->ch[ch].al1_ctrl, ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                            DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS | DMA_CH0_CTRL_TRIG_EN_BITS);
        }
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
    const uint64_t i = state.result.input_words[0], q = state.result.input_words[1];
    const uint64_t low = i < q ? i : q;
    state.result.reusable_words = low > IQQUADPTR_REUSE_GUARD_WORDS ? low - IQQUADPTR_REUSE_GUARD_WORDS : 0;
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
    for (unsigned axis = 0; axis < 2; ++axis) {
        const int channels[] = {state.dma[axis], state.credit_dma[axis]};
        for (unsigned i = 0; i < 2; ++i)
            if (channels[i] >= 0 && (dma_hw->ch[channels[i]].ctrl_trig & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS))
                state.result.errors |= IQ4PTR_ERR_DMA;
    }
    if (!state.live) return;
    // IRQ_QUIET channels raise INTR only on a null trigger: an input DMA reloaded with a
    // zero sentinel because publication fell behind. Any other owned INTR is a DMA fault.
    const uint32_t intr = dma_hw->intr & state.dma_mask;
    if (intr & ~state.input_mask) state.result.errors |= IQ4PTR_ERR_DMA;
    if (intr & state.input_mask) {
        state.result.null_trigger_mask |= intr & state.input_mask;
        state.result.errors |= IQ4PTR_ERR_SOURCE_EMPTY;
    }
    const uint64_t i = state.result.input_words[0], q = state.result.input_words[1];
    if ((i > q ? i : q) > state.result.granted_words) state.result.errors |= IQ4PTR_ERR_OWNERSHIP;
}

static bool failed(const char *error, uint32_t flag) {
    last_error = error;
    state.result.errors |= flag;
    iq4ptr_stop();
    return false;
}

static void prepare_channel(int channel) {
    const unsigned ch = (unsigned)channel;
    dma_channel_cleanup(ch);
    dma_hw->ch[ch].al1_ctrl = DMA_CH0_CTRL_TRIG_READ_ERROR_BITS |
        DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS | ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB;
    for (unsigned irq = 0; irq < NUM_DMA_IRQS; ++irq)
        dma_irqn_set_channel_enabled(irq, ch, false);
    dma_hw->intr = 1u << ch;
}

static bool prepare(PIO pio, const uint32_t *input_i, const uint32_t *input_q,
                    unsigned input_log2, const uint16_t *table,
                    uint32_t previous_i, uint32_t previous_q, bool live, uint64_t published_words) {
    if (state.owned) { last_error = "pointer producer is already owned"; return false; }
    state = (pointer_state_t){.pio = pio, .input = {input_i, input_q}, .live = live,
        .sm = {-1, -1}, .dma = {-1, -1}, .credit_dma = {-1, -1}};
    const uintptr_t base = (uintptr_t)table;
    if ((pio != pio0 && pio != pio1 && pio != pio2) || !input_i || !input_q || !table ||
        input_log2 < 8 || input_log2 > 13 ||
        ((uintptr_t)input_i & ((4u << input_log2) - 1u)) ||
        ((uintptr_t)input_q & ((4u << input_log2) - 1u)) ||
        (base & (IQ4PTR_TABLE_BYTES - 1u)) ||
        base < 0x20000000u || base > 0x20080000u - IQ4PTR_TABLE_BYTES)
        return failed("pointer input needs aligned256..8192-word rings and128KiB-aligned SRAM table",
                      IQ4PTR_ERR_CONFIG);
    state.mask = (1u << input_log2) - 1u;
    if (live && (published_words < IQQUADPTR_BATCH_WORDS || published_words > state.mask + 1u))
        return failed("live startup publication must hold one512-word batch within the ring",
                      IQ4PTR_ERR_CONFIG);
    // Maximum issue rate follows the PIO program, even with an unpaced consumer.
    // Reserve eight words for DMA issue/prefetch and timestamp observation.
    state.poll_limit_us = (uint64_t)(state.mask + 1u - 8u) * IQ4PTR_CYCLES_PER_WORD *
        1000000u / clock_get_hz(clk_sys);
    if (!pio_can_add_program(pio, &program))
        return failed("pointer PIO instruction space unavailable", IQ4PTR_ERR_CONFIG);
    if (live) {
        for (unsigned i = 0; i < IQQUADPTR_GRANT_ENTRIES; ++i) credits[i] = 0;
        state.result.published_words = published_words;
        state.result.granted_words = iqquadptr_grant_until(credits, 0, published_words, 0);
    }
    state.owned = true;
    for (unsigned axis = 0; axis < 2; ++axis) {
        state.sm[axis] = pio_claim_unused_sm(pio, false);
        if (state.sm[axis] < 0) return failed("two pointer SMs unavailable", IQ4PTR_ERR_CONFIG);
        state.sm_mask |= 1u << state.sm[axis];
        state.dma[axis] = dma_claim_unused_channel(false);
        if (state.dma[axis] < 0) return failed("pointer input DMA unavailable", IQ4PTR_ERR_CONFIG);
        prepare_channel(state.dma[axis]);
        state.dma_mask |= 1u << state.dma[axis];
        state.input_mask |= 1u << state.dma[axis];
        if (!live) continue;
        state.credit_dma[axis] = dma_claim_unused_channel(false);
        if (state.credit_dma[axis] < 0) return failed("pointer credit DMA unavailable", IQ4PTR_ERR_CONFIG);
        prepare_channel(state.credit_dma[axis]);
        state.dma_mask |= 1u << state.credit_dma[axis];
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
        if (!live) {
            dma_channel_configure(state.dma[axis], &d, &pio->txf[sm], state.input[axis],
                                  dma_encode_endless_transfer_count(), false);
            continue;
        }
        // Each finished grant chains to the credit channel, which loads the next grant
        // into TRANS_COUNT_TRIG. A zero sentinel is a null trigger: SOURCE_EMPTY.
        channel_config_set_chain_to(&d, state.credit_dma[axis]);
        dma_channel_configure(state.dma[axis], &d, &pio->txf[sm], state.input[axis], 0, false);
        d = dma_channel_get_default_config(state.credit_dma[axis]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_write_increment(&d, false);
        channel_config_set_ring(&d, false, 6); // 16 entries x 4 bytes, shared immutable grants
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure(state.credit_dma[axis], &d,
            &dma_hw->ch[state.dma[axis]].al1_transfer_count_trig, credits, 1, false);
    }
    last_error = "prepared";
    return true;
}

bool iq4ptr_prepare(PIO pio, const uint32_t *input_i, const uint32_t *input_q,
                   unsigned input_log2, const uint16_t *table,
                   uint32_t previous_i, uint32_t previous_q) {
    return prepare(pio, input_i, input_q, input_log2, table, previous_i, previous_q, false, 0);
}

bool iq4ptr_prepare_live(PIO pio, const uint32_t *input_i, const uint32_t *input_q,
                        unsigned input_log2, const uint16_t *table,
                        uint32_t previous_i, uint32_t previous_q, uint64_t published_words) {
    return prepare(pio, input_i, input_q, input_log2, table, previous_i, previous_q, true,
                   published_words);
}

bool iq4ptr_start(void) {
    if (!state.owned || state.result.running || state.result.errors) {
        last_error = "start requires a prepared idle pointer producer";
        return false;
    }
    uint32_t start_mask = state.input_mask;
    if (state.live) start_mask = (1u << state.credit_dma[0]) | (1u << state.credit_dma[1]);
    __dmb();
    dma_start_channel_mask(start_mask);
    const uint32_t start = time_us_32();
    while (!pio_sm_is_tx_fifo_full(state.pio, state.sm[0]) ||
           !pio_sm_is_tx_fifo_full(state.pio, state.sm[1]))
        if ((uint32_t)(time_us_32() - start) >= 1000u)
            return failed("pointer input DMA did not prefill both TX FIFOs", IQ4PTR_ERR_STARTUP);
    state.pio->fdebug = (state.sm_mask << PIO_FDEBUG_TXSTALL_LSB) |
        (state.sm_mask << PIO_FDEBUG_RXSTALL_LSB) | (state.sm_mask << PIO_FDEBUG_TXOVER_LSB) |
        (state.sm_mask << PIO_FDEBUG_RXUNDER_LSB);
    state.last_poll_us = time_us_32();
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
            last_error = "pointer producer stopped; inspect sticky error flags";
            halt();
        }
    }
    return state.result;
}

iq4ptr_progress_t iq4ptr_publish(uint64_t published_words) {
    iq4ptr_progress();
    if (!state.owned || !state.result.running || !state.live) return state.result;
    if (published_words < state.result.published_words ||
        published_words < state.result.reusable_words ||
        published_words - state.result.reusable_words > state.mask + 1u) {
        state.result.errors |= IQ4PTR_ERR_OWNERSHIP;
        last_error = "publication decreases or exceeds the guarded input rings";
        halt();
        return state.result;
    }
    __dmb(); // acquire the caller's completed immutable code words before granting them
    state.result.published_words = published_words;
    state.result.granted_words = iqquadptr_grant_until(credits, state.result.granted_words,
        published_words, state.result.reusable_words);
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
            last_error = "pointer DMA abort did not retire; claims retained";
            return;
        }
    if (state.added) {
        sample_cursors(false); // outstanding source reads can retire during ABORT
        const uint64_t i = state.result.input_words[0], q = state.result.input_words[1];
        if (state.live && ((i > q ? i : q) > state.result.granted_words ||
                           (i > q ? i : q) > state.result.published_words))
            state.result.errors |= IQ4PTR_ERR_OWNERSHIP;
    }
    for (unsigned axis = 0; axis < 2; ++axis) {
        const int channels[] = {state.dma[axis], state.credit_dma[axis]};
        for (unsigned i = 0; i < 2; ++i) if (channels[i] >= 0) {
            dma_channel_cleanup(channels[i]);
            dma_channel_unclaim(channels[i]);
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
