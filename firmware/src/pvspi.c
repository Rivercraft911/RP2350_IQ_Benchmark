#include "pvspi.h"

#include "config.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "pvproto.h"

#define PIN_SCK 17
#define PIN_MOSI 18
#define PIN_CS 19
#define PIN_READY PIN_IN_READY               // GP22

pvspi_stats_t pvspi;
static volatile bool admitting;
static uint8_t *msg;                                 // consumer: slot being read (reset per run)
static int npk, ipk;

static PIO const pio = pio2;
static uint sm_rx, sm_cs, sm_emu, off_rx, off_cs, off_emu;
static int ch_rx = -1, ch_emu = -1, ch_ctl = -1;
static uint32_t *emu_base;                            // read by ch_ctl to rewind ch_emu
static spin_lock_t *lock;
static uint32_t *slot_mem;
static int8_t slot_crc[PV_SLOTS];                    // 1 = sniffer residue OK, 0 = bad
static bool armed, emu_on;
static uint16_t prog_rx[4], prog_cs[3], prog_emu[10];
static uint8_t len_emu;

static inline uint32_t *slot(uint32_t n) { return slot_mem + (n % PV_SLOTS) * PV_MSG_WORDS; }

// RX: wait for CS_N low once, then sample MOSI on each SCK rising edge, MSB first, autopush 32.
// CS watcher: raise PIO IRQ 0 on every CS_N rising edge (end of message).
static void build(int emu_half) {
    prog_rx[0] = pio_encode_wait_gpio(false, PIN_CS);
    prog_rx[1] = pio_encode_wait_gpio(true, PIN_SCK);          // wrap target
    prog_rx[2] = pio_encode_in(pio_pins, 1);
    prog_rx[3] = pio_encode_wait_gpio(false, PIN_SCK);
    prog_cs[0] = pio_encode_wait_gpio(false, PIN_CS);
    prog_cs[1] = pio_encode_wait_gpio(true, PIN_CS);
    prog_cs[2] = pio_encode_irq_set(false, 0);
    // Emulated master: wait READY, CS_N low, 10 656 bits (Y = bits - 1) with SCK as side-set,
    // CS_N high, then a gap of 2 x 32 x 16 cycles (8 us at 128 MHz).
    const uint d = (uint)emu_half - 1;
    int i = 0;
    prog_emu[i++] = pio_encode_wait_gpio(true, PIN_READY) | pio_encode_sideset(1, 0);
    prog_emu[i++] = pio_encode_set(pio_pins, 0) | pio_encode_sideset(1, 0);
    prog_emu[i++] = pio_encode_mov(pio_x, pio_y) | pio_encode_sideset(1, 0);
    prog_emu[i++] = pio_encode_out(pio_pins, 1) | pio_encode_sideset(1, 0) | pio_encode_delay(d);
    prog_emu[i++] = pio_encode_jmp_x_dec(3) | pio_encode_sideset(1, 1) | pio_encode_delay(d);
    prog_emu[i++] = pio_encode_set(pio_pins, 1) | pio_encode_sideset(1, 0);
    prog_emu[i++] = pio_encode_set(pio_x, 31) | pio_encode_sideset(1, 0);
    prog_emu[i++] = pio_encode_jmp_x_dec(7) | pio_encode_sideset(1, 0) | pio_encode_delay(15);
    prog_emu[i++] = pio_encode_set(pio_x, 31) | pio_encode_sideset(1, 0);
    prog_emu[i++] = pio_encode_jmp_x_dec(9) | pio_encode_sideset(1, 0) | pio_encode_delay(15);
    len_emu = (uint8_t)i;
}

static void __not_in_flash_func(arm_locked)(void) {         // caller holds lock
    if (!armed && pvspi.head - pvspi.tail < PV_SLOTS) {
        dma_sniffer_set_data_accumulator(0xFFFFFFFFu);
        dma_channel_transfer_to_buffer_now(ch_rx, slot(pvspi.head), PV_MSG_WORDS);
        armed = true;
    }
    gpio_put(PIN_READY, armed && admitting);
}

static void __not_in_flash_func(rx_reset)(void) {           // drop partial bits and FIFO words
    pio_sm_clear_fifos(pio, sm_rx);
    pio_sm_exec(pio, sm_rx, pio_encode_mov(pio_isr, pio_null));
    pio_sm_exec(pio, sm_rx, pio_encode_jmp(off_rx));
}

// CS_N rose: a whole message is exactly PV_MSG_WORDS words with nothing left in the FIFO. The RX SM
// is reset every time, so up to 31 extra bits (one stray SCK edge is enough) cannot stay in its
// shift register and misalign every later message.
static void __not_in_flash_func(cs_isr)(void) {
    pio_interrupt_clear(pio, 0);
    const uint32_t s = spin_lock_blocking(lock);
    if (armed) {
        const uint32_t got = PV_MSG_WORDS - dma_channel_hw_addr(ch_rx)->transfer_count;
        const bool whole = got == PV_MSG_WORDS && pio_sm_is_rx_fifo_empty(pio, sm_rx);
        if (whole) {
            // CRC-32 over data plus its own little-endian CRC leaves the residue 0x2144DF1C.
            slot_crc[pvspi.head % PV_SLOTS] = dma_sniffer_get_data_accumulator() == 0x2144DF1Cu;
            pvspi.head++;
        } else {
            if (got == PV_MSG_WORDS) pvspi.long_msgs++;
            else pvspi.short_msgs++;
            dma_channel_abort(ch_rx);
        }
        armed = false;
    } else {
        pvspi.overflows++;                                   // arrived with no slot (READY low)
    }
    rx_reset();
    arm_locked();
    spin_unlock(lock, s);
}

void pvspi_start(uint32_t *slots, bool selftest, uint32_t *emu, int emu_half) {
    slot_mem = slots;
    pvspi = (pvspi_stats_t){0};
    armed = false;
    msg = 0, npk = ipk = 0;                               // no stale pointer into last run's slots
    if (ch_rx < 0) {
        ch_rx = dma_claim_unused_channel(true);
        ch_emu = dma_claim_unused_channel(true);
        ch_ctl = dma_claim_unused_channel(true);
        sm_rx = pio_claim_unused_sm(pio, true);
        sm_cs = pio_claim_unused_sm(pio, true);
        sm_emu = pio_claim_unused_sm(pio, true);
        lock = spin_lock_instance(spin_lock_claim_unused(true));
        irq_set_exclusive_handler(PIO2_IRQ_0, cs_isr);
    }
    pio_clear_instruction_memory(pio);
    build(emu_half);
    off_rx = pio_add_program(pio, &(pio_program_t){.instructions = prog_rx, .length = 4, .origin = -1});
    off_cs = pio_add_program(pio, &(pio_program_t){.instructions = prog_cs, .length = 3, .origin = -1});
    off_emu = pio_add_program(pio, &(pio_program_t){.instructions = prog_emu, .length = len_emu, .origin = -1});

    gpio_init(PIN_READY);
    gpio_set_dir(PIN_READY, GPIO_OUT);
    gpio_put(PIN_READY, 0);
    for (uint p = PIN_SCK; p <= PIN_CS; p++) pio_gpio_init(pio, p);
    gpio_pull_up(PIN_CS);                                    // idle high if the master is absent
    // PIO output enables outlive the SM that set them: after a self-test PIO2 would still drive
    // SCK/MOSI/CS against the CM5. Start every run with them as inputs.
    pio_sm_set_consecutive_pindirs(pio, sm_rx, PIN_SCK, 3, false);

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, off_rx + 1, off_rx + 3);
    sm_config_set_in_pins(&c, PIN_MOSI);
    sm_config_set_in_shift(&c, false, true, 32);             // MSB first, autopush 32
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
    pio_sm_init(pio, sm_rx, off_rx, &c);
    c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, off_cs, off_cs + 2);
    pio_sm_init(pio, sm_cs, off_cs, &c);
    pio_set_irq0_source_enabled(pio, pis_interrupt0, true);
    pio_interrupt_clear(pio, 0);

    dma_channel_config d = dma_channel_get_default_config(ch_rx);   // bswap: wire byte order
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, false);
    channel_config_set_write_increment(&d, true);
    channel_config_set_bswap(&d, true);
    channel_config_set_dreq(&d, pio_get_dreq(pio, sm_rx, false));
    channel_config_set_sniff_enable(&d, true);
    dma_channel_configure(ch_rx, &d, slot_mem, &pio->rxf[sm_rx], PV_MSG_WORDS, false);
    // Sniffer = zlib CRC-32 on the wire bytes (found with the snifftest command): CRC32R on the
    // post-bswap data, output bit-reversed and inverted, seed 0xFFFFFFFF.
    dma_sniffer_enable(ch_rx, DMA_SNIFF_CTRL_CALC_VALUE_CRC32R, true);
    dma_sniffer_set_byte_swap_enabled(false);
    dma_sniffer_set_output_reverse_enabled(true);
    dma_sniffer_set_output_invert_enabled(true);

    emu_on = selftest;
    if (selftest) {
        for (uint32_t m = 0; m < PV_EMU_MSGS; m++)
            pv_pattern_message((uint8_t *)(emu + m * PV_MSG_WORDS), m, PV_EMU_MSGS);
        pio_sm_set_consecutive_pindirs(pio, sm_emu, PIN_SCK, 3, true);
        pio_sm_set_pins_with_mask(pio, sm_emu, 1u << PIN_CS, 1u << PIN_CS);
        c = pio_get_default_sm_config();
        sm_config_set_wrap(&c, off_emu, off_emu + len_emu - 1);
        sm_config_set_out_pins(&c, PIN_MOSI, 1);
        sm_config_set_set_pins(&c, PIN_CS, 1);
        sm_config_set_sideset(&c, 1, false, false);
        sm_config_set_sideset_pins(&c, PIN_SCK);
        sm_config_set_out_shift(&c, false, true, 32);        // MSB first
        sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
        pio_sm_init(pio, sm_emu, off_emu, &c);
        pio_sm_put(pio, sm_emu, PV_MSG_BYTES * 8 - 1);         // Y = bits per message - 1
        pio_sm_exec(pio, sm_emu, pio_encode_pull(false, true));
        pio_sm_exec(pio, sm_emu, pio_encode_mov(pio_y, pio_osr));
        pio_sm_exec(pio, sm_emu, pio_encode_out(pio_null, 32));
        // 16 messages = 21 312 B is not a power of two, so no ring wrap: ch_emu sends them once
        // and chains to ch_ctl, which rewrites ch_emu's read address and so re-triggers it.
        emu_base = emu;
        d = dma_channel_get_default_config(ch_emu);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_write_increment(&d, false);
        channel_config_set_bswap(&d, true);
        channel_config_set_dreq(&d, pio_get_dreq(pio, sm_emu, true));
        channel_config_set_chain_to(&d, ch_ctl);
        dma_channel_configure(ch_emu, &d, &pio->txf[sm_emu], emu, PV_EMU_WORDS, false);
        d = dma_channel_get_default_config(ch_ctl);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, false);
        channel_config_set_write_increment(&d, false);
        dma_channel_configure(ch_ctl, &d, &dma_hw->ch[ch_emu].al3_read_addr_trig, &emu_base, 1, false);
    }

    irq_set_enabled(PIO2_IRQ_0, true);
    pio_sm_set_enabled(pio, sm_cs, true);
    pio_sm_set_enabled(pio, sm_rx, true);
    const uint32_t s = spin_lock_blocking(lock);
    admitting = true;
    arm_locked();
    spin_unlock(lock, s);
    if (selftest) {
        dma_channel_start(ch_emu);
        pio_sm_set_enabled(pio, sm_emu, true);
    }
}

void pvspi_pause(void) {
    const uint32_t s = spin_lock_blocking(lock);
    admitting = false;
    gpio_put(PIN_READY, 0);
    spin_unlock(lock, s);
}

void pvspi_stop(void) {
    gpio_put(PIN_READY, 0);
    pio_set_sm_mask_enabled(pio, (1u << sm_rx) | (1u << sm_cs) | (1u << sm_emu), false);
    irq_set_enabled(PIO2_IRQ_0, false);
    dma_channel_abort(ch_rx);
    dma_sniffer_disable();
    if (emu_on) {
        dma_channel_config d = dma_get_channel_config(ch_emu);
        channel_config_set_chain_to(&d, ch_emu);             // break the loop before aborting
        dma_channel_set_config(ch_emu, &d, false);
        dma_channel_abort(ch_ctl);
        dma_channel_abort(ch_emu);
        pio_sm_set_pins_with_mask(pio, sm_emu, 1u << PIN_CS, 1u << PIN_CS);
    }
    pio_sm_set_consecutive_pindirs(pio, sm_rx, PIN_SCK, 3, false);
    for (uint p = PIN_SCK; p <= PIN_CS; p++) gpio_set_dir(p, GPIO_IN), gpio_set_function(p, GPIO_FUNC_SIO);
}

// Consumer: packets of the oldest closed slot; a slot is released (and READY re-evaluated) once
// its packets have been handed out and the caller asks for the next one.

uint8_t *__not_in_flash_func(pvspi_next_packet)(void) {
    for (;;) {
        if (msg && ipk < npk) return msg + 12 + PV_TS * ipk++;
        if (msg) {
            const uint32_t s = spin_lock_blocking(lock);
            pvspi.tail++;
            arm_locked();
            spin_unlock(lock, s);
            msg = 0;
        }
        if (pvspi.tail == pvspi.head) return 0;
        __dmb();
        msg = (uint8_t *)slot(pvspi.tail);
        npk = pv_accept(msg, slot_crc[pvspi.tail % PV_SLOTS]), ipk = 0;
        if (npk <= 0) npk = 0;                               // bad or NOP: released next round
    }
}
