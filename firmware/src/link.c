#include "link.h"

#include "config.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/pio.h"

uint32_t link_ring[LINK_RING_WORDS] __attribute__((aligned(4 * LINK_RING_WORDS)));
volatile uint32_t link_total, link_overruns;

static PIO const pio = pio1;
static uint sm_rx, sm_tx, off_rx, off_tx;
static int ch_rx = -1, ch_tx = -1;
static uint16_t prog_rx[3], prog_tx[4];
static uint32_t last_idx;
// READY is re-evaluated only between blocks (<= ~100 us apart). At 4 lanes x 10.7 MHz = 42.7 Mb/s
// up to ~135 words can arrive after the ring should have closed; 1024 words covers that with the
// conservative consumed pointer (history of the oldest untransmitted block).
enum { READY_MARGIN = 1024, RING_BITS = 15 };   // 2^15 bytes = LINK_RING_WORDS words (DMA max)

// Emulated host: per 32-bit word wait for READY, then 32/lanes groups, data on SCK low, SCK rising
// mid-bit. Receiver: sample on each rising SCK edge (2-cycle input synchroniser plus wait latency,
// so half >= 4 keeps the sample inside the stable window).
static void build(int lanes, int half) {
    const uint d = (uint)half - 1, n = 32u / (uint)lanes - 1;
    prog_tx[0] = pio_encode_wait_gpio(true, PIN_IN_READY) | pio_encode_sideset(1, 0);
    prog_tx[1] = pio_encode_set(pio_x, n) | pio_encode_sideset(1, 0);
    prog_tx[2] = pio_encode_out(pio_pins, (uint)lanes) | pio_encode_sideset(1, 0) | pio_encode_delay(d);
    prog_tx[3] = pio_encode_jmp_x_dec(2) | pio_encode_sideset(1, 1) | pio_encode_delay(d);
    prog_rx[0] = pio_encode_wait_gpio(false, PIN_IN_CLK);
    prog_rx[1] = pio_encode_wait_gpio(true, PIN_IN_CLK);
    prog_rx[2] = pio_encode_in(pio_pins, (uint)lanes);
}

void link_init(int lanes, int half, const uint32_t *src) {
    if (ch_rx < 0) {
        ch_rx = dma_claim_unused_channel(true);
        ch_tx = dma_claim_unused_channel(true);
        sm_rx = pio_claim_unused_sm(pio, true);
        sm_tx = pio_claim_unused_sm(pio, true);
        gpio_init(PIN_IN_READY);
        gpio_set_dir(PIN_IN_READY, GPIO_OUT);
    } else {
        pio_remove_program(pio, &(pio_program_t){.instructions = prog_rx, .length = 3}, off_rx);
        pio_remove_program(pio, &(pio_program_t){.instructions = prog_tx, .length = 4}, off_tx);
    }
    gpio_put(PIN_IN_READY, 0);
    build(lanes, half);
    off_rx = pio_add_program(pio, &(pio_program_t){.instructions = prog_rx, .length = 3, .origin = -1});
    off_tx = pio_add_program(pio, &(pio_program_t){.instructions = prog_tx, .length = 4, .origin = -1});

    for (uint p = PIN_IN_CLK; p <= PIN_IN_D0 + 3; p++) pio_gpio_init(pio, p);
    pio_sm_set_consecutive_pindirs(pio, sm_tx, PIN_IN_CLK, 1, true);
    pio_sm_set_consecutive_pindirs(pio, sm_tx, PIN_IN_D0, (uint)lanes, true);

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, off_tx, off_tx + 3);
    sm_config_set_out_pins(&c, PIN_IN_D0, (uint)lanes);
    sm_config_set_sideset(&c, 1, false, false);
    sm_config_set_sideset_pins(&c, PIN_IN_CLK);
    sm_config_set_out_shift(&c, true, true, 32);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
    pio_sm_init(pio, sm_tx, off_tx, &c);

    c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, off_rx, off_rx + 2);
    sm_config_set_in_pins(&c, PIN_IN_D0);
    sm_config_set_in_shift(&c, true, true, 32);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
    pio_sm_init(pio, sm_rx, off_rx, &c);

    dma_channel_config d = dma_channel_get_default_config(ch_rx);
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, false);
    channel_config_set_write_increment(&d, true);
    channel_config_set_ring(&d, true, RING_BITS);
    channel_config_set_dreq(&d, pio_get_dreq(pio, sm_rx, false));
    dma_channel_configure(ch_rx, &d, link_ring, &pio->rxf[sm_rx], dma_encode_endless_transfer_count(),
                          false);
    d = dma_channel_get_default_config(ch_tx);
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, true);
    channel_config_set_write_increment(&d, false);
    channel_config_set_ring(&d, false, RING_BITS);
    channel_config_set_dreq(&d, pio_get_dreq(pio, sm_tx, true));
    dma_channel_configure(ch_tx, &d, &pio->txf[sm_tx], src, dma_encode_endless_transfer_count(),
                          false);
}

void link_start(void) {
    link_total = link_overruns = last_idx = 0;
    dma_channel_start(ch_rx);
    dma_channel_start(ch_tx);
    pio_sm_set_enabled(pio, sm_rx, true);   // receiver first so no edge is missed
    pio_sm_set_enabled(pio, sm_tx, true);
    gpio_put(PIN_IN_READY, 1);
}

void link_stop(void) {
    gpio_put(PIN_IN_READY, 0);
    pio_set_sm_mask_enabled(pio, (1u << sm_rx) | (1u << sm_tx), false);
    dma_channel_abort(ch_tx);
    dma_channel_abort(ch_rx);
    pio_sm_clear_fifos(pio, sm_rx);
    pio_sm_clear_fifos(pio, sm_tx);
    pio_sm_restart(pio, sm_rx);
    pio_sm_restart(pio, sm_tx);
    pio_sm_exec(pio, sm_rx, pio_encode_jmp(off_rx));
    pio_sm_exec(pio, sm_tx, pio_encode_jmp(off_tx));
}

void __not_in_flash_func(link_poll)(uint32_t consumed) {
    const uint32_t idx = (dma_hw->ch[ch_rx].write_addr - (uintptr_t)link_ring) / 4;
    link_total += (idx - last_idx) & (LINK_RING_WORDS - 1);
    last_idx = idx;
    const uint32_t fill = link_total - consumed;
    if (fill > LINK_RING_WORDS) link_overruns++;
    gpio_put(PIN_IN_READY, fill + READY_MARGIN < LINK_RING_WORDS);
}
