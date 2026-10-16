#include "iq16maplabels.h"

static const uint32_t coordinate[16] = {
    0x00000000u, 0x00040000u, 0x00000004u, 0x00040004u,
    0x00020001u, 0x00060001u, 0x00020005u, 0x00060005u,
    0x00010002u, 0x00050002u, 0x00010006u, 0x00050006u,
    0x00030003u, 0x00070003u, 0x00030007u, 0x00070007u,
};

void iq16maplabels_encode(uint32_t *out, const uint8_t *labels, uint32_t blocks) {
    while (blocks--) {
        *out++ = coordinate[labels[0]] | coordinate[labels[1]] << 3 |
                 coordinate[labels[2]] << 6 | coordinate[labels[3]] << 9;
        labels += 4;
    }
}
