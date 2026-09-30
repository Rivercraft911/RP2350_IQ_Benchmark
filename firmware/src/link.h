// Input link (stage 4): PIO receiver on GPIO17-21 (CLK, D0-D3) with READY on GPIO22, DMA into a
// 16 KiB ring. An on-chip emulator SM drives the same pins from a source ring, standing in for the
// Pi/payload SPI master so the full receive path runs without external hardware.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define LINK_RING_WORDS 4096u     // equals IN_WORDS so ring index = PRBS index

extern uint32_t link_ring[LINK_RING_WORDS];
extern volatile uint32_t link_total;      // words received since start (monotonic)
extern volatile uint32_t link_overruns;   // polls that found unconsumed data overwritten

// lanes: 1, 2 or 4. half: system clocks per SCK half period (>= 4). src must be 16 KiB aligned.
void link_init(int lanes, int half, const uint32_t *src);
void link_start(void);
void link_stop(void);
// Core 0 only. consumed = index of the oldest word still needed. Updates link_total and READY.
void link_poll(uint32_t consumed);
