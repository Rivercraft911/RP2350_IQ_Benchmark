// Two-symbol rolling-key rows: trades16KiB RAM for fewer mapper operations.
#pragma once
#include "iq32mapkeys.h"

enum { IQ32MAPKEYS_PAIR_ROWS = 1024, IQ32MAPKEYS_PAIR_WORDS = 4096 };

// Caller owns16KiB of32-bit-aligned storage. No allocation or global state.
void iq32mapkeys_pair_build(uint32_t table[IQ32MAPKEYS_PAIR_WORDS]);

// Same tokens/history/state/output contract as iq32mapkeys_encode.
void iq32mapkeys_pair_encode(uint32_t *out, const uint8_t *tokens,
                             uint32_t symbols, iq32mapkeys_state_t *state,
                             const uint32_t *table);

// Faster leaf for canonical geometric labels0..31; rotations/pilots must be
// canonicalized by the producer. Unit pilots map to18,22,26,30. Arbitrary
// odd-sized chunks remain supported, with the same persistent state.
void iq32mapkeys_pair_canonical(uint32_t *out, const uint8_t *points,
                                uint32_t symbols, iq32mapkeys_state_t *state,
                                const uint32_t *table);
uint8_t iq32mapkeys_canonical(uint8_t token);
