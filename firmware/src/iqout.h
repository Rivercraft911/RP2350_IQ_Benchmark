// Continuous I/Q output: PIO drives D[15:0] + CLK_IO, fed by a two-channel chained DMA ring.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define SEQ_IDLE 0xFFFFFFFFu
#define IQOUT_MAX_CPW 32        // PIO delay-field limit, see iqout.c

typedef struct {
    uint32_t *buf;                   // n_blocks * block_words
    uint32_t block_words, n_blocks;
    volatile uint32_t ready[16];     // seq + 1 once the slot holds block seq
    volatile uint32_t next_arm;      // next seq to hand to the DMA
    volatile uint32_t done;          // real blocks the DMA has finished reading
    volatile uint32_t underruns;     // arm time: block not ready, idle block sent instead
    volatile uint32_t own_errors;    // arm time: slot holds a newer seq (overwritten early)
    volatile uint32_t idle_sent;
} ring_t;

extern ring_t ring;

void iqout_init(uint32_t *buf, uint32_t block_words, uint32_t n_blocks, int cycles_per_word,
                int layout);
void iqout_start(void);              // first two blocks must be ready
void iqout_stop(void);
bool iqout_take_txstall(void);       // PIO TX FIFO ran dry since last call

// Capture the output pins with a second SM started in sync with the output SM. Discard the
// first IQOUT_CAP_SKIP words (stale FIFO contents). Each word holds two consecutive 16-bit bus words.
#define IQOUT_CAP_SKIP 8
void iqout_capture_start(uint32_t *dst, uint32_t nwords);
bool iqout_capture_busy(void);
