// Experimental output-interface component test, USB-only bench board. No encoder, RRC,
// AFE7071, differential clock receiver, or analog timing qualification is exercised here.
#include "bench.h"

#include <stdio.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/pio.h"
#include "hardware/resets.h"
#include "hardware/structs/hstx_ctrl.h"
#include "hardware/structs/hstx_fifo.h"
#include "pico/stdlib.h"
#include "tx.h"

#include "git_rev.h"

void bench_hstx_ddr(int ms) {
    PIO pio = pio1;
    const int out = pio_claim_unused_sm(pio, false);
    const int data = pio_claim_unused_sm(pio, false);
    const int clk = pio_claim_unused_sm(pio, false);
    if (out < 0 || data < 0 || clk < 0) {
        if (out >= 0) pio_sm_unclaim(pio, out);
        if (data >= 0) pio_sm_unclaim(pio, data);
        if (clk >= 0) pio_sm_unclaim(pio, clk);
        printf("@{\"error\":\"ddrtest needs three unused PIO1 state machines\"}\n");
        return;
    }
    // Sample one data word every 17 clocks. This checks many ring locations while leaving
    // DMA bandwidth for the 64-Mword/s output stream and the one-bit clock capture.
    const uint16_t instructions[] = {pio_encode_out(pio_pins, 16),
                                     pio_encode_in(pio_pins, 16) | pio_encode_delay(16),
                                     pio_encode_in(pio_pins, 1)};
    const pio_program_t po = {.instructions = instructions, .length = 3, .origin = -1};
    if (!pio_can_add_program(pio, &po)) {
        pio_sm_unclaim(pio, out); pio_sm_unclaim(pio, data); pio_sm_unclaim(pio, clk);
        printf("@{\"error\":\"ddrtest PIO program space unavailable\"}\n");
        return;
    }
    const uint oo = pio_add_program(pio, &po), od = oo + 1, oc = oo + 2;
    // Ring addressing wraps at 32 KiB; choose an aligned region inside the 128 KiB scratch.
    uint32_t *samples = (uint32_t *)(((uintptr_t)ring_buf + 32767u) & ~(uintptr_t)32767u);
    uint32_t rng = SEED;
    prbs_fill(samples, 8192, &rng);
    for (uint32_t i = 0; i < 8192; i++) samples[i] &= 0x7fff7fffu;
    for (uint p = PIN_D0; p < PIN_D0 + 16; p++) {
        pio_gpio_init(pio, p);
        gpio_set_slew_rate(p, GPIO_SLEW_RATE_FAST);
        gpio_set_drive_strength(p, BUS_DRIVE);
    }
    pio_sm_set_consecutive_pindirs(pio, out, PIN_D0, 16, true);
    pio_sm_config cfg_out = pio_get_default_sm_config();
    sm_config_set_wrap(&cfg_out, oo, oo);
    sm_config_set_out_pins(&cfg_out, PIN_D0, 16);
    sm_config_set_out_shift(&cfg_out, true, true, 32);
    sm_config_set_fifo_join(&cfg_out, PIO_FIFO_JOIN_TX);
    pio_sm_init(pio, out, oo, &cfg_out);
    pio_sm_config cfg_data = pio_get_default_sm_config();
    sm_config_set_wrap(&cfg_data, od, od);
    sm_config_set_in_pins(&cfg_data, PIN_D0);
    sm_config_set_in_shift(&cfg_data, true, true, 32);
    sm_config_set_fifo_join(&cfg_data, PIO_FIFO_JOIN_RX);
    pio_sm_init(pio, data, od, &cfg_data);
    pio_sm_config cfg_clk = pio_get_default_sm_config();
    sm_config_set_wrap(&cfg_clk, oc, oc);
    sm_config_set_in_pins(&cfg_clk, PIN_CLKIO);
    sm_config_set_in_shift(&cfg_clk, true, true, 32);
    sm_config_set_fifo_join(&cfg_clk, PIO_FIFO_JOIN_RX);
    pio_sm_init(pio, clk, oc, &cfg_clk);
    const uint32_t sync_save = pio->input_sync_bypass;
    pio->input_sync_bypass |= 0x1ffffu;

    // GPIO16 is only a single-ended test clock here. AFE7071 DDR requires a suitable
    // differential clock on DACCLKP/N, not DDR on CLK_IO; buffering/routing is untested.
    // Infinite RAW_REPEAT holds the shifter running without CPU/DMA feeding it;
    // CLKDIV=2, CLKPHASE=1 requests half-cycle clock-edge placement.
    clock_configure(clk_hstx, 0, CLOCKS_CLK_HSTX_CTRL_AUXSRC_VALUE_CLK_SYS,
                    clock_get_hz(clk_sys), clock_get_hz(clk_sys));
    reset_block(RESETS_RESET_HSTX_BITS);
    unreset_block_wait(RESETS_RESET_HSTX_BITS);
    hstx_ctrl_hw->csr = 0;
    hstx_ctrl_hw->bit[4] = HSTX_CTRL_BIT0_CLK_BITS;
    hstx_ctrl_hw->expand_shift = 1u << HSTX_CTRL_EXPAND_SHIFT_RAW_N_SHIFTS_LSB;
    hstx_ctrl_hw->csr = (2u << HSTX_CTRL_CSR_CLKDIV_LSB) |
        (1u << HSTX_CTRL_CSR_CLKPHASE_LSB) | (1u << HSTX_CTRL_CSR_N_SHIFTS_LSB) |
        HSTX_CTRL_CSR_EXPAND_EN_BITS;
    gpio_set_function(PIN_CLKIO, GPIO_FUNC_HSTX);
    gpio_set_slew_rate(PIN_CLKIO, GPIO_SLEW_RATE_FAST);
    gpio_set_drive_strength(PIN_CLKIO, BUS_DRIVE);
    hstx_fifo_hw->fifo = 0x1000u;  // RAW_REPEAT, infinite count
    hstx_fifo_hw->fifo = 0;
    hw_set_bits(&hstx_ctrl_hw->csr, HSTX_CTRL_CSR_EN_BITS);

    const uint ch_out = dma_claim_unused_channel(true), ch_clk = dma_claim_unused_channel(true),
               ch_data = dma_claim_unused_channel(true);
    dma_channel_config dc = dma_channel_get_default_config(ch_out);
    channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
    channel_config_set_read_increment(&dc, true);
    channel_config_set_ring(&dc, false, 15);
    channel_config_set_dreq(&dc, pio_get_dreq(pio, out, true));
    channel_config_set_high_priority(&dc, true);
    dma_channel_configure(ch_out, &dc, &pio->txf[out], samples, dma_encode_endless_transfer_count(), true);
    while (!pio_sm_is_tx_fifo_full(pio, out)) tight_loop_contents();
    pio->fdebug = 1u << (PIO_FDEBUG_TXSTALL_LSB + out);
    dc = dma_channel_get_default_config(ch_clk);
    channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
    channel_config_set_read_increment(&dc, false);
    channel_config_set_write_increment(&dc, true);
    channel_config_set_dreq(&dc, pio_get_dreq(pio, clk, false));
    dma_channel_configure(ch_clk, &dc, cap_buf, &pio->rxf[clk], 4096, false);
    dc = dma_channel_get_default_config(ch_data);
    channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
    channel_config_set_read_increment(&dc, false);
    channel_config_set_write_increment(&dc, true);
    channel_config_set_dreq(&dc, pio_get_dreq(pio, data, false));
    dma_channel_configure(ch_data, &dc, cap_buf + 4096, &pio->rxf[data], 4096, false);
    dma_start_channel_mask((1u << ch_clk) | (1u << ch_data));
    const uint32_t start = time_us_32();
    pio_sm_set_enabled(pio, out, true);
    while (time_us_32() - start < (uint32_t)ms * 500u) tight_loop_contents();
    const uint32_t capture_start_us = time_us_32() - start;
    pio_enable_sm_mask_in_sync(pio, (1u << data) | (1u << clk));
    while (time_us_32() - start < (uint32_t)ms * 1000u) tight_loop_contents();
    const uint32_t elapsed_us = time_us_32() - start;
    pio_set_sm_mask_enabled(pio, (1u << out) | (1u << data) | (1u << clk), false);
    const bool stalled = pio->fdebug & (1u << (PIO_FDEBUG_TXSTALL_LSB + out));
    const uint32_t clock_words = 4096u - dma_hw->ch[ch_clk].transfer_count;
    const uint32_t data_words = 4096u - dma_hw->ch[ch_data].transfer_count;
    dma_channel_abort(ch_out);
    dma_channel_abort(ch_clk);
    dma_channel_abort(ch_data);
    hw_clear_bits(&hstx_ctrl_hw->csr, HSTX_CTRL_CSR_EN_BITS);
    const uint32_t clock_pattern = clock_words > 8 ? cap_buf[8] : 0;
    uint32_t bad_clock = 0;
    for (uint32_t i = 8; i < clock_words; i++)
        if ((clock_pattern != 0x55555555u && clock_pattern != 0xaaaaaaaau) ||
            cap_buf[i] != clock_pattern) bad_clock++;
    const uint16_t *got = (const uint16_t *)(cap_buf + 4096);
    const uint32_t got_n = 2 * data_words;
    const uint16_t *want = (const uint16_t *)samples;
    int offset = -1;
    // Capture starts during steady output. Every captured word must match the ring at the
    // documented fixed stride; alignment cannot discard errors or splice stream segments.
    if (got_n >= 32) for (int o = 0; o < 16384; o++) {
        bool match = true;
        for (uint32_t j = 0; j < got_n; j++)
            if (got[j] != want[(o + j * 17) % 16384]) { match = false; break; }
        if (match) { offset = o; break; }
    }
    pio->input_sync_bypass = sync_save;
    dma_channel_unclaim(ch_out); dma_channel_unclaim(ch_clk); dma_channel_unclaim(ch_data);
    pio_remove_program(pio, &po, oo);
    pio_sm_unclaim(pio, out); pio_sm_unclaim(pio, data); pio_sm_unclaim(pio, clk);
    for (uint p = PIN_D0; p <= PIN_CLKIO; p++) gpio_init(p);
    printf("@{\"cmd\":\"ddrtest\",\"git\":\"%s\",\"clk_hz\":%lu,\"ms\":%d,"
           "\"elapsed_us\":%lu,\"bus_words_s_target\":%lu,\"dac_samples_s_target\":%lu,"
           "\"txstall\":%u,\"clock_words\":%lu,\"clock_pattern\":%lu,\"bad_clock_words\":%lu,"
           "\"data_bus_words\":%lu,\"data_match\":%u,\"data_offset\":%d,"
           "\"data_stride_words\":17,\"capture_start_us\":%lu,\"data_checked_words\":%lu,\"data_head\":[",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), ms, (unsigned long)elapsed_us,
           (unsigned long)clock_get_hz(clk_sys), (unsigned long)clock_get_hz(clk_sys)/2,
           stalled, (unsigned long)clock_words, (unsigned long)clock_pattern, (unsigned long)bad_clock,
           (unsigned long)got_n, offset >= 0, offset, (unsigned long)capture_start_us,
           (unsigned long)(offset >= 0 ? got_n : 0));
    for (uint32_t i = 0; i < 16 && i < got_n; i++) printf("%s%u", i ? "," : "", got[i]);
    printf("],\"source_head\":[");
    for (uint32_t i = 0; i < 16; i++) printf("%s%u", i ? "," : "", want[i]);
    printf("],\"clock_head\":[");
    for (uint32_t i = 0; i < 16 && i < clock_words; i++)
        printf("%s%lu", i ? "," : "", (unsigned long)cap_buf[i]);
    printf("]}\n");
}
