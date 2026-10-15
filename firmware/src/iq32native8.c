#include "iq32native8.h"
#include "iqlanes.h"

static const int32_t coefficients[16] = {
    6658, -48202, 6455, 124501, -58274, -258197, 258053, 1108522,
    1108522, 258053, -258197, -58274, 124501, 6455, -48202, 6658,
};

const int32_t *iq32native8_coefficients(void) { return coefficients; }

bool iq32native8_build(const iq32native8_tables_t *tables, unsigned rate) {
    static const uint16_t gamma[5][2] = {{284,527},{272,487},{264,464},{254,433},{253,430}};
    if (rate >= 5 || !tables || !tables->old || !tables->middle || !tables->future) return false;
    const int32_t diagonal = 759250125, cosine15 = 1037154959, sine15 = 277904834;
    const int32_t g1 = gamma[rate][0], g2 = gamma[rate][1];
    int32_t coordinate[17] = {
        0, (int32_t)(((int64_t)diagonal * 100 + g2/2) / g2),
        (int32_t)(((int64_t)sine15 * g1 + g2/2) / g2),
        (int32_t)(((int64_t)diagonal * g1 + g2/2) / g2),
        (int32_t)(((int64_t)cosine15 * g1 + g2/2) / g2),
        410903207, diagonal, 992008094, 1073741824,
    };
    for (unsigned i = 1; i <= 8; ++i) coordinate[i+8] = -coordinate[i];
    uint32_t *const output[3] = {tables->old, tables->middle, tables->future};
    for (unsigned chunk = 0; chunk < 3; ++chunk)
        for (uint32_t key = 0; key < IQ32NATIVE8_ROWS; ++key) {
            const unsigned code[3] = {key % 17, key / 17 % 17, key / 289};
            for (unsigned symbol = 0; symbol < 2; ++symbol) {
                uint32_t packed = 0;
                for (unsigned phase = 0; phase < 2; ++phase) {
                    int32_t sum = 0;
                    for (unsigned j = 0; j < 3; ++j) {
                        const int age = 7 + (int)symbol - (int)(3*chunk + j);
                        if (age >= 0 && age < 8) {
                            const int64_t product = (int64_t)coordinate[code[j]] * coefficients[2*age + phase];
                            sum += (int32_t)((product + ((int64_t)1 << 29)) >> 30);
                        }
                    }
                    packed |= (uint32_t)(uint16_t)((sum + 128) >> 8) << (16*phase);
                }
                output[chunk][2*key + symbol] = packed;
            }
        }
    return true;
}

// Each halfword is a radix17 key0..4912. Products/sums never carry between
// lanes, allowing both I/Q keys to use one ordinary32-bit arithmetic chain.
static inline uint32_t keys(uint32_t a, uint32_t b, uint32_t c) {
    return a + 17u * (b + 17u*c);
}

void iq32native8_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq32native8_tables_t *tables) {
    if (!blocks) return;
    const uint32_t pilot = 6u | 6u << 5 | 6u << 16 | 6u << 21;
    uint32_t a = prev ? prev[0] : pilot, b = prev ? prev[1] : pilot;
    uint32_t c = prev ? prev[2] : pilot, d = prev ? prev[3] : pilot;
    const uint32_t mask = 0x001f001fu;
    for (uint32_t block = 0; block < blocks; ++block) {
        const uint32_t e = input[block];
        const uint32_t old = keys((a >> 5) & mask, b & mask, (b >> 5) & mask);
        const uint32_t mid = keys(c & mask, (c >> 5) & mask, d & mask);
        const uint32_t fresh = keys((d >> 5) & mask, e & mask, (e >> 5) & mask);
        const uint32_t *oi = tables->old + (old & 0xffffu)*2;
        const uint32_t *oq = tables->old + (old >> 16)*2;
        const uint32_t *mi = tables->middle + (mid & 0xffffu)*2;
        const uint32_t *mq = tables->middle + (mid >> 16)*2;
        const uint32_t *fi = tables->future + (fresh & 0xffffu)*2;
        const uint32_t *fq = tables->future + (fresh >> 16)*2;
        out[0] = add_lanes(add_lanes(oi[0], mi[0]), fi[0]);
        out[1] = add_lanes(add_lanes(oq[0], mq[0]), fq[0]);
        out[2] = add_lanes(add_lanes(oi[1], mi[1]), fi[1]);
        out[3] = add_lanes(add_lanes(oq[1], mq[1]), fq[1]);
        a=b; b=c; c=d; d=e;
        out += 4;
    }
}

void iq32native8_fill(uint32_t *input, uint32_t blocks, uint32_t *seed) {
    // Inner/middle rings start45/15deg; outer ring starts0deg.
    static const uint8_t axis[32][2] = {
        {1,1},{9,1},{9,9},{1,9},
        {4,2},{3,3},{2,4},{10,4},{11,3},{12,2},
        {12,10},{11,11},{10,12},{2,12},{3,11},{4,10},
        {8,0},{7,5},{6,6},{5,7},{0,8},{13,7},{14,6},{15,5},
        {16,0},{15,13},{14,14},{13,15},{0,16},{5,15},{6,14},{7,13},
    };
    uint32_t x = *seed;
    for (uint32_t block = 0; block < blocks; ++block) {
        uint32_t packed = 0;
        for (unsigned j = 0; j < 2; ++j) {
            x ^= x << 13; x ^= x >> 17; x ^= x << 5;
            const uint8_t *point = axis[x & 31u];
            packed |= (uint32_t)point[0] << (5*j) | (uint32_t)point[1] << (16+5*j);
        }
        input[block] = packed;
    }
    *seed = x;
}
