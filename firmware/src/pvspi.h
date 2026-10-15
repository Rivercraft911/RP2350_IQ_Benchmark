// PV-SPI v1 slave on the RP2350 (docs/pv-spi-spec.md): selected PIO receives mode 0 on GP17 (SCK),
// GP18 (MOSI), GP19 (CS_N); DMA writes each 1332-byte message into a queue slot; a CS_N rising
// edge interrupt closes the slot; READY on GP22 is high while a free slot is armed.
// Self-test: a third SM emulates the CM5 master on the same pins from a pattern buffer.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "hardware/pio.h"

#include "pvproto.h"

#define PV_SLOTS 24                          // 24 x 1332 B = 31 968 B, about 25 ms at 10.33 Mb/s
#define PV_EMU_MSGS 16                       // self-test: pattern messages, sent in a loop
#define PV_EMU_WORDS (PV_EMU_MSGS * PV_MSG_WORDS)

enum {
    PV_SPI_ERR_SHORT = 1u << 0,
    PV_SPI_ERR_LONG = 1u << 1,
    PV_SPI_ERR_LATE = 1u << 2,
    PV_SPI_ERR_OVERFLOW = 1u << 3,
    PV_SPI_ERR_DMA = 1u << 4,
};

typedef struct {
    volatile uint32_t head, tail;            // messages closed / consumed (monotonic)
    volatile uint32_t short_msgs, long_msgs, overflows;
    volatile uint32_t deferred_msgs, drain_timeouts, late_closes;
    volatile uint32_t last_short_words, last_short_fifo, last_short_ctrl, last_short_pc, last_short_cs;
    volatile uint32_t errors;               // sticky PV_SPI_ERR_*; cleared only by start
} pvspi_stats_t;

extern pvspi_stats_t pvspi;

// slots: PV_SLOTS * PV_MSG_WORDS words. emu: PV_EMU_WORDS when selftest (else unused).
// Start/stop on the IRQ-owning core, with the consumer quiescent during stop. The selected
// PIO must have three free SMs, 19 instruction slots and exclusive IRQ0 flag0 available.
// Its CS SM owns READY: one CPU token grants one assertion, and CS falling withdraws
// READY in hardware. The next token follows complete RX retirement and a new DMA arm.
void pvspi_start(PIO bus, uint32_t *slots, bool selftest, uint32_t *emu, int emu_half);
void pvspi_pause(void);                    // force READY low; no resume except a fresh start
void pvspi_stop(void);
uint8_t *pvspi_next_packet(void);            // consumer (one core): next accepted TS packet or NULL
