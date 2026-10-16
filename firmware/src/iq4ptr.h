// Cyclic immutable or credit-gated live 2-bit axes -> pin-free PIO full-history
// L7 LUT addresses.
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum {
    IQ4PTR_PROGRAM_LENGTH = 10,
    IQ4PTR_CYCLES_PER_WORD = 145,
    IQ4PTR_SYMBOLS_PER_WORD = 16,
    IQ4PTR_TABLE_BYTES = 128 * 1024,
};

// IN shifts left, OUT right, manual PULL threshold32. X retains the rolling
// history, newest code in bits1:0; Y is the128KiB-aligned table base>>17.
// Nine instructions/symbol plus one PULL per16 symbols. Both axes use one
// flag-free table: each14-bit key selects four uint16 samples, an8-byte row.
static const uint16_t IQ4PTR_PROGRAM[IQ4PTR_PROGRAM_LENGTH] = {
    0x00e2, // JMP !OSRE, have
    0x80a0, // PULL BLOCK
    0xa0c1, // have: MOV ISR,X
    0x40e2, // IN OSR,2
    0x6062, // OUT NULL,2
    0xa026, // MOV X,ISR
    0xa0c2, // MOV ISR,Y
    0x402e, // IN X,14
    0x4063, // IN NULL,3
    0x8020, // PUSH BLOCK; wrap
};

// A packed source word contains16 chronological codes, earliest in bits1:0.
// Seed the six-symbol prehistory from the immediately preceding packed word.
uint32_t iq4ptr_seed(uint32_t previous);

#ifdef IQ_ON_DEVICE
#include "hardware/pio.h"
enum {
    IQ4PTR_ERR_CONFIG = 1u << 0,
    IQ4PTR_ERR_STARTUP = 1u << 1,
    IQ4PTR_ERR_DMA = 1u << 2,
    IQ4PTR_ERR_PIO = 1u << 3,
    IQ4PTR_ERR_POLL_GAP = 1u << 4,
    IQ4PTR_ERR_STOP = 1u << 5,
    IQ4PTR_ERR_SOURCE_EMPTY = 1u << 6, // live: a zero grant sentinel was reached
    IQ4PTR_ERR_OWNERSHIP = 1u << 7,    // live: read beyond grants or bad publication
};

typedef struct {
    // Source read-issue counts, including prefetch, not physical output counts.
    uint64_t input_words[2];
    // Live mode only. Both axes have retired every word strictly before reusable_words.
    uint64_t reusable_words, published_words, granted_words;
    uint32_t errors, txstall_mask, rxstall_mask, null_trigger_mask;
    bool running;
} iq4ptr_progress_t;

// Claims two SMs and two DMA channels. Inputs are separate naturally aligned
// 2^input_log2-word rings (8..13); the caller keeps them immutable until stop.
// DMA ENDLESS plus address rings supplies cyclic input without CPU publication.
// The table occupies128KiB at an aligned address within SRAM0..7. No GPIO,
// output DMA/PIO, IRQ handler or live input ownership is configured here.
bool iq4ptr_prepare(PIO pio, const uint32_t *input_i, const uint32_t *input_q,
                   unsigned input_log2, const uint16_t *table,
                   uint32_t previous_i, uint32_t previous_q);
// Live mode: the same rings are filled by the caller. Each axis input DMA chains to a
// credit DMA that reloads its TRANS_COUNT from iqquadptr-style 512-word grants (four
// DMA channels in total). published_words counts complete code words written to both
// rings, at least one batch and at most one ring. Words become reusable only through
// iq4ptr_publish()'s reusable_words, never through the caller's own cursor.
bool iq4ptr_prepare_live(PIO pio, const uint32_t *input_i, const uint32_t *input_q,
                        unsigned input_log2, const uint16_t *table,
                        uint32_t previous_i, uint32_t previous_q, uint64_t published_words);
bool iq4ptr_start(void);
// Live mode, core 0 only: acquire newly completed words, grant whole batches, poll.
// Stop all output SMs and the DAC clock immediately when errors is nonzero.
iq4ptr_progress_t iq4ptr_publish(uint64_t published_words);
iq4ptr_progress_t iq4ptr_progress(void);
const volatile uint32_t *iq4ptr_rx_fifo(unsigned axis);
unsigned iq4ptr_rx_dreq(unsigned axis);
int iq4ptr_input_dma(unsigned axis);
// Caller stops output SMs and DAC clock before releasing these resources.
void iq4ptr_stop(void);
const char *iq4ptr_last_error(void);
#endif
