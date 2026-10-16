#include "iq4prod_dma.h"
#include "iq4prod.h"
#include "tx.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include <limits.h>
#include <stddef.h>

enum { WORKER_POLL_CYCLES = 64, FAULT_POLL_ROUNDS = 32 };
typedef struct {
    uint32_t completed[4], granted[4], errors, txstall, rxstall;
    uint32_t polls, max_gap;
    uint64_t service_cycles;
} worker_snapshot_t;
typedef struct {
    PIO pio[4];
    const uint32_t *input[4];
    int sm[4], dma[4];
    unsigned offset[2];
    uint32_t sm_mask[2], dma_mask, last[4], last_poll_us;
    bool owned, added[2], live, worker_started;
    volatile bool worker_stop, worker_ready;
    volatile uint32_t published, snapshot_seq;
    volatile worker_snapshot_t snapshot;
    iq4prod_progress_t result;
} producer_state_t;

// Core0's stack is relocated to SRAM_HI in the experiment build. Its unused
// SRAM9 space holds this worker and state without expanding crowded SRAM0..7.
static producer_state_t state __attribute__((section(".scratch_y.iq4prod_state"))) = {0};
static const char *last_error = "not prepared";
static const pio_program_t program = {
    .instructions = IQ4PROD_PROGRAM, .length = IQ4PROD_PROGRAM_LENGTH, .origin = -1,
};

static void disable_producers(void) {
    for (unsigned block = 0; block < 2; ++block)
        // Core0 can stop the DDR clock/capture concurrently with a worker fault.
        // An atomic clear cannot restore an enable bit from a stale CTRL read.
        hw_clear_bits(&(block ? pio1 : pio0)->ctrl, state.sm_mask[block]);
}

static inline __attribute__((always_inline)) void store_snapshot(const worker_snapshot_t *s) {
    state.snapshot_seq++;
    __dmb();
    state.snapshot = *s;
    __dmb();
    state.snapshot_seq++;
}

static worker_snapshot_t load_snapshot(void) {
    worker_snapshot_t s;
    uint32_t before, after;
    do {
        before = state.snapshot_seq;
        __dmb();
        s = state.snapshot;
        __dmb();
        after = state.snapshot_seq;
    } while ((before & 1u) || before != after);
    return s;
}

static void __attribute__((section(".scratch_y.iq4prod_worker"))) input_worker(uint32_t ignored) {
    (void)ignored;
    const uint32_t saved = save_and_disable_interrupts();
    worker_snapshot_t s = {0};
    for (unsigned stream = 0; stream < 4; ++stream) s.granted[stream] = IQ4PROD_BATCH_WORDS;
    store_snapshot(&s);
    uint32_t last = cycles(), rounds = 0;
    state.worker_ready = true;
    __dmb();
    while (!state.worker_stop) {
        const uint32_t now = cycles(), gap = now - last;
        if (gap < WORKER_POLL_CYCLES) continue;
        last = now;
        if (gap > s.max_gap) s.max_gap = gap;
        const uint32_t begin = now;
        s.polls++;
        bool changed = false;
        const uint32_t published = state.published;
        __dmb(); // publication follows every source store on core0
        for (unsigned stream = 0; stream < 4; ++stream) {
            const unsigned ch = (unsigned)state.dma[stream];
            const uint32_t ctrl = dma_hw->ch[ch].ctrl_trig;
            if (ctrl & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS) s.errors |= IQ4PROD_ERR_DMA;
            if (ctrl & DMA_CH0_CTRL_TRIG_BUSY_BITS) continue;
            if (s.completed[stream] != s.granted[stream]) {
                // BUSY clears only after the final write completes. All source
                // reads for this chunk have retired before it becomes reusable.
                s.completed[stream] = s.granted[stream];
                changed = true;
            }
            if (published < s.granted[stream]) { s.errors |= IQ4PROD_ERR_OWNERSHIP; continue; }
            if (published - s.granted[stream] < IQ4PROD_BATCH_WORDS) continue;
            s.granted[stream] += IQ4PROD_BATCH_WORDS;
            __dmb();
            dma_hw->ch[ch].al1_transfer_count_trig = IQ4PROD_BATCH_WORDS;
            changed = true;
        }
        if (++rounds == FAULT_POLL_ROUNDS) {
            rounds = 0;
            for (unsigned block = 0; block < 2; ++block) {
                const uint32_t flags = (block ? pio1 : pio0)->fdebug;
                const uint32_t mask = state.sm_mask[block];
                const uint32_t tx = (flags >> PIO_FDEBUG_TXSTALL_LSB) & mask;
                const uint32_t rx = (flags >> PIO_FDEBUG_RXSTALL_LSB) & mask;
                s.txstall |= tx << (4 * block); s.rxstall |= rx << (4 * block);
                if (tx) s.errors |= IQ4PROD_ERR_SOURCE_EMPTY;
                if (flags & ((mask << PIO_FDEBUG_TXOVER_LSB) |
                             (mask << PIO_FDEBUG_RXUNDER_LSB))) s.errors |= IQ4PROD_ERR_PIO;
            }
            changed = true;
        }
        s.service_cycles += cycles() - begin;
        if (s.errors) {
            disable_producers();
            break;
        }
        if (changed) store_snapshot(&s);
    }
    store_snapshot(&s);
    restore_interrupts(saved);
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

static bool failed(const char *error, uint32_t flag) {
    last_error = error;
    state.result.errors |= flag;
    iq4prod_dma_stop();
    return false;
}

bool iq4prod_dma_prepare(const uint32_t *input_i, const uint32_t *input_q,
                     const uint32_t *table, uint32_t previous_i, uint32_t previous_q,
                     bool live, uint64_t published_words) {
    if (state.owned) { last_error = "pointer producer is already owned"; return false; }
    state = (producer_state_t){.pio = {pio0, pio0, pio0, pio1},
        .input = {input_i, input_q, input_i, input_q},
        .sm = {-1, -1, -1, -1}, .dma = {-1, -1, -1, -1}, .live = live};
    const uintptr_t base = (uintptr_t)table;
    if (!input_i || !input_q || !table ||
        ((uintptr_t)input_i & (IQ4PROD_INPUT_WORDS * 4 - 1u)) ||
        ((uintptr_t)input_q & (IQ4PROD_INPUT_WORDS * 4 - 1u)) ||
        (base & (IQ4PROD_TABLE_BYTES - 1u)) ||
        base < 0x20000000u || base > 0x20080000u - IQ4PROD_TABLE_BYTES ||
        (live && (published_words < IQ4PROD_BATCH_WORDS || published_words > IQ4PROD_INPUT_WORDS)))
        return failed("producer requires aligned4096-word rings,64KiB table and valid publication",
                      IQ4PROD_ERR_CONFIG);
    state.owned = true;
    state.published = (uint32_t)published_words;
    state.result.published_words = published_words;
    for (unsigned block = 0; block < 2; ++block) {
        PIO pio = block ? pio1 : pio0;
        if (!pio_can_add_program(pio, &program))
            return failed("producer PIO instruction memory unavailable", IQ4PROD_ERR_CONFIG);
        state.offset[block] = pio_add_program(pio, &program);
        state.added[block] = true;
    }
    const uint32_t previous[] = {previous_i, previous_q};
    for (unsigned stream = 0; stream < 4; ++stream) {
        PIO pio = state.pio[stream];
        const unsigned block = stream == 3;
        state.sm[stream] = pio_claim_unused_sm(pio, false);
        if (state.sm[stream] >= 0) state.sm_mask[block] |= 1u << state.sm[stream];
        state.dma[stream] = dma_claim_unused_channel(false);
        if (state.dma[stream] >= 0) {
            state.dma_mask |= 1u << state.dma[stream];
            prepare_channel(state.dma[stream]);
        }
        if (state.sm[stream] < 0 || state.dma[stream] < 0)
            return failed("producer PIO SM or input DMA unavailable", IQ4PROD_ERR_CONFIG);
        const unsigned sm = (unsigned)state.sm[stream];
        pio_sm_config c = pio_get_default_sm_config();
        const unsigned offset = state.offset[block];
        sm_config_set_wrap(&c, offset, offset + IQ4PROD_PROGRAM_LENGTH - 1);
        sm_config_set_in_shift(&c, false, false, 32);
        sm_config_set_out_shift(&c, true, false, 32);
        pio_sm_init(pio, sm, offset + iq4prod_entry(stream >> 1), &c);
        pio_sm_put(pio, sm, base >> 16);
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_pull(false, true));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_mov(pio_y, pio_osr));
        pio_sm_put(pio, sm, iq4prod_seed(previous[stream & 1]));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_pull(false, true));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_mov(pio_x, pio_osr));
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_out(pio_null, 32));
        sm_config_set_in_shift(&c, false, true, 16);
        sm_config_set_out_shift(&c, true, true, 32);
        pio_sm_set_config(pio, sm, &c);
        dma_channel_config d = dma_channel_get_default_config(state.dma[stream]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_write_increment(&d, false);
        channel_config_set_ring(&d, false, 14);
        channel_config_set_dreq(&d, pio_get_dreq(pio, sm, true));
        channel_config_set_irq_quiet(&d, true);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure(state.dma[stream], &d, &pio->txf[sm], state.input[stream],
            live ? IQ4PROD_BATCH_WORDS : dma_encode_endless_transfer_count(), false);
    }
    last_error = "prepared";
    return true;
}

bool iq4prod_dma_start(void) {
    if (!state.owned || state.result.running || state.result.errors)
        return failed("start requires prepared idle producers", IQ4PROD_ERR_CONFIG);
    __dmb();
    dma_start_channel_mask(state.dma_mask);
    const uint32_t start = time_us_32();
    for (unsigned stream = 0; stream < 4; ++stream) {
        PIO pio = state.pio[stream];
        const unsigned sm = (unsigned)state.sm[stream];
        while (!pio_sm_is_tx_fifo_full(pio, sm))
            if ((uint32_t)(time_us_32() - start) >= 1000u)
                return failed("producer input DMA did not prefill TX FIFOs", IQ4PROD_ERR_STARTUP);
        pio_sm_exec_wait_blocking(pio, sm, pio_encode_pull(false, true));
    }
    for (unsigned block = 0; block < 2; ++block) {
        PIO pio = block ? pio1 : pio0;
        const uint32_t mask = state.sm_mask[block];
        pio->fdebug = (mask << PIO_FDEBUG_TXSTALL_LSB) | (mask << PIO_FDEBUG_RXSTALL_LSB) |
                     (mask << PIO_FDEBUG_TXOVER_LSB) | (mask << PIO_FDEBUG_RXUNDER_LSB);
    }
    if (state.live) {
        state.worker_started = true;
        tx_worker_start(input_worker, 0);
        while (!state.worker_ready)
            if ((uint32_t)(time_us_32() - start) >= 10000u)
                return failed("producer input worker did not start", IQ4PROD_ERR_STARTUP);
    }
    pio_enable_sm_mask_in_sync(pio0, state.sm_mask[0]);
    pio_enable_sm_mask_in_sync(pio1, state.sm_mask[1]);
    state.last_poll_us = time_us_32();
    state.result.running = true;
    last_error = "running";
    return true;
}

iq4prod_progress_t iq4prod_dma_progress(void) {
    if (!state.owned) return state.result;
    if (state.live) {
        const worker_snapshot_t s = load_snapshot();
        uint32_t low = UINT32_MAX, high = 0;
        for (unsigned stream = 0; stream < 4; ++stream) {
            state.result.input_words[stream] = s.completed[stream];
            state.result.granted_input_words[stream] = s.granted[stream];
            if (s.completed[stream] < low) low = s.completed[stream];
            if (s.granted[stream] > high) high = s.granted[stream];
        }
        state.result.reusable_words = low;
        state.result.granted_words = high;
        state.result.errors |= s.errors;
        state.result.txstall_mask |= s.txstall;
        state.result.rxstall_mask |= s.rxstall;
        state.result.worker_polls = s.polls;
        state.result.worker_max_gap_cycles = s.max_gap;
        state.result.worker_service_cycles = s.service_cycles;
    } else {
        if (!state.result.running) return state.result;
        const uint32_t now = time_us_32();
        // The producer's unbackpressured maximum is16codes/88SYS cycles.
        // A missed full-ring observation makes wrapping cursors ambiguous.
        const uint32_t limit = (uint64_t)(IQ4PROD_INPUT_WORDS - 8) *
            IQ4PROD_CYCLES_PER_WORD * 1000000u / clock_get_hz(clk_sys);
        if ((uint32_t)(now - state.last_poll_us) >= limit)
            state.result.errors |= IQ4PROD_ERR_OWNERSHIP;
        state.last_poll_us = now;
        for (unsigned stream = 0; stream < 4; ++stream) {
            const uint32_t address = dma_hw->ch[state.dma[stream]].read_addr;
            const uint32_t cursor = ((address - (uintptr_t)state.input[stream]) >> 2) &
                (IQ4PROD_INPUT_WORDS - 1u);
            state.result.input_words[stream] += (cursor - state.last[stream]) &
                (IQ4PROD_INPUT_WORDS - 1u);
            state.last[stream] = cursor;
        }
        for (unsigned block = 0; block < 2; ++block) {
            const uint32_t flags = (block ? pio1 : pio0)->fdebug;
            const uint32_t mask = state.sm_mask[block];
            const uint32_t tx = (flags >> PIO_FDEBUG_TXSTALL_LSB) & mask;
            state.result.txstall_mask |= tx << (4 * block);
            state.result.rxstall_mask |= ((flags >> PIO_FDEBUG_RXSTALL_LSB) & mask) << (4 * block);
            if (tx || (flags & ((mask << PIO_FDEBUG_TXOVER_LSB) |
                               (mask << PIO_FDEBUG_RXUNDER_LSB)))) state.result.errors |= IQ4PROD_ERR_PIO;
        }
        for (unsigned stream = 0; stream < 4; ++stream)
            if (dma_hw->ch[state.dma[stream]].ctrl_trig & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS)
                state.result.errors |= IQ4PROD_ERR_DMA;
    }
    if (state.result.errors) {
        disable_producers();
        state.result.running = false;
        last_error = "producer fault; inspect sticky flags";
    }
    return state.result;
}

iq4prod_progress_t iq4prod_dma_publish(uint64_t published_words) {
    iq4prod_dma_progress();
    if (!state.owned || !state.live || !state.result.running) return state.result;
    if (published_words > UINT32_MAX || published_words < state.result.published_words ||
        published_words - state.result.reusable_words > IQ4PROD_INPUT_WORDS) {
        state.result.errors |= IQ4PROD_ERR_OWNERSHIP;
        state.result.running = false;
        state.worker_stop = true;
        disable_producers();
        last_error = "publication decreases or exceeds completed input ownership";
        return state.result;
    }
    __dmb();
    state.published = (uint32_t)published_words;
    state.result.published_words = published_words;
    return state.result;
}

const volatile uint32_t *iq4prod_dma_rx_fifo(unsigned stream) {
    return state.owned && stream < 4 ? &state.pio[stream]->rxf[state.sm[stream]] : NULL;
}

unsigned iq4prod_dma_rx_dreq(unsigned stream) {
    return state.owned && stream < 4 ? pio_get_dreq(state.pio[stream], state.sm[stream], false) : UINT32_MAX;
}

int iq4prod_dma_pio_index(unsigned stream) { return stream < 4 ? (stream == 3 ? 1 : 0) : -1; }
int iq4prod_dma_sm(unsigned stream) { return state.owned && stream < 4 ? state.sm[stream] : -1; }

void iq4prod_dma_stop(void) {
    if (!state.owned) return;
    if (state.worker_started) {
        state.worker_stop = true;
        __dmb();
        const uint32_t start = time_us_32();
        while (!multicore_fifo_rvalid())
            if ((uint32_t)(time_us_32() - start) >= 1000u) {
                disable_producers();
                state.result.running = false;
                state.result.errors |= IQ4PROD_ERR_STOP;
                last_error = "producer worker did not join; claims retained";
                return;
            }
        tx_worker_wait();
        state.worker_started = false;
    }
    iq4prod_dma_progress();
    disable_producers();
    state.result.running = false;
    for (unsigned stream = 0; stream < 4; ++stream) if (state.dma[stream] >= 0) {
        const unsigned ch = (unsigned)state.dma[stream];
        hw_write_masked(&dma_hw->ch[ch].al1_ctrl, ch << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                        DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS | DMA_CH0_CTRL_TRIG_EN_BITS);
    }
    dma_hw->abort = state.dma_mask;
    const uint32_t start = time_us_32();
    while (dma_hw->abort & state.dma_mask)
        if ((uint32_t)(time_us_32() - start) >= 1000u) {
            state.result.errors |= IQ4PROD_ERR_STOP;
            last_error = "producer DMA abort did not retire; claims retained";
            return;
        }
    for (unsigned stream = 0; stream < 4; ++stream) {
        if (state.dma[stream] >= 0) {
            dma_channel_cleanup(state.dma[stream]);
            dma_channel_unclaim(state.dma[stream]);
        }
        if (state.sm[stream] >= 0) {
            pio_sm_clear_fifos(state.pio[stream], state.sm[stream]);
            pio_sm_unclaim(state.pio[stream], state.sm[stream]);
        }
    }
    for (unsigned block = 0; block < 2; ++block)
        if (state.added[block]) pio_remove_program(block ? pio1 : pio0, &program, state.offset[block]);
    state.owned = false;
}

const char *iq4prod_dma_last_error(void) { return last_error; }
