// Geometric 32APSK labels directly to rolling radix17 I/Q keys.
#pragma once
#include <stdint.h>

// Tokens0..127 are point | quarter_turn<<5. Points use the ring/angle order
// of iq32native8_fill, not DVB-S2 bit labels. Positive turns rotate +90deg.
// Tokens128..131 are unit-radius pilots with quarter_turn=token-128.
// Tokens132..255 are invalid. The hot leaf requires valid tokens.
// Coordinates/radix keys are rate-independent; amplitude tables are not.
typedef struct {
    uint32_t last; // packed I/Q coordinate codes, one per16-bit lane
    uint32_t pair; // preceding two coordinates, older +17*newer per lane
} iq32mapkeys_state_t;

// previous holds two chronological tokens; NULL means repeated unit pilot.
void iq32mapkeys_init(iq32mapkeys_state_t *state, const uint8_t previous[2]);

// One32-bit key per symbol: I bits0..15,Q bits16..31, each0..4912.
// Preserves the preceding two coordinates across arbitrary call boundaries.
// Zero symbols dereferences no pointer. Input/output must not overlap.
void iq32mapkeys_encode(uint32_t *out, const uint8_t *tokens, uint32_t symbols,
                        iq32mapkeys_state_t *state);

// Five preceding keys from seven preceding tokens. NULL means unit pilots.
void iq32mapkeys_history(uint32_t out[5], const uint8_t previous[7]);

// Packed I/Q coordinate codes for reference/integration; valid tokens only.
uint32_t iq32mapkeys_coordinate(uint8_t token);
