// The transmitter: core 0 shapes symbols into the DMA ring that the PIO puts on the AFE7071 bus.
// The symbols come from a PRBS, the PIO input link, or the DVB-S2 encoder on core 1 (fed with
// TS over PV-SPI in pvtx).
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "config.h"
#include "hardware/structs/m33.h"
#include "iqgen.h"
#include "pvspi.h"

#define SEED 0x1234ABCDu                         // PRBS seed for in_buf
#define BB_WORDS (64800 / 32 + 2)                // one BBFRAME, normal frames

// Owned by the transmitter; the benchmarks borrow them between runs.
extern uint32_t ring_buf[N_BLOCKS * BLOCK_WORDS];   // output DMA ring
extern uint32_t in_buf[IN_WORDS];                   // PRBS symbols; PV-SPI message slots in pvtx
extern uint32_t cap_buf[CAP_WORDS_MAX];             // pin capture; PV-SPI emulator on top
extern uint32_t bb_buf[BB_WORDS];                   // BBFRAME being encoded
extern iq_cfg_t cfg;                                // selected shaper

typedef struct {
    const char *kernel;          // shaper kernel (iqgen.c)
    int sps, L;                  // samples/symbol, filter span in symbols
    const char *cores;           // shaping cores: "0", "1" or "01" (alternate blocks)
    int cpw, ms;                 // PIO clocks/bus word; ms=0 runs until stop
    int cap_words, cap_ms;       // pin capture length and start (-1 = mid-run)
    int lanes, half;             // > 0: PRBS over the PIO input link, SCK half period
    int code, pilots;            // DVB-S2 code index on core 1, -1 = PRBS input
    int pv;                      // TS over PV-SPI: -1 off, 0 from the CM5, 1 on-chip emulator
    void (*service)(void);       // optional bounded core-0 service, at most once per ms
} tx_run_t;

#define TX_CAP_SELFTEST_MAX (CAP_WORDS_MAX - PV_EMU_WORDS)

void tx_init(void);                                 // PRBS input, core 1 task loop
// Select coefficient set and kernel. Returns NULL or an error message.
const char *tx_shaper(const char *kernel, int sps, int L, const iq_kernel_info_t **k,
                      bool *tables_ok);
uint32_t tx_code(int code, bool pilots);            // load a code and its test BBFRAME; µs taken
const char *tx_run(const tx_run_t *r);              // stream, then print the result as JSON
void tx_json_head(const char *cmd, const iq_kernel_info_t *k);

static inline uint32_t cycles(void) { return m33_hw->dwt_cyccnt; }
static inline void enable_cyccnt(void) {
    m33_hw->demcr |= M33_DEMCR_TRCENA_BITS;
    m33_hw->dwt_ctrl |= M33_DWT_CTRL_CYCCNTENA_BITS;
}

void tx_request_stop(void);                        // core-0 service: stop at next block boundary
void tx_status(void);                              // compact live, non-atomic counter snapshot
int tx_status_json(char *buffer, unsigned size);   // caller-owned bounded JSON snapshot
