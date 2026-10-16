#pragma once
#include <stdint.h>

// Complete chronological16-symbol code words, paired axes. The caller supplies
// writable owned storage, then publishes both axes together. History is external:
// FEC/ingress can replace this source with the same packed-word interface.
void iq4prod_source(uint32_t *i, uint32_t *q, unsigned words, uint32_t *seed);
