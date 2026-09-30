// Board, pin and buffer configuration. Pin facts: docs/board-pico-plus-2.md.
#pragma once

// 128 MHz = 4 x 32 MHz: integer PIO cycles per bus word at 8 Msym/s for sps 2 (4 cyc) and
// sps 4 (2 cyc). 12 MHz XOSC -> VCO 1536 MHz / 6 / 2. Stock limit is 150 MHz.
#define DEFAULT_SYS_KHZ 128000

// AFE7071 data bus: out pins GPIO0-15 = D0..D13, IQ_FLAG, spare (header pins 1-20).
// CLK_IO on GPIO16 (side-set). GP0/1 are the default UART: stdio uses USB only.
#define PIN_D0 0
#define PIN_CLKIO 16

// Input link (stage 4): clock, 4 data lanes, ready. Reserved, GPIO17-22.
#define PIN_IN_CLK 17
#define PIN_IN_D0 18
#define PIN_IN_READY 22

#define BLOCK_WORDS 4096                      // output words per ring block (16 KiB)
#define BLOCK_SYMS(sps) (BLOCK_WORDS / (sps)) // 2048, 1024, 512 symbols for sps 2, 4, 8
#define BLOCK_IN(sps) (BLOCK_SYMS(sps) / 16)  // input words per block
#define N_BLOCKS 8                            // output ring depth
#define IN_WORDS 8192                         // input ring: 131072 symbols (32 KiB)
#define CAP_WORDS_MAX 14336                   // capture buffer, 56 KiB (pvtx self-test uses the top 21 KiB)
#define MAX_L 12
#define TABLE_WORDS 8192                      // max 2^L * sps / 2 over COEF_SETS (32 KiB)

// Placement (see CMakeLists.txt): SRAM4-7 for data the CPU reads randomly, away from DMA traffic.
#define SRAM_HI __attribute__((section(".sram_hi")))
