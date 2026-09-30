#include "pvproto.h"

#include <string.h>

#ifdef IQ_ON_DEVICE
#define PV_HI __attribute__((section(".sram_hi")))
#define HOT(f) __attribute__((section(".time_critical." #f))) f     // run from SRAM, not XIP
#else
#define PV_HI
#define HOT(f) f
#endif

pv_stats_t pv_stats;
static uint32_t crc_t[4][256] PV_HI;                // slicing-by-4, reflected 0xEDB88320
static uint8_t crc8_t[256];
static uint8_t *cur;                               // packet being emitted (sync byte replaced)
static uint32_t cur_off = PV_TS;                    // bytes of cur already emitted
static uint8_t prev_crc8, null_crc8;                // CRC-8 of the previous packet / of a null
static uint8_t null_pkt[PV_TS];                     // PID 0x1FFF, payload only, 0xFF stuffing

void pv_init(uint32_t seq_mod) {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++) c = c & 1 ? (c >> 1) ^ 0xEDB88320u : c >> 1;
        crc_t[0][i] = c;
        uint8_t d = (uint8_t)i;
        for (int k = 0; k < 8; k++) d = (uint8_t)(d & 0x80 ? (d << 1) ^ 0xD5 : d << 1);
        crc8_t[i] = d;
    }
    for (uint32_t i = 0; i < 256; i++)
        for (int t = 1; t < 4; t++) crc_t[t][i] = (crc_t[t - 1][i] >> 8) ^ crc_t[0][crc_t[t - 1][i] & 0xFF];
    pv_stats = (pv_stats_t){.seq_mod = seq_mod, .first_bad_at = -1};
    cur_off = PV_TS, prev_crc8 = 0;
    memset(null_pkt, 0xFF, PV_TS);
    null_pkt[0] = 0x47, null_pkt[1] = 0x1F, null_pkt[2] = 0xFF, null_pkt[3] = 0x10;
    null_crc8 = pv_crc8(null_pkt + 1, PV_TS - 1);
}

uint32_t HOT(pv_crc32)(uint32_t crc, const void *p, size_t n) {
    const uint8_t *b = p;
    crc = ~crc;
    for (; n >= 4; n -= 4, b += 4) {
        crc ^= (uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24;
        crc = crc_t[3][crc & 0xFF] ^ crc_t[2][(crc >> 8) & 0xFF] ^ crc_t[1][(crc >> 16) & 0xFF] ^
              crc_t[0][crc >> 24];
    }
    while (n--) crc = (crc >> 8) ^ crc_t[0][(crc ^ *b++) & 0xFF];
    return ~crc;
}

uint8_t HOT(pv_crc8)(const uint8_t *p, size_t n) {
    uint8_t c = 0;
    for (; n >= 4; n -= 4, p += 4) {
        c = crc8_t[c ^ p[0]], c = crc8_t[c ^ p[1]];
        c = crc8_t[c ^ p[2]], c = crc8_t[c ^ p[3]];
    }
    while (n--) c = crc8_t[c ^ *p++];
    return c;
}

static inline uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static inline uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

static void HOT(note_bad)(uint32_t reason, uint32_t off, uint32_t byte) {
    if (pv_stats.first_bad_at < 0) {
        pv_stats.first_bad_at = (int32_t)(pv_stats.ok + pv_stats.nop + pv_stats.bad_hdr + pv_stats.bad_crc +
                                          pv_stats.bad_sync);
        pv_stats.first_bad_info = reason << 24 | (off & 0xFFFF) << 8 | (byte & 0xFF);
    }
}

int HOT(pv_accept)(const uint8_t *m, int crc_state) {
    const uint32_t type = m[3], len = le16(m + 6);
    if (le16(m) != 0x5650 || m[2] != 1 || type > 1 || (type == 1 && (len == 0 || len > PV_PAYLOAD ||
                                                                     len % PV_TS)) || (type == 0 && len)) {
        note_bad(1, 0, m[0]);
        pv_stats.bad_hdr++;
        return -1;
    }
    if (crc_state == 0 || (crc_state < 0 && pv_crc32(0, m, PV_MSG_BYTES - 4) != le32(m + PV_MSG_BYTES - 4))) {
        note_bad(2, 0, 0);
        pv_stats.bad_crc++;
        return -1;
    }
    const uint32_t n = len / PV_TS;
    for (uint32_t i = 0; i < n; i++)
        if (m[12 + PV_TS * i] != 0x47) {
            note_bad(3, 12 + PV_TS * i, m[12 + PV_TS * i]);
            pv_stats.bad_sync++;
            return -1;
        }
    const uint32_t seq = le16(m + 4);
    if (pv_stats.have_seq)
        pv_stats.lost += (seq + pv_stats.seq_mod - pv_stats.last_seq - 1u) % pv_stats.seq_mod;
    pv_stats.last_seq = seq, pv_stats.have_seq = true;
    if (type == 0) {
        pv_stats.nop++;
    } else {
        pv_stats.ok++;
        pv_stats.crc_chain = pv_crc32(pv_stats.crc_chain, m + PV_MSG_BYTES - 4, 4);
    }
    return (int)n;
}

void pv_pattern_message(uint8_t *m, uint32_t idx, uint32_t seq_mod) {
    const uint16_t seq = (uint16_t)(idx % seq_mod), len = PV_PAYLOAD;
    const uint8_t hdr[12] = {0x50, 0x56, 1, 1, (uint8_t)seq, (uint8_t)(seq >> 8), (uint8_t)len,
                             (uint8_t)(len >> 8), 0, 0, 0, 0};
    memcpy(m, hdr, 12);
    for (uint32_t i = 0; i < 7; i++) {
        const uint32_t k = 7 * idx + i;
        uint8_t *p = m + 12 + PV_TS * i;
        p[0] = 0x47, p[1] = 0x01, p[2] = 0x00, p[3] = (uint8_t)(0x10 | (k & 15));
        for (uint32_t j = 0; j < 184; j++) p[4 + j] = (uint8_t)((k % 112 + j) & 0xFF);
    }
    const uint32_t c = pv_crc32(0, m, PV_MSG_BYTES - 4);
    for (int b = 0; b < 4; b++) m[PV_MSG_BYTES - 4 + b] = (uint8_t)(c >> (8 * b));
}

// Next packet: its sync byte becomes the previous packet's CRC-8 (5.1.4), written in place.
static void HOT(next_packet)(pv_packet_fn next) {
    uint8_t *p = next(), c;
    if (p) {
        c = pv_crc8(p + 1, PV_TS - 1);
        pv_stats.ts_packets++;
    } else {
        p = null_pkt, c = null_crc8;
        pv_stats.null_packets++;
    }
    p[0] = prev_crc8;
    prev_crc8 = c, cur = p, cur_off = 0;
}

static inline void HOT(copy)(uint8_t *d, const uint8_t *s, uint32_t n) {   // unaligned word moves
    for (; n >= 4; n -= 4, d += 4, s += 4) {
        uint32_t w;
        __builtin_memcpy(&w, s, 4);
        __builtin_memcpy(d, &w, 4);
    }
    while (n--) *d++ = *s++;
}

void HOT(pv_bbframe)(uint8_t *out, uint32_t kbch, uint8_t matype1, pv_packet_fn next) {
    const uint32_t dfl = kbch - 80, syncd = cur_off == PV_TS ? 0 : (PV_TS - cur_off) * 8u;
    uint8_t *h = out;
    h[0] = matype1, h[1] = 0, h[2] = (1504 >> 8), h[3] = 1504 & 0xFF;
    h[4] = (uint8_t)(dfl >> 8), h[5] = (uint8_t)dfl, h[6] = 0x47;
    h[7] = (uint8_t)(syncd >> 8), h[8] = (uint8_t)syncd, h[9] = pv_crc8(h, 9);
    uint8_t *d = out + 10;
    for (uint32_t left = dfl / 8; left;) {
        if (cur_off == PV_TS) next_packet(next);
        uint32_t n = PV_TS - cur_off;
        if (n > left) n = left;
        copy(d, cur + cur_off, n);
        d += n, cur_off += n, left -= n;
    }
    pv_stats.bbframes++;
}
