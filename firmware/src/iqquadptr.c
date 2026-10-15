#include "iqquadptr.h"
#include <stddef.h>

#ifdef IQ_ON_DEVICE
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#define RELEASE() __dmb()
#else
#include <stdatomic.h>
#define RELEASE() atomic_thread_fence(memory_order_release)
#endif

uint32_t iqquadptr_key(uint32_t key) {
    return ((key & 15u) << 8) | (key & 0xf0u) | ((key >> 8) & 15u);
}

static const iqquadptr_profile_info_t profiles[] = {
    {IQQUADPTR_PROGRAM, IQQUADPTR_PROGRAM_LENGTH, 37, 128u * 1024u,
     IQQUADPTR_TABLE_CRC, false},
    {IQQUADPTR_L8_PROGRAM, IQQUADPTR_L8_PROGRAM_LENGTH, 49, 64u * 1024u,
     IQQUADPTR_L8_TABLE_CRC, true},
};

const iqquadptr_profile_info_t *iqquadptr_profile_info(iqquad_profile_t profile) {
    if ((unsigned)profile >= sizeof profiles / sizeof profiles[0]) return NULL;
    return &profiles[profile];
}

uint32_t iqquadptr_key_profile(uint32_t key, iqquad_profile_t profile) {
    if (profile == IQQUAD_PROFILE_L9_KAISER) return iqquadptr_key(key);
    if (profile == IQQUAD_PROFILE_L8_OPTIMIZED_BOTH)
        return (key & 7u) | (((key >> 3) & 15u) << 7) | (((key >> 7) & 15u) << 3);
    return UINT32_MAX;
}

uint32_t iqquadptr_seed(uint16_t previous_axis) {
    return ((previous_axis >> 12) & 15u) | ((previous_axis >> 4) & 0xf0u);
}

void iqquadptr_build_table(uint32_t *table) {
    (void)iqquadptr_build_table_profile(table, IQQUAD_WORDS, IQQUAD_PROFILE_L9_KAISER);
}

bool iqquadptr_build_table_profile(uint32_t *table, unsigned word_capacity, iqquad_profile_t profile) {
    if (!iqquadptr_profile_info(profile) || !iqquad_build_profile(table, word_capacity, profile)) return false;
    const iqquad_profile_info_t *info = iqquad_profile_info(profile);
    for (uint32_t key = 0; key < info->rows; ++key) {
        const uint32_t other = iqquadptr_key_profile(key, profile);
        if (key >= other) continue;
        for (unsigned word = 0; word < IQQUAD_ROW_WORDS; ++word) {
            const uint32_t temp = table[key * IQQUAD_ROW_WORDS + word];
            table[key * IQQUAD_ROW_WORDS + word] = table[other * IQQUAD_ROW_WORDS + word];
            table[other * IQQUAD_ROW_WORDS + word] = temp;
        }
    }
    return true;
}

uint64_t iqquadptr_grant_until(volatile uint32_t *grants, uint64_t granted,
                             uint64_t published, uint64_t reusable) {
    const uint64_t released_batches = reusable / IQQUADPTR_BATCH_WORDS;
    while (granted + IQQUADPTR_BATCH_WORDS <= published) {
        const uint64_t batch = granted / IQQUADPTR_BATCH_WORDS;
        // Leave a sentinel entry that neither consumer has reached. A slow axis
        // protects both the data ring and shared metadata against producer wrap.
        if (batch + 1 - released_batches >= IQQUADPTR_GRANT_ENTRIES) break;
        const unsigned slot = (unsigned)batch & (IQQUADPTR_GRANT_ENTRIES - 1u);
        grants[(slot + 1) & (IQQUADPTR_GRANT_ENTRIES - 1u)] = 0;
        RELEASE();
        grants[slot] = IQQUADPTR_BATCH_WORDS;
        RELEASE();
        granted += IQQUADPTR_BATCH_WORDS;
    }
    return granted;
}

#ifdef IQ_ON_DEVICE
typedef struct {
    PIO pio;
    const uint32_t *input;
    int sm[2], input_dma[2], credit_dma[2];
    unsigned offset, words, mask;
    const iqquadptr_profile_info_t *profile;
    uint32_t sm_mask, dma_mask, input_mask, last[2], last_poll_us, poll_limit_us;
    bool owned, added;
    iqquadptr_progress_t result;
} pointer_state_t;

static pointer_state_t state;
static uint32_t credits[IQQUADPTR_GRANT_ENTRIES] __attribute__((aligned(64)));
static const char *last_error = "not prepared";

static void prepare_channel(int channel) {
    dma_channel_cleanup(channel);
    dma_hw->ch[channel].al1_ctrl = DMA_CH0_CTRL_TRIG_READ_ERROR_BITS |
        DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS | (uint32_t)channel << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB;
    for (unsigned irq = 0; irq < NUM_DMA_IRQS; ++irq)
        dma_irqn_set_channel_enabled(irq, channel, false);
    dma_hw->intr = 1u << channel;
}

static void halt(void) {
    if (!state.owned) return;
    pio_set_sm_mask_enabled(state.pio, state.sm_mask, false);
    for (unsigned axis = 0; axis < 2; ++axis) {
        const int channels[] = {state.input_dma[axis], state.credit_dma[axis]};
        for (unsigned i = 0; i < 2; ++i) if (channels[i] >= 0) {
            const unsigned channel = (unsigned)channels[i];
            // Break every chain before abort (RP2350-E5), and stop new requests.
            hw_write_masked(&dma_hw->ch[channel].al1_ctrl,
                            channel << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                            DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS | DMA_CH0_CTRL_TRIG_EN_BITS);
        }
    }
    state.result.running = false;
}

static void sample_cursors(bool check_gap) {
    const uint32_t begin = time_us_32();
    uint32_t cursor[2];
    for (unsigned axis = 0; axis < 2; ++axis) {
        const uintptr_t origin = (uintptr_t)state.input + 2u * axis;
        cursor[axis] = ((dma_hw->ch[state.input_dma[axis]].read_addr - origin) >> 2) & state.mask;
    }
    const uint32_t end = time_us_32();
    if (check_gap && (uint32_t)(end - state.last_poll_us) >= state.poll_limit_us)
        state.result.errors |= IQQUADPTR_ERR_POLL_GAP;
    uint64_t *totals[] = {&state.result.input_i_words, &state.result.input_q_words};
    for (unsigned axis = 0; axis < 2; ++axis) {
        *totals[axis] += (cursor[axis] - state.last[axis]) & state.mask;
        state.last[axis] = cursor[axis];
    }
    const uint64_t i = state.result.input_i_words, q = state.result.input_q_words;
    state.result.input_low_words = i < q ? i : q;
    state.result.input_high_words = i > q ? i : q;
    state.result.reusable_words = state.result.input_low_words > IQQUADPTR_REUSE_GUARD_WORDS ?
        state.result.input_low_words - IQQUADPTR_REUSE_GUARD_WORDS : 0;
    state.last_poll_us = begin;
}

static void sample_faults(void) {
    const uint32_t flags = state.pio->fdebug;
    const uint32_t tx = (flags >> PIO_FDEBUG_TXSTALL_LSB) & state.sm_mask;
    const uint32_t rx = (flags >> PIO_FDEBUG_RXSTALL_LSB) & state.sm_mask;
    state.result.txstall_mask |= tx;
    state.result.rxstall_mask |= rx; // PUSH backpressure is normal; never consume RX here.
    if (tx || (flags & (state.sm_mask << PIO_FDEBUG_TXOVER_LSB)) ||
        (flags & (state.sm_mask << PIO_FDEBUG_RXUNDER_LSB))) state.result.errors |= IQQUADPTR_ERR_PIO;
    const uint32_t intr = dma_hw->intr & state.dma_mask;
    for (unsigned axis = 0; axis < 2; ++axis) {
        const int channels[] = {state.input_dma[axis], state.credit_dma[axis]};
        for (unsigned i = 0; i < 2; ++i)
            if (dma_hw->ch[channels[i]].ctrl_trig & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS)
                state.result.errors |= IQQUADPTR_ERR_DMA;
    }
    if (intr & ~state.input_mask) state.result.errors |= IQQUADPTR_ERR_DMA;
    if (intr & state.input_mask) {
        state.result.null_trigger_mask |= intr & state.input_mask;
        state.result.errors |= IQQUADPTR_ERR_SOURCE_EMPTY;
    }
    if ((state.pio->ctrl & state.sm_mask) != state.sm_mask) state.result.errors |= IQQUADPTR_ERR_PIO;
    if (state.result.input_high_words > state.result.granted_words)
        state.result.errors |= IQQUADPTR_ERR_OWNERSHIP;
}

static bool failed(const char *error, uint32_t flag) {
    last_error = error;
    state.result.errors |= flag;
    iqquadptr_stop();
    return false;
}

bool iqquadptr_prepare(PIO pio, const uint32_t *input, unsigned input_log2,
                      const uint32_t *table, uint32_t previous, uint64_t published_words) {
    return iqquadptr_prepare_profile(pio, input, input_log2, table, previous,
                                    published_words, IQQUAD_PROFILE_L9_KAISER);
}

bool iqquadptr_prepare_profile(PIO pio, const uint32_t *input, unsigned input_log2,
                              const uint32_t *table, uint32_t previous, uint64_t published_words,
                              iqquad_profile_t profile) {
    if (state.owned) { last_error = "pointer producer is already owned"; return false; }
    state = (pointer_state_t){.pio = pio, .input = input, .sm = {-1, -1},
        .input_dma = {-1, -1}, .credit_dma = {-1, -1}, .profile = iqquadptr_profile_info(profile)};
    const uintptr_t base = (uintptr_t)table;
    if (!state.profile || (pio != pio0 && pio != pio1 && pio != pio2) ||
        !input || !table || input_log2 < 10 || input_log2 > 13 ||
        ((uintptr_t)input & ((4u << input_log2) - 1u)) || (base & (state.profile->table_alignment - 1u)) ||
        base < 0x20000000u || base > 0x20080000u - state.profile->table_alignment)
        return failed("raw input needs a known profile, aligned1K..8K-word ring and profile-aligned SRAM table",
                      IQQUADPTR_ERR_CONFIG);
    state.words = 1u << input_log2;
    state.mask = state.words - 1;
    if (published_words < IQQUADPTR_BATCH_WORDS || published_words > state.words)
        return failed("startup publication must contain at least one512-word batch within the ring", IQQUADPTR_ERR_CONFIG);
    state.result.published_words = published_words;
    // Profile minimum PIO cycles/input word, less an8-word burst/retirement guard.
    state.poll_limit_us = (uint32_t)((uint64_t)(state.words - IQQUADPTR_REUSE_GUARD_WORDS) *
        state.profile->cycles_per_input_word * 1000000u / clock_get_hz(clk_sys));
    for (unsigned i = 0; i < IQQUADPTR_GRANT_ENTRIES; ++i) credits[i] = 0;
    state.result.granted_words = iqquadptr_grant_until(credits, 0, published_words, 0);
    const pio_program_t program = {.instructions = state.profile->program,
        .length = state.profile->program_length, .origin = -1};
    if (!pio_can_add_program(pio, &program)) return failed("pointer PIO instruction space unavailable", IQQUADPTR_ERR_CONFIG);
    state.owned = true;
    for (unsigned axis = 0; axis < 2; ++axis) {
        state.sm[axis] = pio_claim_unused_sm(pio, false);
        if (state.sm[axis] < 0) return failed("two pointer SMs unavailable", IQQUADPTR_ERR_CONFIG);
        state.sm_mask |= 1u << state.sm[axis];
        state.input_dma[axis] = dma_claim_unused_channel(false);
        if (state.input_dma[axis] < 0) return failed("pointer input DMA unavailable", IQQUADPTR_ERR_CONFIG);
        prepare_channel(state.input_dma[axis]);
        state.dma_mask |= 1u << state.input_dma[axis];
        state.input_mask |= 1u << state.input_dma[axis];
        state.credit_dma[axis] = dma_claim_unused_channel(false);
        if (state.credit_dma[axis] < 0) return failed("pointer credit DMA unavailable", IQQUADPTR_ERR_CONFIG);
        prepare_channel(state.credit_dma[axis]);
        state.dma_mask |= 1u << state.credit_dma[axis];
    }
    state.offset = pio_add_program(pio, &program);
    state.added = true;
    for (unsigned axis = 0; axis < 2; ++axis) {
        const unsigned sm = (unsigned)state.sm[axis];
        pio_sm_config c = pio_get_default_sm_config();
        sm_config_set_wrap(&c, state.offset, state.offset + state.profile->program_length - 1);
        sm_config_set_in_shift(&c, false, false, 32);
        sm_config_set_out_shift(&c, true, false, 16);
        pio_sm_init(pio, sm, state.offset, &c);
        uint32_t y = base >> 17;
        if (state.profile->control_bswap) {
            const uint32_t upper = base >> 16;
            y = ((upper & 255u) << 8) | (upper >> 8);
        }
        pio_sm_put(pio, sm, y);
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_pull(false, true));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_mov(pio_y, pio_osr));
        pio_sm_put(pio, sm, iqquadptr_seed((uint16_t)(previous >> (16 * axis))));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_pull(false, true));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_mov(pio_x, pio_osr));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_out(pio_null, 32));
        dma_channel_config d = dma_channel_get_default_config(state.input_dma[axis]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_16);
        // INCR_READ=0/REV=1 increments by twice the16-bit transfer size.
        channel_config_set_read_increment(&d, false);
        d.ctrl |= DMA_CH0_CTRL_TRIG_INCR_READ_REV_BITS;
        channel_config_set_write_increment(&d, false);
        channel_config_set_ring(&d, false, input_log2 + 2);
        channel_config_set_dreq(&d, pio_get_dreq(pio, sm, true));
        channel_config_set_chain_to(&d, state.credit_dma[axis]);
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure(state.input_dma[axis], &d, &pio->txf[sm],
            (const uint8_t *)input + 2 * axis, 0, false);
        d = dma_channel_get_default_config(state.credit_dma[axis]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_write_increment(&d, false);
        channel_config_set_ring(&d, false, 6); //16 entries ×4bytes, shared immutable credits
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure(state.credit_dma[axis], &d,
            &dma_hw->ch[state.input_dma[axis]].al1_transfer_count_trig, credits, 1, false);
    }
    last_error = "prepared";
    return true;
}

bool iqquadptr_start(void) {
    if (!state.owned || state.result.running || state.result.errors) {
        last_error = "start requires a prepared idle pointer producer";
        return false;
    }
    uint32_t controls = 0;
    for (unsigned axis = 0; axis < 2; ++axis) controls |= 1u << state.credit_dma[axis];
    RELEASE();
    dma_start_channel_mask(controls);
    const uint32_t start = time_us_32();
    while (!pio_sm_is_tx_fifo_full(state.pio, state.sm[0]) ||
           !pio_sm_is_tx_fifo_full(state.pio, state.sm[1])) {
        if ((uint32_t)(time_us_32() - start) >= 1000u)
            return failed("pointer input DMA did not prefill both TX FIFOs", IQQUADPTR_ERR_STARTUP);
    }
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

iqquadptr_progress_t iqquadptr_progress(void) {
    if (state.owned && state.result.running) {
        sample_cursors(true);
        sample_faults();
        if (state.result.errors) {
            last_error = "pointer producer stopped; inspect sticky input/DMA/PIO error flags";
            halt();
        }
    }
    return state.result;
}

iqquadptr_progress_t iqquadptr_publish(uint64_t published_words) {
    iqquadptr_progress();
    if (!state.owned || !state.result.running) return state.result;
    if (published_words < state.result.published_words ||
        published_words < state.result.reusable_words ||
        published_words - state.result.reusable_words > state.words) {
        state.result.errors |= IQQUADPTR_ERR_OWNERSHIP;
        last_error = "publication decreases or exceeds the guarded input ring";
        halt();
        return state.result;
    }
    RELEASE(); // acquire the caller's completed immutable source words
    state.result.published_words = published_words;
    state.result.granted_words = iqquadptr_grant_until(credits, state.result.granted_words,
        published_words, state.result.reusable_words);
    return state.result;
}

const volatile uint32_t *iqquadptr_rx_fifo(unsigned axis) {
    return state.owned && axis < 2 ? &state.pio->rxf[state.sm[axis]] : NULL;
}

unsigned iqquadptr_rx_dreq(unsigned axis) {
    return state.owned && axis < 2 ? pio_get_dreq(state.pio, state.sm[axis], false) : UINT32_MAX;
}

void iqquadptr_stop(void) {
    if (!state.owned) return;
    if (state.result.running) {
        sample_cursors(false);
        sample_faults(); // include the last active interval before halt disables the SMs
    }
    halt();
    dma_hw->abort = state.dma_mask;
    const uint32_t start = time_us_32();
    while (dma_hw->abort & state.dma_mask) {
        if ((uint32_t)(time_us_32() - start) >= 1000u) {
            state.result.errors |= IQQUADPTR_ERR_STOP;
            last_error = "pointer DMA abort did not retire; claims retained";
            return;
        }
    }
    if (state.added) {
        // ABORT can retire an outstanding source read. Latch its final cursor
        // while channels are still owned; intentionally halted SMs are not faults.
        sample_cursors(false);
        if (state.result.input_high_words > state.result.granted_words ||
            state.result.input_high_words > state.result.published_words)
            state.result.errors |= IQQUADPTR_ERR_OWNERSHIP;
    }
    for (unsigned axis = 0; axis < 2; ++axis) {
        const int channels[] = {state.input_dma[axis], state.credit_dma[axis]};
        for (unsigned i = 0; i < 2; ++i) if (channels[i] >= 0) {
            dma_channel_cleanup(channels[i]);
            dma_channel_unclaim(channels[i]);
        }
        if (state.sm[axis] >= 0) {
            pio_sm_clear_fifos(state.pio, state.sm[axis]);
            pio_sm_unclaim(state.pio, state.sm[axis]);
        }
    }
    if (state.added) {
        const pio_program_t program = {.instructions = state.profile->program,
            .length = state.profile->program_length, .origin = -1};
        pio_remove_program(state.pio, &program, state.offset);
    }
    state.owned = false;
}

const char *iqquadptr_last_error(void) { return last_error; }
#endif
