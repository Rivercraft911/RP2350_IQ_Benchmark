// Pin-free finite L8 pointer test; borrow tx buffers only while the transmitter is idle.
#pragma once

// mode0 captures raw byte-reversed PIO pointers; mode1 uses capture-DMA BSWAP.
// Emits one JSON result and 2048 I then2048 Q pointer words with @cap/@capend.
// This tests addresses and credited-source termination, not live output timing.
void bench_quadptr(int mode);
