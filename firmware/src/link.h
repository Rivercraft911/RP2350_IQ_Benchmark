// Input link (stage 4): PIO receiver on GPIO17-21 (CLK, D0-D3) with READY on GPIO22, DMA into a
// 32 KiB ring. An on-chip emulator SM drives the same pins from a source ring, standing in for the
// Pi/payload SPI master so the full receive path runs without external hardware.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define LINK_RING_WORDS 8192u     // equals IN_WORDS so ring index = PRBS index

extern uint32_t link_ring[LINK_RING_WORDS];
extern volatile uint32_t link_total;      // words received since start (monotonic)
extern volatile uint32_t link_overruns;   // polls that found unconsumed data overwritten

// lanes: 1, 2 or 4. half: system clocks per SCK half period (>= 2; see results for the working
// minimum). src must be 16 KiB aligned.
void link_init(int lanes, int half, const uint32_t *src);
void link_start(void);
void link_stop(void);                   // also stops the poll timer
// Updates link_total and READY. Called from a 50 us repeating-timer interrupt while the link
// runs, so the poll interval stays bounded regardless of foreground work: at the fastest link
// (4 lanes x 32 MHz = 128 Mb/s) 50 us is 200 words, inside READY_MARGIN, and 40x shorter than
// one ring wrap (2 ms), so no wrap is lost. *consumed_word is the oldest word still needed.
void link_poll(uint32_t consumed);
void link_autopoll(volatile uint32_t *consumed_blocks, uint32_t words_per_block);
