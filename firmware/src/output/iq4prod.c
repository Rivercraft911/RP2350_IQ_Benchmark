#include "iq4prod.h"

uint32_t iq4prod_seed(uint32_t previous) {
    uint32_t history = 0;
    for (unsigned age = 0; age < 6; ++age)
        history |= ((previous >> (30 - 2 * age)) & 3u) << (2 * age);
    return history;
}

unsigned iq4prod_entry(unsigned parity) {
    return parity < 2 ? parity * IQ4PROD_HISTORY_ENTRY : IQ4PROD_PROGRAM_LENGTH;
}
