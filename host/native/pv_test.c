// Host build of firmware/src/pvproto.c: prints pattern messages, validation counters and BBFRAMEs
// as JSON for host/test_pv_native.py.
#include <stdio.h>
#include <string.h>

#include "pvproto.h"

static uint8_t msgs[40][PV_MSG_BYTES];
static uint32_t n_avail, next_idx;                  // packets handed out so far / available

static const uint8_t *pkt(void) {                   // packets 7m+i of the accepted messages
    if (next_idx >= n_avail) return NULL;
    const uint32_t k = next_idx++;
    return msgs[k / 7] + 12 + PV_TS * (k % 7);
}

static void hex(const uint8_t *p, size_t n) {
    for (size_t i = 0; i < n; i++) printf("%02x", p[i]);
}

int main(void) {
    pv_init(65536);
    for (uint32_t m = 0; m < 40; m++) pv_pattern_message(msgs[m], m, 65536);
    printf("{\"pattern\":[");
    for (int m = 0; m < 3; m++) printf("%s\"", m ? "," : ""), hex(msgs[m], PV_MSG_BYTES), printf("\"");
    // validation: 30 good, then one each of bad crc / bad magic / bad sync / skipped seq
    uint8_t bad[PV_MSG_BYTES];
    int good = 0;
    for (int m = 0; m < 30; m++) good += pv_accept(msgs[m]) == 7;
    memcpy(bad, msgs[30], PV_MSG_BYTES), bad[100] ^= 1, pv_accept(bad);
    memcpy(bad, msgs[30], PV_MSG_BYTES), bad[0] = 0, pv_accept(bad);
    pv_pattern_message(bad, 30, 65536), bad[12 + 188] = 0x46;             // re-CRC'd, bad sync
    {
        const uint32_t c = pv_crc32(0, bad, PV_MSG_BYTES - 4);
        for (int b = 0; b < 4; b++) bad[PV_MSG_BYTES - 4 + b] = (uint8_t)(c >> (8 * b));
    }
    pv_accept(bad);
    pv_accept(msgs[32]);                                                  // seq 30, 31 missing
    printf("],\"good\":%d,\"ok\":%u,\"bad_crc\":%u,\"bad_hdr\":%u,\"bad_sync\":%u,\"lost\":%u",
           good, pv_stats.ok, pv_stats.bad_crc, pv_stats.bad_hdr, pv_stats.bad_sync, pv_stats.lost);
    // BBFRAMEs, normal 2/3 (Kbch 43040), roll-off 0.20: 4 frames from 30 messages (210 packets),
    // then a 5th with only 2 packets left so nulls fill it.
    pv_init(65536);
    n_avail = 210;
    static uint8_t bb[5380];
    printf(",\"bbframes\":[");
    for (int f = 0; f < 5; f++) {
        if (f == 4) n_avail = next_idx + 2;
        pv_bbframe(bb, 43040, 0xF2, pkt);
        printf("%s\"", f ? "," : ""), hex(bb, sizeof bb), printf("\"");
    }
    printf("],\"ts_packets\":%u,\"null_packets\":%u,\"ts_crc\":%u}\n", pv_stats.ts_packets,
           pv_stats.null_packets, pv_stats.ts_crc);
    return 0;
}
