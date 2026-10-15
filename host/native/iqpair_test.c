// Dump the actual paired-table builder for independent integer-FIR verification.
#include <stdio.h>

#include "coeffs.h"
#include "iqgen.h"
#include "iqpair.h"

enum { AXIS_WORDS = 4096 * 2 };
static struct { uint32_t before, words[AXIS_WORDS], after; } source;
static struct { uint32_t before, words[IQPAIR_WORDS], after; } target;
static uint32_t ti[AXIS_WORDS];

int main(void) {
    source.before = target.before = 0x13579bdfu;
    source.after = target.after = 0x2468ace0u;
    const iq_cfg_t cfg = {4, 12, c_4_12, ti, source.words};
    iq_build_tables(&cfg);
    const uint32_t original_crc = crc32_update(0, &source, sizeof source);
    iqpair_build(target.words, source.words);
    if (target.before != 0x13579bdfu || target.after != 0x2468ace0u ||
        crc32_update(0, &source, sizeof source) != original_crc) return 2;
    return fwrite(target.words, sizeof target.words[0], IQPAIR_WORDS, stdout) == IQPAIR_WORDS ? 0 : 3;
}
