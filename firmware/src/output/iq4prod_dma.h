// Four independent input DMAs and alternate-row PIO pointer producers.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "hardware/pio.h"

enum {
    IQ4PROD_INPUT_WORDS = 4096,
    IQ4PROD_BATCH_WORDS = 512,
    IQ4PROD_ERR_CONFIG = 1u << 0,
    IQ4PROD_ERR_STARTUP = 1u << 1,
    IQ4PROD_ERR_DMA = 1u << 2,
    IQ4PROD_ERR_PIO = 1u << 3,
    IQ4PROD_ERR_STOP = 1u << 5,
    IQ4PROD_ERR_SOURCE_EMPTY = 1u << 6,
    IQ4PROD_ERR_OWNERSHIP = 1u << 7,
};

typedef struct {
    // Completed finite chunks in live mode; conservative source retirement,
    // not physical output position. The minimum is safe for ring reuse.
    uint64_t input_words[4], granted_input_words[4], reusable_words, published_words, granted_words;
    uint32_t errors, txstall_mask, rxstall_mask;
    uint32_t worker_polls, worker_max_gap_cycles;
    uint64_t worker_service_cycles;
    // IRQ mode only: handler/foreground-kick calls and the longest bounded
    // service body. This is not completion-to-entry interrupt latency.
    uint32_t irq_calls, irq_max_service_cycles;
    uint64_t irq_service_cycles;
    int irq_index;
    bool running;
} iq4prod_progress_t;

typedef struct {
    uint32_t reusable_words, published_words, errors;
} iq4prod_irq_poll_t;

// Stream order I-even,Q-even,I-odd,Q-odd. Streams0..2 use PIO0; stream3
// uses PIO1, leaving PIO0 one SM for the DDR clock and PIO1 capture space.
// Inputs are 4096-word,16-KiB-aligned rings, shared by each axis's producers.
// Cyclic input is immutable. Live publication is a monotonic complete-word
// total for BOTH rings, at most one ring ahead of reusable_words; DMA grants
// complete512-word chunks with the original worker; IRQ mode also admits a
// shorter published suffix. Finite grants protect ownership even when
// software service misses its FIFO deadline: the source stops, never replays.
bool iq4prod_dma_prepare(const uint32_t *input_i, const uint32_t *input_q,
                     const uint32_t *table, uint32_t previous_i, uint32_t previous_q,
                     bool live, uint64_t published_words);
// Opt-in live source: finite grants rearmed by a high-priority core0 DMA IRQ,
// leaving core1 available for FEC. No source replay on late IRQ/publication.
// Claims an otherwise unused DMA IRQ2/3 and restores its priority on stop.
bool iq4prod_dma_prepare_irq(const uint32_t *input_i, const uint32_t *input_q,
                     const uint32_t *table, uint32_t previous_i, uint32_t previous_q,
                     uint64_t published_words);
bool iq4prod_dma_start(void);
iq4prod_progress_t iq4prod_dma_progress(void);
// Core0-only bounded supervision for the opt-in IRQ backend. Every completed
// cursor is monotonic, so independently read32-bit cursors give conservative
// retirement without copying the full diagnostic snapshot/result structures.
iq4prod_irq_poll_t iq4prod_dma_poll_irq(void);
iq4prod_irq_poll_t iq4prod_dma_poll_light(void);
iq4prod_progress_t iq4prod_dma_publish(uint64_t published_words);
const volatile uint32_t *iq4prod_dma_rx_fifo(unsigned stream);
unsigned iq4prod_dma_rx_dreq(unsigned stream);
int iq4prod_dma_pio_index(unsigned stream);
int iq4prod_dma_sm(unsigned stream);
// Stop output writers/clock first. Joins the core1 worker before DMA abort;
// retains claims on abort failure. Final progress survives release.
void iq4prod_dma_stop(void);
const char *iq4prod_dma_last_error(void);
