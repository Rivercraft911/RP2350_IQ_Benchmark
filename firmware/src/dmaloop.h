#pragma once

// Time the control -> sample -> chain DMA loop of the quad-row engine without writers:
// chains 1..4, row_words 1..2, sink 0 = SRAM word / 1 = draining PIO2 FIFO / 2 = PIO2 FIFO with
// pointers from PIO1 RX FIFOs (the B2/B4 graph), background load
// at SYS/load_div (0 = off) to a PIO1 FIFO (kind 0) or SRAM (kind 1), BUS_PRIORITY mask,
// and an optional core-1 SRAM copy hammer. Prints one JSON line.
void dmaloop(int chains, int row_words, int sink, int load_div, int load_kind, int prio, int hammer);
