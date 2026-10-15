#include "dvbs2_qpsk8.h"

static inline uint32_t append_packed(uint32_t *carry, uint32_t word,
                                     uint32_t fill, uint32_t mask) {
    const uint32_t rotated = (word << fill) | (word >> ((0u - fill) & 31u));
    const uint32_t spill = rotated & mask;
    const uint32_t output = (rotated & ~mask) | *carry;
    *carry = (spill << 16) | (spill >> 16);
    return output;
}

static bool valid_stream(const dvbs2_8psk_stream_t *stream) {
    return stream && stream->out && stream->fill < 16;
}

bool dvbs2_qpsk8_append(dvbs2_8psk_stream_t *stream, const uint32_t *words,
                        uint32_t nwords, uint32_t tail_word, unsigned tail_symbols) {
    if (!valid_stream(stream) || (nwords && !words) || tail_symbols > 16) return false;
    dvbs2_8psk_stream_t s = *stream;
    const uint32_t fill = s.fill, mask = ((1u << fill) - 1u) * 0x10001u;
    uint32_t carry_a = s.acc_ai | (s.acc_aq << 16), carry_b = s.acc_bi | (s.acc_bq << 16);
    for (uint32_t j = 0; j < nwords; ++j) {
        const uint32_t w = words[j], b = (~(w >> 16) & 0xffffu) | (w << 16);
        s.out[s.n++ & s.mask] = (iq8_input_t){
            append_packed(&carry_a, w, fill, mask),
            append_packed(&carry_b, b, fill, mask),
        };
    }
    s.acc_ai = carry_a & 0xffffu; s.acc_aq = carry_a >> 16;
    s.acc_bi = carry_b & 0xffffu; s.acc_bq = carry_b >> 16;
    if (tail_symbols) {
        const uint32_t valid = (1u << tail_symbols) - 1u;
        const uint32_t i = tail_word & valid, q = (tail_word >> 16) & valid;
        s.acc_ai |= i << s.fill; s.acc_aq |= q << s.fill;
        s.acc_bi |= (~q & valid) << s.fill; s.acc_bq |= i << s.fill;
        s.fill += tail_symbols;
        if (s.fill >= 16) {
            s.out[s.n++ & s.mask] = (iq8_input_t){
                (s.acc_ai & 0xffffu) | (s.acc_aq << 16),
                (s.acc_bi & 0xffffu) | (s.acc_bq << 16),
            };
            s.acc_ai >>= 16; s.acc_aq >>= 16; s.acc_bi >>= 16; s.acc_bq >>= 16;
            s.fill -= 16;
        }
    }
    *stream = s;
    return true;
}

bool dvbs2_qpsk8_frame_from(const uint32_t *frame, dvbs2_8psk_stream_t *stream,
                            uint32_t *scratch, uint32_t scratch_words) {
    if (!frame || !scratch || scratch_words < DVBS2_QPSK8_SCRATCH_WORDS || !valid_stream(stream))
        return false;
    const uint32_t symbols = dvbs2_plframe_symbols();
    if (symbols <= 90 || symbols >= 16u * DVBS2_QPSK8_SCRATCH_WORDS) return false;
    symstream_t qpsk = {.out = scratch, .mask = DVBS2_QPSK8_SCRATCH_WORDS - 1u};
    dvbs2_frame_finish_from(frame, &qpsk);
    if (qpsk.n != symbols / 16u || qpsk.fill != symbols % 16u) return false;
    return dvbs2_qpsk8_append(stream, scratch, qpsk.n,
                             qpsk.acc_i | (qpsk.acc_q << 16), qpsk.fill);
}
