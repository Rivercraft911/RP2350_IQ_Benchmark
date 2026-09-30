#include "iqout.h"

#include "config.h"
#include "hardware/dma.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/sync.h"

ring_t ring;

static PIO const pio = pio0;
static uint sm_out, sm_cap, off_out, off_cap;
static int ch[2] = {-1, -1}, ch_cap = -1;
static uint32_t ch_seq[2];
static uint16_t prog_out[2], prog_cap[3];
static uint32_t idle_block[MAX_SPS * BLOCK_SYMS];

// out pins,16 side 0 [d0] ; nop side 1 [d1]: data changes, CLK_IO rises floor(cpw/2) cycles
// later and falls with the next data change. AFE7071 needs >= 1 ns setup and hold at CLK_IO.
static void build_programs(int cpw) {
    const uint d0 = (uint)cpw / 2 - 1, d1 = (uint)cpw - 2 - d0;   // cpw >= 2; odd cpw allowed
    prog_out[0] = pio_encode_out(pio_pins, 16) | pio_encode_sideset(1, 0) | pio_encode_delay(d0);
    prog_out[1] = pio_encode_nop() | pio_encode_sideset(1, 1) | pio_encode_delay(d1);
    // Capture: two samples cpw apart, then push noblock so the SM never stalls or slips phase.
    prog_cap[0] = pio_encode_in(pio_pins, 16) | pio_encode_delay((uint)cpw - 1);
    prog_cap[1] = pio_encode_in(pio_pins, 16) | pio_encode_delay((uint)cpw - 2);
    prog_cap[2] = pio_encode_push(false, false);
}

static void __not_in_flash_func(arm)(int c) {
    const uint32_t s = ring.next_arm, slot = s % ring.n_blocks, r = ring.ready[slot];
    const uint32_t *src;
    if (r == s + 1) {
        src = ring.buf + slot * ring.block_words;
        ch_seq[c] = s;
        ring.next_arm = s + 1;
    } else {
        if (r > s + 1) ring.own_errors++;
        ring.underruns++;
        ring.idle_sent++;
        src = idle_block;
        ch_seq[c] = SEQ_IDLE;
    }
    dma_channel_set_read_addr(ch[c], src, false);
}

static void __not_in_flash_func(dma_isr)(void) {
    for (int c = 0; c < 2; c++) {
        if (!dma_channel_get_irq0_status(ch[c])) continue;
        dma_channel_acknowledge_irq0(ch[c]);
        if (ch_seq[c] != SEQ_IDLE) ring.done = ch_seq[c] + 1;
        arm(c);   // the other channel is already running; this one restarts on its chain
    }
}

void iqout_init(uint32_t *buf, uint32_t block_words, uint32_t n_blocks, int cpw) {
    ring = (ring_t){.buf = buf, .block_words = block_words, .n_blocks = n_blocks};
    for (uint32_t i = 0; i < block_words; i++) idle_block[i] = 1u << 14;  // zero, IQ_FLAG on I

    if (ch[0] < 0) {
        ch[0] = dma_claim_unused_channel(true);
        ch[1] = dma_claim_unused_channel(true);
        ch_cap = dma_claim_unused_channel(true);
        sm_out = pio_claim_unused_sm(pio, true);
        sm_cap = pio_claim_unused_sm(pio, true);
        irq_set_exclusive_handler(DMA_IRQ_0, dma_isr);
    } else {
        pio_remove_program(pio, &(pio_program_t){prog_out, 2, -1, 0}, off_out);
        pio_remove_program(pio, &(pio_program_t){prog_cap, 3, -1, 0}, off_cap);
    }
    build_programs(cpw);
    const pio_program_t po = {prog_out, 2, -1, 0}, pc = {prog_cap, 3, -1, 0};
    off_out = pio_add_program(pio, &po);
    off_cap = pio_add_program(pio, &pc);

    for (uint p = PIN_D0; p < PIN_D0 + 16; p++) pio_gpio_init(pio, p);
    pio_gpio_init(pio, PIN_CLKIO);
    pio_sm_set_consecutive_pindirs(pio, sm_out, PIN_D0, 16, true);
    pio_sm_set_consecutive_pindirs(pio, sm_out, PIN_CLKIO, 1, true);

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, off_out, off_out + 1);
    sm_config_set_out_pins(&c, PIN_D0, 16);
    sm_config_set_sideset(&c, 1, false, false);
    sm_config_set_sideset_pins(&c, PIN_CLKIO);
    sm_config_set_out_shift(&c, true, true, 32);          // LSB first: I slot, then Q slot
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
    sm_config_set_clkdiv_int_frac8(&c, 1, 0);
    pio_sm_init(pio, sm_out, off_out, &c);

    c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, off_cap, off_cap + 2);
    sm_config_set_in_pins(&c, PIN_D0);
    sm_config_set_in_shift(&c, true, false, 32);          // first sample lands in [15:0]
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
    sm_config_set_clkdiv_int_frac8(&c, 1, 0);
    pio_sm_init(pio, sm_cap, off_cap, &c);

    for (int i = 0; i < 2; i++) {
        dma_channel_config d = dma_channel_get_default_config(ch[i]);
        channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
        channel_config_set_read_increment(&d, true);
        channel_config_set_write_increment(&d, false);
        channel_config_set_dreq(&d, pio_get_dreq(pio, sm_out, true));
        channel_config_set_chain_to(&d, ch[i ^ 1]);
        channel_config_set_high_priority(&d, true);
        dma_channel_configure(ch[i], &d, &pio->txf[sm_out], idle_block, block_words, false);
    }
}

void iqout_start(void) {
    ring.next_arm = 0;
    arm(0);
    arm(1);
    dma_channel_acknowledge_irq0(ch[0]);
    dma_channel_acknowledge_irq0(ch[1]);
    dma_channel_set_irq0_enabled(ch[0], true);
    dma_channel_set_irq0_enabled(ch[1], true);
    irq_set_enabled(DMA_IRQ_0, true);
    dma_channel_start(ch[0]);
    while (!pio_sm_is_tx_fifo_full(pio, sm_out)) tight_loop_contents();
    pio->fdebug = 1u << (PIO_FDEBUG_TXSTALL_LSB + sm_out);
    pio_enable_sm_mask_in_sync(pio, (1u << sm_out) | (1u << sm_cap));
}

void iqout_stop(void) {
    pio_set_sm_mask_enabled(pio, (1u << sm_out) | (1u << sm_cap), false);
    irq_set_enabled(DMA_IRQ_0, false);
    for (int i = 0; i < 2; i++) {
        dma_channel_set_irq0_enabled(ch[i], false);
        dma_channel_config d = dma_get_channel_config(ch[i]);
        channel_config_set_chain_to(&d, ch[i]);           // no re-trigger during abort
        dma_channel_set_config(ch[i], &d, false);
    }
    dma_channel_abort(ch[0]);
    dma_channel_abort(ch[1]);
    dma_channel_acknowledge_irq0(ch[0]);
    dma_channel_acknowledge_irq0(ch[1]);
    for (int i = 0; i < 2; i++) {                         // restore ping-pong chaining
        dma_channel_config d = dma_get_channel_config(ch[i]);
        channel_config_set_chain_to(&d, ch[i ^ 1]);
        dma_channel_set_config(ch[i], &d, false);
    }
    pio_sm_clear_fifos(pio, sm_out);
    pio_sm_clear_fifos(pio, sm_cap);
    pio_sm_restart(pio, sm_out);
    pio_sm_restart(pio, sm_cap);
    pio_sm_exec(pio, sm_out, pio_encode_jmp(off_out));
    pio_sm_exec(pio, sm_cap, pio_encode_jmp(off_cap));
}

bool iqout_take_txstall(void) {
    const uint32_t bit = 1u << (PIO_FDEBUG_TXSTALL_LSB + sm_out);
    if (!(pio->fdebug & bit)) return false;
    pio->fdebug = bit;
    return true;
}

void iqout_capture_start(uint32_t *dst, uint32_t n) {
    dma_channel_config d = dma_channel_get_default_config(ch_cap);
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, false);
    channel_config_set_write_increment(&d, true);
    channel_config_set_dreq(&d, pio_get_dreq(pio, sm_cap, false));
    channel_config_set_high_priority(&d, true);
    // The joined RX FIFO (8 entries) holds stale samples from before the start and a gap
    // follows them; the caller discards the first 8 words.
    dma_channel_configure(ch_cap, &d, dst, &pio->rxf[sm_cap], n, true);
}

bool iqout_capture_busy(void) { return dma_channel_is_busy(ch_cap); }
