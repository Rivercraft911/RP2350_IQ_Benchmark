// PV-SPI v1 slave on the RP2350 (docs/pv-spi-spec.md): PIO2 receives SPI mode 0 on GP17 (SCK),
// GP18 (MOSI), GP19 (CS_N); DMA writes each 1332-byte message into a queue slot; a CS_N rising
// edge interrupt closes the slot; READY on GP22 is high while a free slot is armed.
// Self-test: a third SM emulates the CM5 master on the same pins from a pattern buffer.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define PV_SLOTS 24                          // 24 x 1332 B = 31 968 B, about 25 ms at 10.33 Mb/s

typedef struct {
    volatile uint32_t head, tail;            // messages closed / consumed (monotonic)
    volatile uint32_t short_msgs, long_msgs, overflows;
} pvspi_stats_t;

extern pvspi_stats_t pvspi;

// slots: PV_SLOTS * PV_MSG_WORDS words. emu: 16 pattern messages when selftest (else unused).
void pvspi_start(uint32_t *slots, bool selftest, uint32_t *emu, int emu_half);
void pvspi_stop(void);
uint8_t *pvspi_next_packet(void);            // consumer (one core): next accepted TS packet or NULL
