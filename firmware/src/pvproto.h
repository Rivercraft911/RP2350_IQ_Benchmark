// PV-SPI v1 message handling and BBFRAME adaptation (docs/pv-spi-spec.md). Portable C.
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
    uint32_t gcs_packets, gcs_bytes, gcs_underflows;
    uint32_t crc_chain;                            // CRC-32 over the CRC fields of accepted TS_DATA
    uint32_t last_seq, seq_mod;                    // seq_mod: 65536 (or 16 for the self-test)
    bool have_seq;
    int32_t first_bad_at;                          // messages seen before the first rejection (-1)
    uint32_t first_bad_info;                       // reason << 24 | offset << 8 | byte
} pv_stats_t;

extern pv_stats_t pv_stats;

void pv_init(uint32_t seq_mod);                    // tables, counters, BBFRAME state
uint32_t pv_crc32(uint32_t crc, const void *p, size_t n);   // zlib-compatible
uint8_t pv_crc8(const uint8_t *p, size_t n);                // EN 302 307-1 5.1.4, g = 0xD5

// Validate one message; returns its TS packet count (0 for NOP) or -1, and updates pv_stats.
// crc_state: 1 = CRC already verified in hardware (DMA sniffer), 0 = failed there, -1 = check here.
int pv_accept(const uint8_t *msg, int crc_state);

// Test pattern (identical to host/cm5/pv_spi_tx.py): message m carries packets 7m..7m+6.
void pv_pattern_message(uint8_t *msg, uint32_t m, uint32_t seq_mod);

// Source of 188-byte TS packets; NULL when none is waiting (a null packet is sent instead).
// The builder writes the CRC-8 into the packet's sync byte in place.
typedef uint8_t *(*pv_packet_fn)(void);

// One TS-mode BBFRAME (Table 4): MATYPE TS/SIS/CCM + roll-off, UPL 1504, DFL = Kbch - 80,
// SYNC 0x47, SYNCD, CRC-8; each packet's sync byte carries the CRC-8 of the previous packet.
// Writes Kbch/8 bytes in transmission order.
void pv_bbframe(uint8_t *out, uint32_t kbch, uint8_t matype1, pv_packet_fn next);

// Generic Continuous Stream (5.1.1/5.1.4/5.1.6): UPL/SYNCD = 0, no per-packet CRC-8.
// PV's 188-byte containers become an opaque byte stream; their bytes are not changed.
// The returned packet must remain valid until the next callback. Kbch is byte-aligned.
// A missing container fails the frame, clears its output, and requires a stream restart.
bool pv_gcs_bbframe(uint8_t *out, uint32_t kbch, uint8_t matype1, uint8_t sync, pv_packet_fn next);

// Opt-in TS adapter with fused CRC-8 and word output (dualptr/pvbench kernel 1 or 2).
// pv_bbframe_words writes exactly the words that pv_bbframe followed by zeroing bytes
// Kbch/8.. of the last word and swapping every word to MSB-first order produces: the
// encoder's input format, with no separate swap pass. Each word-aligned source word is
// read once; its CRC-8 uses four 256-byte tables (slicing by four) and its bytes are
// funnelled into the output phase. The tables, state and word leaf are caller-owned
// (no static storage). Within one stream use pv_bbframe or pv_bbframe_words for every
// frame, never both: their mid-packet CRC state differs. pv_init resets the shared
// packet cursor and previous CRC-8 for both.
typedef struct {
    uint32_t acc;              // pending output bytes, most significant first, low bits zero
    uint32_t crc;              // CRC-8 state over the current packet (byte 0 seeded, see .c)
    uint32_t phase;            // bytes pending in acc, 0..3
    const uint8_t *tables;     // T0..T3, 256 bytes each: Tk[x] = CRC-8 of x then k zero bytes
} pv_words_state_t;
// Copy n >= 1 word-aligned source words (stream order = increasing address) into n
// output words at state->phase and fold all 4n bytes into state->crc.
typedef void (*pv_words_fn)(const uint32_t *src, uint32_t *dst, uint32_t n,
                            pv_words_state_t *state);
typedef struct {
    pv_words_state_t state;
    pv_words_fn words;
    uint8_t tables[1024];
} pv_words_t;
void pv_words_c(const uint32_t *src, uint32_t *dst, uint32_t n, pv_words_state_t *state);
// Bytes of the position-independent Arm leaf (0 on other targets).
uint32_t pv_words_code_bytes(void);
// Build the tables and select the leaf. code == NULL selects the portable pv_words_c
// (flash on the device); otherwise, on Arm only, the leaf is copied into code (word
// aligned, at least pv_words_code_bytes()) and run from there. Returns false on bad
// arguments without changing ctx. Keep ctx and code fixed while a stream uses them.
bool pv_words_prepare(pv_words_t *ctx, void *code, uint32_t code_bytes);
// out: (Kbch/8 + 3) / 4 words. Same pv_stats updates and missing-packet nulls as pv_bbframe.
void pv_bbframe_words(uint32_t *out, uint32_t kbch, uint8_t matype1, pv_packet_fn next,
                      pv_words_t *ctx);
