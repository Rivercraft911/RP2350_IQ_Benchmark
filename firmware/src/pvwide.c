#include "pvwide.h"

#include <stdio.h>
#include <string.h>

#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/structs/io_bank0.h"
#include "hardware/structs/pads_bank0.h"
#include "hardware/structs/sio.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "pvproto.h"
#include "tx.h"

enum {
    W_DATA = 17, W_CLOCK = 21, W_READY = 22, W_CS = 26,
    W_RX_LEN = 8, W_GATE_LEN = 5, W_TX_LEN = 12, W_DRAIN_US = 3,
    W_ERR_SHORT = 1u, W_ERR_LONG = 2u, W_ERR_LATE = 4u,
    W_ERR_OVERFLOW = 8u, W_ERR_DMA = 16u, W_GUARD = 0x6d39a75bu,
};
#define W_PIN_MASK ((15u << W_DATA) | (1u << W_CLOCK) | (1u << W_READY) | (1u << W_CS))
_Static_assert(PV_SLOTS * PV_MSG_WORDS + 4 <= IN_WORDS, "RX slots and guard borrow input");
_Static_assert(PV_EMU_WORDS + 4 <= CAP_WORDS_MAX, "sender and guard borrow capture");

typedef struct {
    uint32_t dma, sm[3];
} w_claims_t;

typedef struct {
    PIO pio;
    int ch_rx, ch_tx, ch_ctl, sm_rx, sm_gate, sm_tx;
    uint off_rx, off_gate, off_tx, irq_num;
    uint16_t prog_rx[W_RX_LEN], prog_gate[W_GATE_LEN], prog_tx[W_TX_LEN];
    uint32_t *emu_base;
    uint32_t beats, source_bytes, source_words;
    volatile uint32_t head, tail, started, short_msgs, long_msgs, overflows;
    volatile uint32_t errors, drain_timeouts, late_closes;
    volatile uint64_t close_cycles;
    int8_t slot_crc[PV_SLOTS];
    volatile bool admitting, armed;
    uint32_t closed, consumed, pattern_mismatches, payload_crc, first_seq;
    uint64_t consume_cycles, validated_bytes;
    uint32_t pin_ctrl[7], pin_pad[7], sio_out, sio_oe, pio_out, pio_oe;
    uint32_t sniff_ctrl, sniff_data, sync_bypass;
    bool irq_added, programs_added, emulate;
} w_state_t;

// Four bytes of permanent state. Programs, queue metadata and counters live on
// the idle command's stack rather than consuming the nearly full SRAM arenas.
static w_state_t *w_active;
static const uint8_t w_pins[7] = {17, 18, 19, 20, 21, 22, 26};

static w_claims_t w_claims(void) {
    w_claims_t c = {0};
    const PIO buses[] = {pio0, pio1, pio2};
    for (uint i = 0; i < NUM_DMA_CHANNELS; ++i)
        if (dma_channel_is_claimed(i)) c.dma |= 1u << i;
    for (uint p = 0; p < 3; ++p)
        for (uint sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm)
            if (pio_sm_is_claimed(buses[p], sm)) c.sm[p] |= 1u << sm;
    return c;
}

static uint32_t *w_slot(uint32_t n) { return in_buf + (n % PV_SLOTS) * PV_MSG_WORDS; }

// The RX counter is exact in beats, not merely DMA words. After the last
// falling clock the terminal WAIT at +5 admits no data. Any extra rising edge
// sets sticky IRQ1 and parks at +7, including a single extra bit/nibble.
static void w_build(w_state_t *s, int width, int half) {
    s->prog_rx[0] = pio_encode_wait_gpio(false, W_CS);
    s->prog_rx[1] = pio_encode_wait_gpio(true, W_CLOCK);
    s->prog_rx[2] = pio_encode_in(pio_pins, (uint)width);
    s->prog_rx[3] = pio_encode_wait_gpio(false, W_CLOCK);
    s->prog_rx[4] = pio_encode_jmp_x_dec(1);
    s->prog_rx[5] = pio_encode_wait_gpio(true, W_CLOCK);
    s->prog_rx[6] = pio_encode_irq_set(false, 1);
    s->prog_rx[7] = pio_encode_jmp(7);
    s->prog_gate[0] = pio_encode_pull(false, true) | pio_encode_sideset(1, 0);
    s->prog_gate[1] = pio_encode_wait_gpio(true, W_CS) | pio_encode_sideset(1, 0);
    s->prog_gate[2] = pio_encode_wait_gpio(false, W_CS) | pio_encode_sideset(1, 1);
    s->prog_gate[3] = pio_encode_wait_gpio(true, W_CS) | pio_encode_sideset(1, 0);
    s->prog_gate[4] = pio_encode_irq_wait(false, 0) | pio_encode_sideset(1, 0);
    const uint d = (uint)half - 1;
    s->prog_tx[0] = pio_encode_wait_gpio(true, W_READY) | pio_encode_sideset(1, 0);
    s->prog_tx[1] = pio_encode_set(pio_pins, 0) | pio_encode_sideset(1, 0);
    s->prog_tx[2] = pio_encode_mov(pio_x, pio_y) | pio_encode_sideset(1, 0);
    s->prog_tx[3] = pio_encode_out(pio_pins, (uint)width) | pio_encode_sideset(1, 0) | pio_encode_delay(d);
    s->prog_tx[4] = pio_encode_jmp_x_dec(3) | pio_encode_sideset(1, 1) | pio_encode_delay(d);
    s->prog_tx[5] = pio_encode_set(pio_pins, 1) | pio_encode_sideset(1, 0);
    // Three 32 x 16-cycle gaps, 12 us at SYS128. The sender always rechecks
    // READY before CS. CPU validation and admission may extend this gap.
    for (uint n = 0; n < 3; ++n) {
        s->prog_tx[6 + 2 * n] = pio_encode_set(pio_x, 31) | pio_encode_sideset(1, 0);
        s->prog_tx[7 + 2 * n] = pio_encode_jmp_x_dec(7 + 2 * n) | pio_encode_sideset(1, 0) | pio_encode_delay(15);
    }
}

static void w_rx_reset(w_state_t *s) {
    pio_sm_set_enabled(s->pio, (uint)s->sm_rx, false);
    pio_sm_clear_fifos(s->pio, (uint)s->sm_rx);
    pio_sm_restart(s->pio, (uint)s->sm_rx);  // also reset ISR shift count
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_mov(pio_isr, pio_null));
    // RX's joined FIFO has no TX capacity. Load X in three 5-bit chunks through
    // ISR using SET/IN, then clear ISR and its count before enabling RX.
    const uint32_t x = s->beats - 1;
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_set(pio_x, (x >> 10) & 31u));
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_in(pio_x, 5));
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_set(pio_x, (x >> 5) & 31u));
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_in(pio_x, 5));
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_set(pio_x, x & 31u));
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_in(pio_x, 5));
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_mov(pio_x, pio_isr));
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_mov(pio_isr, pio_null));
    pio_sm_restart(s->pio, (uint)s->sm_rx);
    pio_interrupt_clear(s->pio, 1);
    pio_sm_exec(s->pio, (uint)s->sm_rx, pio_encode_jmp(s->off_rx));
    pio_sm_set_enabled(s->pio, (uint)s->sm_rx, true);
}

// Both callers have interrupts excluded. A false->true arm posts exactly one
// CPU token, so consumer polling cannot accidentally grant a second message.
static void w_arm(w_state_t *s) {
    if (!s->armed && s->admitting && s->head - s->tail < PV_SLOTS) {
        dma_sniffer_set_data_accumulator(0xffffffffu);
        dma_channel_transfer_to_buffer_now((uint)s->ch_rx, w_slot(s->head), PV_MSG_WORDS);
        s->armed = true;
        __dmb();
        pio_sm_put(s->pio, (uint)s->sm_gate, 0);
    }
}

static bool w_close(w_state_t *s) {
    dma_channel_hw_t *rx = dma_channel_hw_addr((uint)s->ch_rx);
    uint32_t remaining, ctrl, pc;
    const uint32_t start = time_us_32();
    // CS may become visible before the final RX instruction/DMA beat retires.
    // Drain both machines, and require terminal PC even when DMA is already full.
    do {
        remaining = rx->transfer_count;
        ctrl = rx->ctrl_trig;
        pc = pio_sm_get_pc(s->pio, (uint)s->sm_rx);
        if ((!remaining && !(ctrl & DMA_CH0_CTRL_TRIG_BUSY_BITS) && pc == s->off_rx + 5) ||
            pio_interrupt_get(s->pio, 1)) break;
    } while ((uint32_t)(time_us_32() - start) < W_DRAIN_US);
    const bool late = !gpio_get(W_CS);
    const bool longer = pio_interrupt_get(s->pio, 1) || pc == s->off_rx + 6 || pc == s->off_rx + 7;
    const uint32_t fifo = pio_sm_get_rx_fifo_level(s->pio, (uint)s->sm_rx);
    if (late) { s->late_closes++; s->errors |= W_ERR_LATE; }
    if (ctrl & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS) s->errors |= W_ERR_DMA;
    if (!remaining && !(ctrl & (DMA_CH0_CTRL_TRIG_BUSY_BITS | DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS)) &&
        !fifo && !late && !longer && pc == s->off_rx + 5) return true;
    if (longer || fifo) { s->long_msgs++; s->errors |= W_ERR_LONG; }
    else { s->short_msgs++; s->errors |= W_ERR_SHORT; }
    if (remaining || (ctrl & DMA_CH0_CTRL_TRIG_BUSY_BITS)) s->drain_timeouts++;
    dma_channel_abort((uint)s->ch_rx);
    return false;
}

static void w_cs_isr(void) {
    w_state_t *s = w_active;
    if (!s) return;
    const uint32_t start = cycles();
    s->started++;
    if (s->armed) {
        if (w_close(s)) {
            s->slot_crc[s->head % PV_SLOTS] = dma_sniffer_get_data_accumulator() == 0x2144df1cu;
            __dmb();
            s->head++;
        }
        s->armed = false;
    } else { s->overflows++; s->errors |= W_ERR_OVERFLOW; }
    w_rx_reset(s);
    w_arm(s);
    pio_interrupt_clear(s->pio, 0);
    __dsb();
    s->close_cycles += (uint32_t)(cycles() - start);
}

static void w_consume(w_state_t *s) {
    // Bound each call so a saturated sender cannot postpone the run deadline.
    for (uint budget = PV_SLOTS; budget && s->tail != s->head; --budget) {
        const uint32_t saved = save_and_disable_interrupts();
        const uint64_t irq_before = s->close_cycles;
        const uint32_t start = cycles();
        restore_interrupts(saved);
        __dmb();
        const uint8_t *m = (const uint8_t *)w_slot(s->tail);
        if (!s->consumed) s->first_seq = (uint32_t)m[4] | (uint32_t)m[5] << 8;
        const int accepted = pv_accept(m, s->slot_crc[s->tail % PV_SLOTS]);
        if (accepted > 0) {
            const uint32_t seq = (uint32_t)m[4] | (uint32_t)m[5] << 8;
            if (s->emulate && (seq >= PV_EMU_MSGS || memcmp(m, (const uint8_t *)s->emu_base + seq * s->source_bytes, PV_MSG_BYTES)))
                s->pattern_mismatches++;
            s->payload_crc = pv_crc32(s->payload_crc, m + 12, (uint)accepted * PV_TS);
            s->validated_bytes += (uint)accepted * PV_TS;
        }
        s->consumed++;
        const uint32_t saved_after = save_and_disable_interrupts();
        s->tail++;
        w_arm(s);
        const uint32_t elapsed = cycles() - start;
        const uint64_t irq_cycles = s->close_cycles - irq_before;
        s->consume_cycles += elapsed >= irq_cycles ? elapsed - irq_cycles : 0;
        restore_interrupts(saved_after);
    }
}

static void w_save_pins(w_state_t *s) {
    s->pio_out = s->pio->dbg_padout & W_PIN_MASK;
    s->pio_oe = s->pio->dbg_padoe & W_PIN_MASK;
    s->sync_bypass = s->pio->input_sync_bypass;
    s->sio_out = sio_hw->gpio_out & W_PIN_MASK;
    s->sio_oe = sio_hw->gpio_oe & W_PIN_MASK;
    for (uint i = 0; i < 7; ++i) {
        s->pin_ctrl[i] = io_bank0_hw->io[w_pins[i]].ctrl;
        s->pin_pad[i] = pads_bank0_hw->io[w_pins[i]];
    }
}

static bool w_restore_pins(w_state_t *s) {
    s->pio->input_sync_bypass = s->sync_bypass;
    // Restore the original SIO latches before reconnecting their pin functions.
    sio_hw->gpio_clr = W_PIN_MASK;
    sio_hw->gpio_set = s->sio_out;
    sio_hw->gpio_oe_clr = W_PIN_MASK;
    sio_hw->gpio_oe_set = s->sio_oe;
    for (uint i = 0; i < 7; ++i) {
        pads_bank0_hw->io[w_pins[i]] = s->pin_pad[i];
        io_bank0_hw->io[w_pins[i]].ctrl = s->pin_ctrl[i];
    }
    bool same = (s->pio->dbg_padout & W_PIN_MASK) == s->pio_out &&
                (s->pio->dbg_padoe & W_PIN_MASK) == s->pio_oe &&
                s->pio->input_sync_bypass == s->sync_bypass && (sio_hw->gpio_out & W_PIN_MASK) == s->sio_out && (sio_hw->gpio_oe & W_PIN_MASK) == s->sio_oe;
    for (uint i = 0; i < 7; ++i)
        same = same && pads_bank0_hw->io[w_pins[i]] == s->pin_pad[i] && io_bank0_hw->io[w_pins[i]].ctrl == s->pin_ctrl[i];
    return same;
}

static void w_release(w_state_t *s) {
    if (s->irq_added) {
        irq_set_enabled(s->irq_num, false);
        pio_set_irq0_source_enabled(s->pio, pis_interrupt0, false);
        pio_set_sm_mask_enabled(s->pio, (1u << s->sm_rx) | (1u << s->sm_gate) | (s->emulate ? 1u << s->sm_tx : 0u), false);
        pio_sm_clear_fifos(s->pio, (uint)s->sm_gate);
        pio_interrupt_clear(s->pio, 0);
        pio_interrupt_clear(s->pio, 1);
        irq_clear(s->irq_num);
        irq_remove_handler(s->irq_num, w_cs_isr);
        w_active = NULL;
    }
    const int channels[] = {s->ch_rx, s->ch_tx, s->ch_ctl};
    // Break every trigger path and disable all channels before any abort (E5).
    for (uint n = 0; n < 3; ++n) if (channels[n] >= 0)
        hw_write_masked(&dma_hw->ch[channels[n]].al1_ctrl, (uint32_t)channels[n] << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB,
                        DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS | DMA_CH0_CTRL_TRIG_EN_BITS);
    for (uint n = 0; n < 3; ++n) if (channels[n] >= 0) {
        dma_channel_cleanup((uint)channels[n]);
        dma_channel_unclaim((uint)channels[n]);
    }
    dma_hw->sniff_ctrl = s->sniff_ctrl;
    dma_hw->sniff_data = s->sniff_data;
    const int sms[] = {s->sm_rx, s->sm_gate, s->sm_tx};
    for (uint n = 0; n < 3; ++n) if (sms[n] >= 0) {
        pio_sm_set_enabled(s->pio, (uint)sms[n], false);
        pio_sm_set_pindirs_with_mask(s->pio, (uint)sms[n], 0, W_PIN_MASK);
        pio_sm_clear_fifos(s->pio, (uint)sms[n]);
    }
    // All SMs are quiet before restoring shared PIO output latches. An old
    // pin function may still select PIO2 even when its SM is unclaimed.
    if (s->sm_rx >= 0) {
        pio_sm_set_pins_with_mask(s->pio, (uint)s->sm_rx, s->pio_out, W_PIN_MASK);
        pio_sm_set_pindirs_with_mask(s->pio, (uint)s->sm_rx, s->pio_oe, W_PIN_MASK);
    }
    for (uint n = 0; n < 3; ++n) if (sms[n] >= 0) pio_sm_unclaim(s->pio, (uint)sms[n]);
    if (s->programs_added) {
        pio_remove_program(s->pio, &(pio_program_t){.instructions = s->prog_rx, .length = W_RX_LEN, .origin = -1}, s->off_rx);
        pio_remove_program(s->pio, &(pio_program_t){.instructions = s->prog_gate, .length = W_GATE_LEN, .origin = -1}, s->off_gate);
        if (s->emulate) pio_remove_program(s->pio, &(pio_program_t){.instructions = s->prog_tx, .length = W_TX_LEN, .origin = -1}, s->off_tx);
    }
}

void bench_pvwide(int ms, int width, int half, int fault, int emulate) {
    const uint32_t hz = clock_get_hz(clk_sys);
    const w_claims_t before = w_claims();
    if (ms < 100 || ms > 60000 || (width != 1 && width != 4) || half < 3 || half > 16 || fault < 0 || fault > 2 ||
        emulate < 0 || emulate > 1 || (!emulate && fault) ||
        hz != 128000000u || before.sm[2] || pio_get_gpio_base(pio2) != 0 || __builtin_popcount(before.dma) > NUM_DMA_CHANNELS - (emulate ? 3 : 1) ||
        irq_is_enabled(PIO2_IRQ_0) || pio2->inte0 || pio2->irq || (dma_hw->sniff_ctrl & DMA_SNIFF_CTRL_EN_BITS)) {
        printf("@{\"cmd\":\"pvwide\",\"error\":\"needs SYS128, idle PIO2(base0)/IRQ0/sniffer,ms100..60000,width1/4,half3..16,fault0..2,emulate0/1(external requiresfault0)\"}\n");
        return;
    }
    w_state_t state = {.pio = pio2, .ch_rx = -1, .ch_tx = -1, .ch_ctl = -1, .sm_rx = -1, .sm_gate = -1, .sm_tx = -1};
    w_state_t *s = &state;
    s->emulate = emulate != 0;
    s->irq_num = PIO2_IRQ_0;
    s->sniff_ctrl = dma_hw->sniff_ctrl;
    s->sniff_data = dma_hw->sniff_data;
    s->beats = PV_MSG_BYTES * 8u / (uint)width;
    s->source_bytes = PV_MSG_BYTES - (fault == 2 ? 4u : 0u);
    s->source_words = s->source_bytes / 4u * PV_EMU_MSGS;
    w_build(s, width, half);
    uint16_t reserve[W_RX_LEN + W_GATE_LEN + W_TX_LEN] = {0};
    if (!pio_can_add_program(s->pio, &(pio_program_t){.instructions = reserve, .length = W_RX_LEN + W_GATE_LEN + (emulate ? W_TX_LEN : 0), .origin = -1})) {
        printf("@{\"cmd\":\"pvwide\",\"error\":\"insufficient PIO2 instruction slots (13 RX or25 selftest)\"}\n");
        return;
    }
    w_save_pins(s);
    s->ch_rx = dma_claim_unused_channel(false);
    if (emulate) {
        s->ch_tx = dma_claim_unused_channel(false);
        s->ch_ctl = dma_claim_unused_channel(false);
    }
    s->sm_rx = pio_claim_unused_sm(s->pio, false);
    s->sm_gate = pio_claim_unused_sm(s->pio, false);
    if (emulate) s->sm_tx = pio_claim_unused_sm(s->pio, false);
    if (s->ch_rx < 0 || s->sm_rx < 0 || s->sm_gate < 0 || (emulate && (s->ch_tx < 0 || s->ch_ctl < 0 || s->sm_tx < 0))) {
        w_release(s);
        (void)w_restore_pins(s);
        printf("@{\"cmd\":\"pvwide\",\"error\":\"resource claim failed\"}\n");
        return;
    }
    pv_init(emulate ? PV_EMU_MSGS : 65536u);
    // Build in ascending order. The next message overwrites the prior message's
    // omitted CRC when packing shorts. The DMA stride remains word aligned.
    for (uint32_t m = 0; emulate && m < PV_EMU_MSGS; ++m) {
        pv_pattern_message((uint8_t *)cap_buf + m * s->source_bytes, m, PV_EMU_MSGS);
    }
    if (fault == 1) ((uint8_t *)cap_buf)[20] ^= 0x01;
    s->emu_base = cap_buf;
    const uint32_t source_crc = emulate ? pv_crc32(0, cap_buf, s->source_words * 4u) : 0;
    for (uint n = PV_SLOTS * PV_MSG_WORDS; n < IN_WORDS; ++n) in_buf[n] = W_GUARD;
    if (emulate) cap_buf[s->source_words] = W_GUARD;
    s->off_rx = pio_add_program(s->pio, &(pio_program_t){.instructions = s->prog_rx, .length = W_RX_LEN, .origin = -1});
    s->off_gate = pio_add_program(s->pio, &(pio_program_t){.instructions = s->prog_gate, .length = W_GATE_LEN, .origin = -1});
    if (emulate) s->off_tx = pio_add_program(s->pio, &(pio_program_t){.instructions = s->prog_tx, .length = W_TX_LEN, .origin = -1});
    s->programs_added = true;
    // GPIOs feed both receiver and on-chip sender. No wires or external source
    // are required. Enable input synchronizers explicitly for every bus pin.
    s->pio->input_sync_bypass &= ~W_PIN_MASK;
    for (uint n = 0; n < 7; ++n) pio_gpio_init(s->pio, w_pins[n]);
    pio_sm_set_pindirs_with_mask(s->pio, (uint)s->sm_rx, 0, W_PIN_MASK);
    gpio_pull_up(W_CS);
    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, s->off_rx, s->off_rx + W_RX_LEN - 1);
    sm_config_set_in_pins(&c, W_DATA);
    sm_config_set_in_shift(&c, false, true, 32);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
    pio_sm_init(s->pio, (uint)s->sm_rx, s->off_rx, &c);
    c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, s->off_gate, s->off_gate + W_GATE_LEN - 1);
    sm_config_set_sideset(&c, 1, false, false);
    sm_config_set_sideset_pins(&c, W_READY);
    pio_sm_init(s->pio, (uint)s->sm_gate, s->off_gate, &c);
    pio_sm_set_pins_with_mask(s->pio, (uint)s->sm_gate, 0, 1u << W_READY);
    pio_sm_set_consecutive_pindirs(s->pio, (uint)s->sm_gate, W_READY, 1, true);
    gpio_set_outover(W_READY, GPIO_OVERRIDE_LOW);
    if (emulate) {
        c = pio_get_default_sm_config();
        sm_config_set_wrap(&c, s->off_tx, s->off_tx + W_TX_LEN - 1);
        sm_config_set_out_pins(&c, W_DATA, (uint)width);
        sm_config_set_set_pins(&c, W_CS, 1);
        sm_config_set_sideset(&c, 1, false, false);
        sm_config_set_sideset_pins(&c, W_CLOCK);
        sm_config_set_out_shift(&c, false, true, 32);
        sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
        pio_sm_init(s->pio, (uint)s->sm_tx, s->off_tx, &c);
        pio_sm_set_pins_with_mask(s->pio, (uint)s->sm_tx, 1u << W_CS, (1u << W_CLOCK) | (1u << W_CS));
        pio_sm_set_pindirs_with_mask(s->pio, (uint)s->sm_tx, (1u << W_CLOCK) | (1u << W_CS) | (((1u << width) - 1u) << W_DATA), W_PIN_MASK & ~(1u << W_READY));
        pio_sm_put(s->pio, (uint)s->sm_tx, s->source_bytes * 8u / (uint)width - 1u);
        pio_sm_exec(s->pio, (uint)s->sm_tx, pio_encode_pull(false, true));
        pio_sm_exec(s->pio, (uint)s->sm_tx, pio_encode_mov(pio_y, pio_osr));
        pio_sm_exec(s->pio, (uint)s->sm_tx, pio_encode_out(pio_null, 32));
    }
    const int channels[] = {s->ch_rx, s->ch_tx, s->ch_ctl};
    for (uint n = 0; n < 3; ++n) if (channels[n] >= 0) {
        dma_channel_cleanup((uint)channels[n]);
        dma_hw->ch[channels[n]].al1_ctrl = DMA_CH0_CTRL_TRIG_READ_ERROR_BITS | DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS |
                                        (uint32_t)channels[n] << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB;
    }
    dma_channel_config d = dma_channel_get_default_config((uint)s->ch_rx);
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, false);
    channel_config_set_write_increment(&d, true);
    channel_config_set_bswap(&d, true);
    channel_config_set_dreq(&d, pio_get_dreq(s->pio, (uint)s->sm_rx, false));
    channel_config_set_sniff_enable(&d, true);
    dma_channel_configure((uint)s->ch_rx, &d, in_buf, &s->pio->rxf[s->sm_rx], PV_MSG_WORDS, false);
    dma_sniffer_enable((uint)s->ch_rx, DMA_SNIFF_CTRL_CALC_VALUE_CRC32R, true);
    dma_sniffer_set_byte_swap_enabled(false);
    dma_sniffer_set_output_reverse_enabled(true);
    dma_sniffer_set_output_invert_enabled(true);
    if (emulate) {
        d = dma_channel_get_default_config((uint)s->ch_tx);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_write_increment(&d, false);
        channel_config_set_bswap(&d, true);
        channel_config_set_dreq(&d, pio_get_dreq(s->pio, (uint)s->sm_tx, true));
        channel_config_set_chain_to(&d, (uint)s->ch_ctl);
        dma_channel_configure((uint)s->ch_tx, &d, &s->pio->txf[s->sm_tx], s->emu_base, s->source_words, false);
        d = dma_channel_get_default_config((uint)s->ch_ctl);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, false);
        channel_config_set_write_increment(&d, false);
        dma_channel_configure((uint)s->ch_ctl, &d, &dma_hw->ch[s->ch_tx].al3_read_addr_trig, &s->emu_base, 1, false);
    }
    w_active = s;
    irq_add_shared_handler(s->irq_num, w_cs_isr, PICO_SHARED_IRQ_HANDLER_DEFAULT_ORDER_PRIORITY);
    s->irq_added = true;
    pio_set_irq0_source_enabled(s->pio, pis_interrupt0, true);
    pio_interrupt_clear(s->pio, 0);
    pio_interrupt_clear(s->pio, 1);
    irq_set_enabled(s->irq_num, true);
    enable_cyccnt();
    w_rx_reset(s);
    pio_sm_set_enabled(s->pio, (uint)s->sm_gate, true);
    const uint64_t start = time_us_64(), end = start + (uint64_t)ms * 1000u;
    const uint32_t saved = save_and_disable_interrupts();
    s->admitting = true;
    w_arm(s);
    gpio_set_outover(W_READY, GPIO_OVERRIDE_NORMAL);
    restore_interrupts(saved);
    if (emulate) {
        dma_channel_start((uint)s->ch_tx);
        pio_sm_set_enabled(s->pio, (uint)s->sm_tx, true);
    }
    while (time_us_64() < end) w_consume(s);
    const uint32_t paused = save_and_disable_interrupts();
    s->admitting = false;
    gpio_set_outover(W_READY, GPIO_OVERRIDE_LOW);
    restore_interrupts(paused);
    // A master may have observed READY just before it fell. Allow that one
    // message to finish, including the slowest supported width1/half16 case.
    const uint32_t settle_us = emulate ? (uint32_t)(((uint64_t)s->source_bytes * 8u * (2u * (uint)half) * 1000000u +
                                          (uint64_t)width * hz - 1u) / ((uint64_t)width * hz)) + 1000u : 10000u;
    const uint64_t settle_end = time_us_64() + settle_us;
    while (time_us_64() < settle_end) w_consume(s);
    w_consume(s);
    const uint64_t elapsed = time_us_64() - start;
    s->closed = s->head;
    const bool shutdown_inflight = !gpio_get(W_CS);
    if (shutdown_inflight) s->errors |= W_ERR_LATE;
    w_release(s);
    bool guards = !emulate || cap_buf[s->source_words] == W_GUARD;
    for (uint n = PV_SLOTS * PV_MSG_WORDS; n < IN_WORDS; ++n) guards = guards && in_buf[n] == W_GUARD;
    const bool source_unchanged = !emulate || pv_crc32(0, cap_buf, s->source_words * 4u) == source_crc;
    const bool pins_restored = w_restore_pins(s);
    const w_claims_t after = w_claims();
    const bool released = !memcmp(&before, &after, sizeof before) && pins_restored &&
                          !irq_is_enabled(s->irq_num) && !s->pio->inte0 &&
                          dma_hw->sniff_ctrl == s->sniff_ctrl && dma_hw->sniff_data == s->sniff_data;
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
    printf("@{\"cmd\":\"pvwide\",\"git\":\"%s\",\"sys_hz\":%lu,\"ms\":%d,\"width\":%d,\"half\":%d,\"fault\":%d,\"emulate\":%d,"
           "\"elapsed_us\":%llu,\"wire_hz\":%lu,\"closed\":%lu,\"consumed\":%lu,\"started\":%lu,"
           "\"ok\":%lu,\"nop\":%lu,\"bad_crc\":%lu,\"bad_hdr\":%lu,\"bad_sync\":%lu,\"lost\":%lu,\"short_msgs\":%lu,"
           "\"long_msgs\":%lu,\"overflows\":%lu,\"errors\":%lu,\"crc_chain\":%lu,\"payload_crc\":%lu,"
           "\"pattern_mismatches\":%lu,\"close_cycles\":%llu,\"consume_cycles\":%llu,\"first_seq\":%lu,\"last_seq\":%lu,"
           "\"drain_timeouts\":%lu,\"late_closes\":%lu,\"slots_addr\":%lu,\"sender_addr\":%lu,"
           "\"state_addr\":%lu,\"rx_off\":%u,\"gate_off\":%u,\"tx_off\":%u,\"pio\":2,\"sms\":%u,\"dma_channels\":%u,"
           "\"pio_instructions\":%u,\"seq_mod\":%lu,\"period_msgs\":%u,\"source\":\"%s\",\"validated_bytes\":%llu,\"shutdown_inflight\":%s,\"guard_ok\":%s,\"source_unchanged\":%s,\"released\":%s}\n",
           GIT_REV, (unsigned long)hz, ms, width, half, fault, emulate, (unsigned long long)elapsed,
           (unsigned long)(emulate ? hz / (2u * (uint)half) : 0), (unsigned long)s->closed, (unsigned long)s->consumed,
           (unsigned long)s->started, (unsigned long)pv_stats.ok, (unsigned long)pv_stats.nop, (unsigned long)pv_stats.bad_crc,
           (unsigned long)pv_stats.bad_hdr, (unsigned long)pv_stats.bad_sync, (unsigned long)pv_stats.lost,
           (unsigned long)s->short_msgs, (unsigned long)s->long_msgs, (unsigned long)s->overflows,
           (unsigned long)s->errors, (unsigned long)pv_stats.crc_chain, (unsigned long)s->payload_crc,
           (unsigned long)s->pattern_mismatches, (unsigned long long)s->close_cycles,
           (unsigned long long)s->consume_cycles, (unsigned long)s->first_seq, (unsigned long)pv_stats.last_seq,
           (unsigned long)s->drain_timeouts, (unsigned long)s->late_closes, (unsigned long)(uintptr_t)in_buf,
           (unsigned long)(uintptr_t)cap_buf, (unsigned long)(uintptr_t)s, s->off_rx, s->off_gate, s->off_tx,
           emulate ? 3u : 2u, emulate ? 3u : 1u, emulate ? 25u : 13u,
           (unsigned long)pv_stats.seq_mod, emulate ? PV_EMU_MSGS : 0u, emulate ? "on_chip_pv_pattern" : "external",
           (unsigned long long)s->validated_bytes, shutdown_inflight ? "true" : "false", guards ? "true" : "false", source_unchanged ? "true" : "false", released ? "true" : "false");
}
