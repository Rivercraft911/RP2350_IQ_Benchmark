#pragma once
#include <stdbool.h>
#include <stdint.h>

enum { R2_GRANT_WORDS = 512, R2_GRANT_BATCHES = 8, R2_GRANT_RING_WORDS = 4096 };

// Immutable finite prefix: only complete, already-published batches get grants.
// The terminating zero is a null trigger, never an endless/self-trigger count.
bool r2grant_plan(uint32_t *counts, unsigned capacity, unsigned published_words);
uint32_t r2grant_source(unsigned axis, unsigned word);

// Pin-free, opt-in four-reader DMA count-list experiment. No shaping or FEC.
void bench_r2grant(int batches, int cpw, int gap_us, int fault, int priority);
