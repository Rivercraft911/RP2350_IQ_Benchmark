// Raw packed-QPSK input -> pin-free PIO four-symbol LUT addresses.
// Experimental producer only; the output engine must consume both RX FIFOs.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "iqquad.h"

enum {
    IQQUADPTR_PROGRAM_LENGTH = 10,
    IQQUADPTR_L8_PROGRAM_LENGTH = 13,
    IQQUADPTR_BATCH_WORDS = 512,
    IQQUADPTR_GRANT_ENTRIES = 16,
    IQQUADPTR_REUSE_GUARD_WORDS = 8,
    // Conservative pending-sample bound with coupled paired-output consumers.
    // Independent RX FIFO drains do not give a bounded physical output cursor.
    IQQUADPTR_LOOKAHEAD_SYMBOLS = 128,
};
#define IQQUADPTR_TABLE_CRC 0x9da111d1u
#define IQQUADPTR_L8_TABLE_CRC 0xccad7f5au

// IN shifts left; OUT shifts right, manual PULL threshold16. Raw halfwords keep
// chronological bits0..15. X holds newest nibble low, oldest nibble high, while
// preserving bit order within each nibble. Y is the128KiB-aligned table base>>17.
static const uint16_t IQQUADPTR_PROGRAM[IQQUADPTR_PROGRAM_LENGTH] = {
    0x00e2, // JMP !OSRE, have
    0x80a0, // PULL BLOCK
    0xa0c1, // have: MOV ISR, X
    0x40e4, // IN OSR,4
    0x6064, // OUT NULL,4
    0xa026, // MOV X,ISR
    0xa0c2, // MOV ISR,Y
    0x402c, // IN X,12
    0x4065, // IN NULL,5
    0x8020, // PUSH BLOCK; wrap
};

// L8 keeps eleven history bits. Y contains byte-swapped table base[31:16];
// the consumer's 32-bit control DMA MUST byte-swap RX values into addresses.
static const uint16_t IQQUADPTR_L8_PROGRAM[IQQUADPTR_L8_PROGRAM_LENGTH] = {
    0x00e2, // JMP !OSRE, have
    0x80a0, // PULL BLOCK
    0xa0c1, // have: MOV ISR,X
    0x40e4, // IN OSR,4
    0x6064, // OUT NULL,4
    0xa026, // MOV X,ISR
    0xa0d1, // MOV ISR,::X
    0x4069, // IN NULL,9
    0xa0d6, // MOV ISR,::ISR; now X>>9
    0x4065, // IN NULL,5
    0x4028, // IN X,8
    0x4050, // IN Y,16
    0x8020, // PUSH BLOCK; wrap
};

typedef struct {
    const uint16_t *program;
    unsigned program_length;
    unsigned cycles_per_input_word;
    unsigned table_alignment;
    uint32_t table_crc;
    bool control_bswap;
} iqquadptr_profile_info_t;

const iqquadptr_profile_info_t *iqquadptr_profile_info(iqquad_profile_t profile);
uint32_t iqquadptr_key_profile(uint32_t pio_key, iqquad_profile_t profile);
bool iqquadptr_build_table_profile(uint32_t *table, unsigned word_capacity, iqquad_profile_t profile);
uint32_t iqquadptr_key(uint32_t pio_key);
uint32_t iqquadptr_seed(uint16_t previous_axis);
// Build in place using the existing quad builder, then swap row pairs. No extra LUT.
void iqquadptr_build_table(uint32_t *table);

// Portable credit publication leaf used by the device and its native test.
// grants starts all-zero and is naturally64B-aligned on device. Arguments count
// complete packed input words. Caller owns source data and monotonically advances
// published/reusable; granted is the returned value from its previous call.
// A zero sentinel is installed before its preceding valid credit is released.
uint64_t iqquadptr_grant_until(volatile uint32_t *grants, uint64_t granted,
                             uint64_t published, uint64_t reusable);

#ifdef IQ_ON_DEVICE
#include "hardware/pio.h"

enum {
    IQQUADPTR_ERR_CONFIG = 1u << 0,
    IQQUADPTR_ERR_STARTUP = 1u << 1,
    IQQUADPTR_ERR_DMA = 1u << 2,
    IQQUADPTR_ERR_SOURCE_EMPTY = 1u << 3,
    IQQUADPTR_ERR_PIO = 1u << 4,
    IQQUADPTR_ERR_POLL_GAP = 1u << 5,
    IQQUADPTR_ERR_OWNERSHIP = 1u << 6,
    IQQUADPTR_ERR_STOP = 1u << 7,
};

typedef struct {
    // READ_ADDR can lead SRAM retirement. These are source-read issue bounds,
    // not the number of symbols physically emitted by the output PIO.
    uint64_t input_i_words, input_q_words;
    uint64_t input_low_words, input_high_words;
    // Both axes have retired every source word strictly before this bound.
    uint64_t reusable_words;
    uint64_t published_words, granted_words;
    uint32_t errors, txstall_mask, rxstall_mask, null_trigger_mask;
    bool running;
} iqquadptr_progress_t;

// Claims2 SMs on pointer_pio and4 DMA channels (two input/two credit).
// Input is a naturally aligned2^input_log2-word ring, input_log2=10..13.
// PIO RX is paced only by the future output control DMA, one channel per RX DREQ.
// No GPIO, IRQ handler, output DMA or output PIO is configured by this module.
bool iqquadptr_prepare(PIO pointer_pio, const uint32_t *input, unsigned input_log2,
                      const uint32_t *table, uint32_t previous, uint64_t published_words);
bool iqquadptr_prepare_profile(PIO pointer_pio, const uint32_t *input, unsigned input_log2,
                              const uint32_t *table, uint32_t previous, uint64_t published_words,
                              iqquad_profile_t profile);
// Starts input DMA, prefills TX FIFOs, then enables both pointer SMs together.
bool iqquadptr_start(void);
// Core0 only: acquire completed immutable input, publish bounded credits and poll.
// The caller must stop ALL output SMs/clock immediately when errors is nonzero.
iqquadptr_progress_t iqquadptr_publish(uint64_t published_words);
iqquadptr_progress_t iqquadptr_progress(void);
const volatile uint32_t *iqquadptr_rx_fifo(unsigned axis);
unsigned iqquadptr_rx_dreq(unsigned axis);
// Output must be stopped before this releases the producer's FIFO/DMA resources.
void iqquadptr_stop(void);
const char *iqquadptr_last_error(void);
#endif
