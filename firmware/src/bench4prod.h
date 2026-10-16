#pragma once
#include <stdbool.h>

// Opt-in four PIO producers and four OUT8 writers. Live uses runtime packed
// PRBS and finite input DMA grants; starve_ms stops publication deliberately.
void bench4prod8(int cpw, int ms, int invert, int priority, bool live, int starve_ms);
