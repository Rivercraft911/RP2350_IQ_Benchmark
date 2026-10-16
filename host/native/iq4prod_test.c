// Execute the actual B6 opcodes, including FIFO stalls and shift counters.
#include "iq4prod.h"
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { FIFO_WORDS = 4, MAX_WORDS = 8192, MAGIC = 0x62367038 };

typedef struct {
    uint32_t x, y, isr, osr;
    uint32_t tx[FIFO_WORDS], rx[FIFO_WORDS];
    unsigned tx_head, tx_count, rx_head, rx_count, in_count, out_count, pc;
    unsigned offered, pulled, retired, output_count, out_stalls, in_stalls;
    uint64_t cycles, offer_after, first_push;
    bool push_pending, enabled;
    const uint32_t *source;
    unsigned source_words;
    uint32_t *output;
} machine_t;

static uint32_t reg_read(const machine_t *m, unsigned reg) {
    switch (reg) {
    case 1: return m->x;
    case 2: return m->y;
    case 3: return 0;
    case 6: return m->isr;
    default: abort(); // This program never observes the autopulled OSR.
    }
}

static void reg_write(machine_t *m, unsigned reg, uint32_t value) {
    switch (reg) {
    case 1: m->x = value; break;
    case 2: m->y = value; break;
    case 6: m->isr = value; m->in_count = 0; break;
    default: abort();
    }
}

static void pull_if_possible(machine_t *m) {
    if (m->out_count < 32 || !m->tx_count) return;
    m->osr = m->tx[m->tx_head];
    m->tx_head = (m->tx_head + 1) % FIFO_WORDS;
    --m->tx_count;
    ++m->pulled;
    m->out_count = 0;
}

static void push(machine_t *m) {
    if (!m->first_push) m->first_push = m->cycles;
    m->rx[(m->rx_head + m->rx_count) % FIFO_WORDS] = m->isr;
    ++m->rx_count;
    m->isr = m->in_count = 0;
    m->push_pending = false;
}

static void drain(machine_t *m) {
    if (!m->rx_count) return;
    m->output[m->output_count++] = m->rx[m->rx_head];
    m->rx_head = (m->rx_head + 1) % FIFO_WORDS;
    --m->rx_count;
}

// A disabled SM cannot retire an instruction or mutate its FIFO/register state.
// DMA completion, arbitration and device disable latency are outside this model.
static void cycle(machine_t *m, unsigned delays, unsigned target) {
    if (!m->enabled) return;
    ++m->cycles;
    if (m->offered < m->source_words && m->tx_count < FIFO_WORDS) {
        bool ready = !(delays & 1u);
        if ((delays & 1u) && m->out_count == 32 && !m->tx_count) {
            if (!m->offer_after) m->offer_after = m->cycles + 6 + m->offered % 13;
            ready = m->cycles >= m->offer_after;
        }
        if (ready) {
            m->tx[(m->tx_head + m->tx_count) % FIFO_WORDS] = m->source[m->offered++];
            ++m->tx_count;
            m->offer_after = 0;
        }
    }
    if ((delays & 2u) && m->cycles % 29 == 0) drain(m);
    if (m->retired < target) {
        const uint16_t insn = IQ4PROD_PROGRAM[m->pc];
        const unsigned major = insn >> 13, arg = (insn >> 5) & 7, bits = insn & 31;
        bool retired = true;
        if (m->push_pending) {
            // IN already shifted once. A retry only waits for/pushes that ISR.
            if (m->rx_count == FIFO_WORDS) { ++m->in_stalls; retired = false; }
            else push(m);
        } else if (major == 5) {
            if ((insn >> 3) & 3) abort();
            reg_write(m, arg, reg_read(m, insn & 7));
        } else if (major == 3) {
            if (m->out_count >= 32) {
                // Datasheet11.5.4.2: refill on an empty OUT still stalls for
                // that cycle; the new code is only consumed on its retry.
                pull_if_possible(m);
                ++m->out_stalls;
                retired = false;
            } else {
                reg_write(m, arg, m->osr & ((1u << bits) - 1u));
                m->osr >>= bits;
                m->out_count += bits;
                pull_if_possible(m);
            }
        } else if (major == 2) {
            m->isr = (m->isr << bits) | (reg_read(m, arg) & ((1u << bits) - 1u));
            m->in_count += bits;
            if (m->in_count >= 16) {
                if (m->rx_count == FIFO_WORDS) {
                    m->push_pending = true;
                    ++m->in_stalls;
                    retired = false;
                } else push(m);
            }
        } else abort();
        if (major != 3) pull_if_possible(m); // Opportunistic non-OUT refill.
        if (retired) {
            m->pc = (m->pc + 1) % IQ4PROD_PROGRAM_LENGTH;
            ++m->retired;
        }
    }
    if (!(delays & 2u)) drain(m);
}

static machine_t initial(const uint32_t *words, unsigned count, uint32_t previous,
                         uint32_t base, unsigned parity, uint32_t *output) {
    // The device startup PULL barrier supplies word0 before enabling the SM.
    return (machine_t){.x = iq4prod_seed(previous), .y = base >> 16, .osr = words[0],
        .pc = iq4prod_entry(parity), .enabled = true, .offered = 1, .pulled = 1,
        .source = words, .source_words = count, .output = output};
}

static bool run(machine_t *m, unsigned delays) {
    const unsigned target = m->source_words * IQ4PROD_CYCLES_PER_WORD;
    while (m->retired < target || m->rx_count) {
        if (m->cycles >= (uint64_t)target * 50 + 1000) return false;
        cycle(m, delays, target);
    }
    return m->output_count == m->source_words * 8 && m->pulled == m->source_words &&
        m->out_count == 32 && !m->push_pending;
}

static uint32_t reference(const uint32_t *words, uint32_t previous, unsigned symbol,
                          uint32_t base) {
    uint32_t key = 0;
    for (unsigned age = 0; age < 7; ++age) {
        const int n = (int)symbol - (int)age;
        const unsigned code = n < 0 ? (previous >> (2 * (16 + n))) & 3u :
            (words[(unsigned)n / 16] >> (2 * ((unsigned)n % 16))) & 3u;
        key |= code << (2 * age);
    }
    return base + 4 * key;
}

static int selftest(void) {
    unsigned cases = 0, stops = 0;
    for (unsigned history = 0; history < 4096; ++history) {
        uint32_t previous = 0;
        const uint32_t irrelevant[] = {0, 0xfffff, 0x93526};
        for (unsigned a = 0; a < 6; ++a)
            previous |= ((history >> (2 * a)) & 3u) << (30 - 2 * a);
        for (unsigned k = 0; k < sizeof irrelevant / sizeof irrelevant[0]; ++k)
            if (iq4prod_seed(previous | irrelevant[k]) != history) return 11;
    }
    for (unsigned parity = 0; parity < 2; ++parity)
        for (uint32_t base = 0x20000000; base < 0x20080000; base += 0x10000)
            for (unsigned key = 0; key < 16384; ++key) {
                uint32_t previous = 0, word = key & 3u, output[8];
                if (parity) {
                    word = ((key >> 2) & 3u) | ((key & 3u) << 2);
                    for (unsigned a = 0; a < 5; ++a)
                        previous |= ((key >> (4 + 2 * a)) & 3u) << (30 - 2 * a);
                } else {
                    for (unsigned a = 0; a < 6; ++a)
                        previous |= ((key >> (2 + 2 * a)) & 3u) << (30 - 2 * a);
                }
                machine_t m = initial(&word, 1, previous, base, parity, output);
                if (!run(&m, false) || m.cycles != IQ4PROD_CYCLES_PER_WORD ||
                    m.first_push != 7 + 4 * parity || output[0] != base + 4 * key) return 2;
                for (unsigned n = 0; n < 8; ++n)
                    if (output[n] != reference(&word, previous, 2 * n + parity, base)) return 3;
                ++cases;
            }
    if (iq4prod_entry(2) != IQ4PROD_PROGRAM_LENGTH) return 4;
    for (unsigned parity = 0; parity < 2; ++parity) {
        uint32_t words[4] = {0xc43529e7, 0x72bd91a0, 0x095ce431, 0xffffffff}, output[40];
        machine_t m = initial(words, 4, 0x753916ac, 0x20040000, parity, output);
        const unsigned target = 4 * IQ4PROD_CYCLES_PER_WORD;
        // Stop at every PC, at empty-source OUT retries and at blocked pushes.
        while (m.retired < target || m.rx_count) {
            machine_t frozen = m;
            frozen.enabled = false;
            machine_t snapshot = frozen;
            for (unsigned i = 0; i < 17; ++i) cycle(&frozen, 3, target);
            if (memcmp(&frozen, &snapshot, sizeof frozen)) return 5;
            ++stops;
            cycle(&m, 3, target);
        }
        if (!m.in_stalls || m.output_count != 32) return 6;
        for (unsigned n = 0; n < 32; ++n)
            if (output[n] != reference(words, 0x753916ac, 2 * n + parity, 0x20040000)) return 7;
        // Removing all future source words leaves both parities at their next
        // OUT fence. No stale OSR bits may become a speculative ninth row.
        for (unsigned i = 0; i < 100; ++i) cycle(&m, 0, target + IQ4PROD_PROGRAM_LENGTH);
        if (m.output_count != 32 || m.retired != target + 1 || m.out_count != 32 ||
            m.pc != iq4prod_entry(parity) + 1) return 10;
        m = initial(words, 4, 0x753916ac, 0x20040000, parity, output);
        if (!run(&m, 1) || !m.out_stalls || m.in_stalls) return 6;
        for (unsigned n = 0; n < 32; ++n)
            if (output[n] != reference(words, 0x753916ac, 2 * n + parity, 0x20040000)) return 7;
    }
    printf("{\"all_keys\":16384,\"table_bases\":8,\"prehistory_cases\":12288,\"parity_cases\":%u,"
           "\"disabled_state_cases\":%u,\"cycles_per_word\":%u,\"device_rate_measured\":false}\n",
           cases, stops, IQ4PROD_CYCLES_PER_WORD);
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "selftest")) return selftest();
    if (argc == 2 && !strcmp(argv[1], "program"))
        return fwrite(IQ4PROD_PROGRAM, sizeof IQ4PROD_PROGRAM, 1, stdout) == 1 ? 0 : 8;
    if (argc != 6 || strcmp(argv[1], "stream")) return 1;
    const uint32_t base = (uint32_t)strtoul(argv[2], NULL, 0);
    const unsigned parity = (unsigned)strtoul(argv[3], NULL, 0);
    const uint32_t previous = (uint32_t)strtoul(argv[4], NULL, 0);
    const unsigned delays = (unsigned)strtoul(argv[5], NULL, 0);
    if ((base & 65535) || base > UINT32_MAX - 65535 || parity > 1 || delays > 3) return 1;
    static uint32_t source[MAX_WORDS], output[MAX_WORDS * 8];
    const unsigned count = (unsigned)fread(source, sizeof source[0], MAX_WORDS, stdin);
    if (!count || !feof(stdin)) return 1;
    machine_t m = initial(source, count, previous, base, parity, output);
    if (!run(&m, delays)) return 9;
    const uint32_t header[] = {MAGIC, m.output_count, (uint32_t)m.cycles,
        (uint32_t)(m.cycles >> 32), m.pulled, m.out_stalls, m.in_stalls, m.retired};
    if (fwrite(header, sizeof header, 1, stdout) != 1 ||
        fwrite(output, sizeof output[0], m.output_count, stdout) != m.output_count) return 8;
    return 0;
}
