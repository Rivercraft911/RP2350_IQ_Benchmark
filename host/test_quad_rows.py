"""Execute the production one-symbol four-writer opcodes with packed ring data.

This assumes non-stalling joined TX FIFOs and a preloaded OSR, as production
startup requires. It proves slot/row/flag/clock logic, not DMA handoff timing.
"""
from dataclasses import dataclass
import unittest

from test_dual_faults import FIXTURE as FAULT_FIXTURE, function_body
from test_dual_fifo import compile_run
from test_dual_pio import firmware_program
from test_dual_pio import SOURCE


def sample(axis, symbol, phase, bits):
    # An independent synthetic sample sequence exercises all output bits. This
    # schedule test does not reuse firmware history-key or waveform generation.
    return (19001 * symbol + 11731 * phase + 8273 * axis) & ((1 << bits) - 1)


@dataclass
class Writer:
    code: list[int]
    stream: int
    bits: int
    pc: int
    x: int
    pointers: int = 32
    delay: int = 0
    words: int = 0
    shifts: int = 0
    osr: int = 0

    def pull(self):
        per_word = 4 if self.bits == 8 else 2
        per_row = 4 // per_word
        ring_index = (self.words // per_row) % self.pointers
        symbol = 2 * ring_index + self.stream // 2
        first_phase = (self.words % per_row) * per_word
        self.osr = sum(sample(self.stream & 1, symbol, first_phase + p, self.bits)
                       << (p * (8 if self.bits == 8 else 16)) for p in range(per_word))
        self.shifts = 0
        self.words += 1

    def step(self):
        if self.delay:
            self.delay -= 1
            return None, None
        insn = self.code[self.pc]
        flag = (insn >> 11) & 1 if insn & 0x1000 else None
        self.delay = (insn >> 8) & 7
        next_pc = (self.pc + 1) % len(self.code)
        data, opcode = None, insn >> 13
        if opcode == 3:  # OUT PINS8/16, shift-right and autopull32
            count = insn & 31
            if ((insn >> 5) & 7) != 0 or count != (8 if self.bits == 8 else 16):
                raise AssertionError("wrong OUT target or shift count")
            if self.shifts == 32:
                self.pull()
            value = self.osr & ((1 << self.bits) - 1)
            self.osr >>= count
            self.shifts += count
            data = value << (6 if self.bits == 8 else 0)
        elif opcode == 0:
            condition, target = (insn >> 5) & 7, insn & 31
            if condition == 0:
                next_pc = target
            elif condition == 2:
                old = self.x
                self.x = (old - 1) & 0xffffffff
                if old:
                    next_pc = target
            else:
                raise AssertionError("unsupported JMP")
        elif opcode == 7 and ((insn >> 5) & 7) == 1:
            self.x = insn & 31
        else:
            raise AssertionError(f"unsupported opcode {insn:04x}")
        self.pc = next_pc
        return data, flag


class QuadRowsModel(unittest.TestCase):
    def test_native_quad_fault_polling_and_progress_rejection(self):
        source = SOURCE.read_text()
        fixture = FAULT_FIXTURE.replace("bool owned,", "bool quad_rows, owned,")
        functions = [("static void", "sample_capture", "uint32_t dma_interrupts"),
                     ("static void", "sample_faults", "void"),
                     ("dual_dma_progress_t", "dual_dma_progress", "void"),
                     ("dual_dma_faults_t", "dual_dma_faults", "void")]
        code = fixture + "\n".join(f"{kind} {name}({args}) {{\n{function_body(source, name)}\n}}"
                                    for kind, name, args in functions)
        code += r'''
static void init(void) {
    memset(&state, 0, sizeof state); memset(&dma, 0, sizeof dma);
    output.fdebug = capture.fdebug = 0; dma_hw = &dma;
    state.owned = state.running = state.quad_rows = state.result.running = true;
    state.output_pio = &output; state.output_sm_mask = 15;
    state.txstall_i_mask = 5u << 24; state.txstall_q_mask = 10u << 24;
    state.output_debug_mask = (15u << 24) | (15u << 16);
    state.output_dma_mask = 0x5555; state.capture_dma_mask = 1u << 15;
    state.capture_debug_mask = 1u | (1u << 8); state.cap_dma = 15;
    state.capture_started = true; state.capture_requested = 16; dma.ch[15].transfer_count = 16;
    fifo_level = disables = cursor_reads = 0;
}
int main(void) {
    for (uint32_t irq = 0; irq < 65536; irq++) {
        init(); dma.intr = irq;
        dual_dma_faults_t f = dual_dma_faults();
        uint32_t want = (irq & 0x5555 ? DUAL_DMA_ERR_OUTPUT_DMA : 0) |
            (irq & 0x8000 ? DUAL_DMA_ERR_CAPTURE_DMA : 0);
        assert(f.errors == want && f.running == !want && !cursor_reads);
    }
    for (uint32_t flags = 0; flags < 256; flags++) {
        init(); output.fdebug = ((flags & 15) << 24) | ((flags >> 4) << 16);
        dual_dma_faults_t f = dual_dma_faults();
        uint32_t want = (flags & 5 ? DUAL_DMA_ERR_TXSTALL_I : 0) |
            (flags & 10 ? DUAL_DMA_ERR_TXSTALL_Q : 0) |
            (flags & 240 ? DUAL_DMA_ERR_OUTPUT_DMA : 0);
        assert(f.errors == want && f.running == !want && !cursor_reads);
        assert(f.txstalls_i == (unsigned)__builtin_popcount(flags & 5));
        assert(f.txstalls_q == (unsigned)__builtin_popcount(flags & 10));
    }
    init(); dma.ch[15].transfer_count = 5;
    assert(dual_dma_faults().running && state.captured == 11 && !cursor_reads);
    capture.fdebug = 1;
    dual_dma_faults_t f = dual_dma_faults();
    assert(f.errors == DUAL_DMA_ERR_CAPTURE_STALL && !f.running && !cursor_reads);
    state.owned = false; dma_hw = NULL;
    assert(dual_dma_faults().errors == f.errors && !cursor_reads);
    init(); dual_dma_progress_t rejected = dual_dma_progress();
    assert(rejected.errors == DUAL_DMA_ERR_CONFIG && !rejected.running && !cursor_reads);
    assert(strstr(last_error, "unavailable"));
    puts("quad row faults: PASS (all raw IRQ/output flags, capture fault, released queries, no cursor reads)");
}
'''
        compile_run(code, "quad-faults", experiment=True)

    def test_packed_ring_wrap_bus_owner_and_atomic_clock_parity(self):
        starts = firmware_program("quad_row_start")
        initial_x = firmware_program("quad_row_initial_x")
        clock = firmware_program("ddr_clock_code")
        self.assertEqual(starts, [0, 1, 2, 1])
        self.assertEqual(initial_x, [3, 4, 0, 0])
        for bits in (8, 14):
            programs = [firmware_program(f"quad_row{bits}_{axis}_code") for axis in "iq"]
            self.assertEqual([len(p) for p in programs], [4, 4])
            for cpw in (1, 2, 4):
                for invert in (0, 1):
                    with self.subTest(bits=bits, cpw=cpw, invert=invert):
                        machines = [Writer(programs[s & 1], s, bits, starts[s], initial_x[s])
                                    for s in range(4)]
                        for machine in machines:
                            machine.pull()  # same explicit pre-start PULL as production
                        for tick in range(16384):
                            writes, flags = [], []
                            for machine in machines:
                                data, flag = machine.step()
                                if data is not None:
                                    writes.append((machine.stream, data))
                                if flag is not None:
                                    flags.append((machine.stream, flag))
                            axis = tick & 1
                            symbol, phase = (tick // 8) % 64, (tick // 2) % 4
                            owner = 2 * ((tick // 8) & 1) + axis
                            expected = sample(axis, symbol, phase, bits) << (6 if bits == 8 else 0)
                            self.assertEqual(writes, [(owner, expected)], f"slot{tick}")
                            self.assertEqual(flags, [(owner, 1 - axis)], f"slot{tick}")
                            # A mandatory one-bit side-set clock starts in the same
                            # enable slot; both dividers repeat this state cpw cycles.
                            ddr = (clock[(tick + invert) & 1] >> 12) & 1
                            for _ in range(cpw):
                                self.assertEqual(ddr, (1 - axis) ^ invert)
                                bus = expected | (flags[0][1] << 14)
                                self.assertEqual(bus >> 15, 0)
                                if bits == 8:
                                    self.assertEqual(bus & 63, 0)
                        words_per_row = 1 if bits == 8 else 2
                        self.assertEqual([m.words for m in machines], [1024 * words_per_row] * 4)


if __name__ == "__main__":
    unittest.main()
