// PV-SPI v1 message handling and TS-mode BBFRAME building (docs/pv-spi-spec.md). Portable C.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PV_MSG_BYTES 1332
#define PV_MSG_WORDS (PV_MSG_BYTES / 4)
#define PV_PAYLOAD 1316
#define PV_TS 188

typedef struct {
    uint32_t ok, nop, bad_hdr, bad_crc, bad_sync;  // messages
    uint32_t lost;                                 // messages missing by sequence number
    uint32_t ts_packets, null_packets, bbframes;
    uint32_t ts_crc;                               // CRC-32 of all accepted TS bytes, in order
    uint32_t last_seq, seq_mod;                    // seq_mod: 65536 (or 16 for the self-test)
    bool have_seq;
} pv_stats_t;

extern pv_stats_t pv_stats;

void pv_init(uint32_t seq_mod);                    // tables, counters, BBFRAME state
uint32_t pv_crc32(uint32_t crc, const void *p, size_t n);   // zlib-compatible
uint8_t pv_crc8(const uint8_t *p, size_t n);                // EN 302 307-1 5.1.4, g = 0xD5

// Validate one message; returns its TS packet count (0 for NOP) or -1, and updates pv_stats.
int pv_accept(const uint8_t *msg);

// Test pattern (identical to host/cm5/pv_spi_tx.py): message m carries packets 7m..7m+6.
void pv_pattern_message(uint8_t *msg, uint32_t m, uint32_t seq_mod);

// Source of 188-byte TS packets; NULL when none is waiting (a null packet is sent instead).
typedef const uint8_t *(*pv_packet_fn)(void);

// One TS-mode BBFRAME (Table 4): MATYPE TS/SIS/CCM + roll-off, UPL 1504, DFL = Kbch - 80,
// SYNC 0x47, SYNCD, CRC-8; each packet's sync byte carries the CRC-8 of the previous packet.
// Writes Kbch/8 bytes in transmission order.
void pv_bbframe(uint8_t *out, uint32_t kbch, uint8_t matype1, pv_packet_fn next);
