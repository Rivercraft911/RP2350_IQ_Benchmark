"""Execute N5 production writer opcodes against independent padded cyclic rows.

Models RP2350 section11.5.4.2 autopull (including its OUT retry cycle), not
the DMA arbiter. Synthetic byte samples isolate packing and scheduling from
the separately checked scalar pulse/FIR reference.
"""
from dataclasses import dataclass
import unittest

from test_dual_faults import function_body
from test_dual_fifo import CHECKS, FIXTURE, compile_run
from test_dual_pio import SOURCE, firmware_program


def sample(axis, symbol, phase):
    return (19001 * symbol + 11731 * phase + 8273 * axis) & 255


@dataclass
class Writer:
    code: list[int]
    stream: int
    pc: int
    x: int
    ring_rows: int = 32
    padding: int = 0xa5b6c7
    threshold: int = 32
    word_limit: int | None = None
    delay: int = 0
    words: int = 0
    shifts: int = 32
    osr: int = 0

    def pull(self):
        if self.word_limit is not None and self.words == self.word_limit:
            return False
        symbol = 2 * ((self.words // 2) % self.ring_rows) + self.stream // 2
        values = [sample(self.stream & 1, symbol, p) for p in range(5)]
        row = sum(value << (8 * p) for p, value in enumerate(values)) | (self.padding << 40)
        self.osr = (row >> (32 * (self.words & 1))) & 0xffffffff
        self.words += 1
        self.shifts = 0
        return True

    def autopull(self):
        if self.shifts >= self.threshold:
            self.pull()

    def step(self):
        if self.delay:
            self.autopull()  # non-OUT delay cycles can prefetch as well
            self.delay -= 1
            return None, None, False
        insn = self.code[self.pc]
        opcode, destination = insn >> 13, (insn >> 5) & 7
        flag = (insn >> 11) & 1 if insn & 0x1000 else None
        if opcode == 3 and self.shifts >= self.threshold:
            # The pull completes on this cycle, but the OUT itself retries on
            # the next cycle (RP2350 section11.5.4.2). Side-set still occurs.
            self.autopull()
            return None, flag, True
        self.delay = (insn >> 8) & 7
        next_pc = (self.pc + 1) % len(self.code)
        data = None
        if opcode == 3:
            count = (insn & 31) or 32
            if destination == 0:
                if count != 8:
                    raise AssertionError("N5 data OUT must consume one byte")
                data = (self.osr & 255) << 6
            elif destination != 3:
                raise AssertionError("unexpected OUT destination")
            self.osr >>= count
            self.shifts = min(32, self.shifts + count)
            self.autopull()
        elif opcode == 0:
            if destination == 0:
                next_pc = insn & 31
            elif destination == 2:
                old_x, self.x = self.x, (self.x - 1) & 0xffffffff
                if old_x:
                    next_pc = insn & 31
            else:
                raise AssertionError("unexpected JMP condition")
            self.autopull()
        elif opcode == 7 and destination == 1:
            self.x = insn & 31
            self.autopull()
        else:
            raise AssertionError(f"unsupported opcode {insn:04x}")
        self.pc = next_pc
        return data, flag, False


def machines(padding=0xa5b6c7, **kwargs):
    starts = firmware_program("quint_row_start")
    xs = firmware_program("quint_row_initial_x")
    programs = [firmware_program(f"quint_row8_{axis}_code") for axis in "iq"]
    result = [Writer(programs[s & 1], s, starts[s], xs[s], padding=padding, **kwargs)
              for s in range(4)]
    for writer in result:
        assert writer.pull()  # explicit pre-start PULL, as in production
    return result


def check_trace(writers, slots, invert=0, cpw=1):
    clock = firmware_program("ddr_clock_code")
    for tick in range(slots):
        writes, flags = [], []
        for writer in writers:
            data, flag, stalled = writer.step()
            if stalled:
                raise AssertionError(f"unexpected OUT stall at slot{tick}")
            if data is not None:
                writes.append((writer.stream, data))
            if flag is not None:
                flags.append((writer.stream, flag))
        axis, phase = tick & 1, (tick // 2) % 5
        symbol = (tick // 10) % (2 * writers[0].ring_rows)
        owner = 2 * ((tick // 10) & 1) + axis
        expected = sample(axis, symbol, phase) << 6
        if writes != [(owner, expected)] or flags != [(owner, 1 - axis)]:
            raise AssertionError(f"slot{tick}: writes{writes}, flags{flags}, owner{owner}, expected{expected}")
        ddr = (clock[(tick + invert) & 1] >> 12) & 1
        for _ in range(cpw):
            if ddr != ((1 - axis) ^ invert):
                raise AssertionError("DDR phase")
            bus = expected | ((1 - axis) << 14)
            if bus & 63 or bus >> 15:
                raise AssertionError("low/spare pin corruption")


NATIVE_CHECKS = r'''
static void quint_checks(void) {
    const uint32_t *desc[4];
    for (unsigned s = 0; s < 4; s++) desc[s] = (const uint32_t *)(uintptr_t)(0x20000000u + s * 32768u);
    for (unsigned log = 5; log <= 13; log++)
    for (unsigned cpw = 0; cpw <= 5; cpw++)
    for (unsigned schedule = 0; schedule < 4; schedule++) {
        reset(); selected_writers = 4; selected_row_words = 2; selected_sample_bits = 8;
        selected_schedule = schedule;
        bool want = cpw == 1 || cpw == 2;
        assert(dual_dma_start_quad5_rows(desc, log, cpw) == want);
        if (!want) { assert(stopped == 1 && !starts && !state.owned); continue; }
        assert(starts == 1 && state.owned && state.quad_n5 && state.quad_rows && state.shared_axis_row);
        assert(!state.fifo_source && !state.quad_fifo && state.sample_bits == 8 && state.row_words == 2);
        assert(state.writers == 4 && state.output_pio == pio2 && state.count == (1u << log));
        assert(state.schedule == schedule && state.sys_hz == 128000000u);
        assert(divider_arg == 256 * cpw && log_arg == log);
        for (unsigned s = 0; s < 4; s++) {
            assert(state.quad_desc[s] == desc[s]);
            axis_dma(s, s + 4, s, state.quad_desc[s], log + 2);
            dma_channel_config c = channels[s].config, d = channels[s + 4].config;
            assert(c.read_inc && !c.ctrl && !c.write_inc && c.ring_bits == log + 2 && !c.ring_write);
            assert(channels[s].read == desc[s] && channels[s].write == &dma.ch[s + 4].al3_read_addr_trig);
            assert(channels[s].count == 1 && c.dreq == DREQ_FORCE && c.chain == s && c.quiet);
            assert(channels[s + 4].count == 2 && channels[s + 4].write == &pio2->txf[s]);
            assert(d.read_inc && !d.write_inc && d.dreq == 16 + s && d.chain == s && d.quiet);
        }
    }
    for (unsigned bad = 0; bad < 12; bad++) {
        reset(); selected_writers = 4; selected_row_words = 2; selected_sample_bits = 8;
        const uint32_t *bad_desc[4]; memcpy(bad_desc, desc, sizeof bad_desc);
        if (bad == 0) selected_writers = 2;
        if (bad == 1) selected_row_words = 1;
        if (bad == 2) selected_dac_clock = 0;
        if (bad == 3) bad_desc[0] = NULL;
        if (bad == 4) bad_desc[1]++;
        if (bad == 5) bad_desc[3] = bad_desc[1];
        if (bad == 6) bad_desc[3] = NULL;
        if (bad == 7) selected_sample_bits = 14;
        assert(!dual_dma_start_quad5_rows(bad == 8 ? NULL : bad_desc,
                                          bad == 9 ? 4 : bad == 10 ? 14 : 11, bad == 11 ? 4 : 1));
        assert(stopped == 1 && !starts && !state.owned && state.result.errors == DUAL_DMA_ERR_CONFIG);
    }
    reset(); state.owned = true;
    assert(!dual_dma_start_quad5_rows(desc, 11, 1));
    assert(state.owned && !starts && !stopped);
    reset(); selected_writers = 4; selected_row_words = 2; selected_sample_bits = 8;
    reject_start = true;
    assert(!dual_dma_start_quad5_rows(desc, 11, 1));
    assert(!state.owned && starts == 1 && stopped == 1 && state.result.errors == DUAL_DMA_ERR_STARTUP);
    reject_start = false;
    reset(); selected_writers = 4; selected_row_words = 1; selected_sample_bits = 8;
    assert(dual_dma_start_quad_rows(desc, 11, 1) && !state.quad_n5);
    puts("N5 setup: PASS (native actual API, independent two-word chains, admission and N4 reset)");
}
'''


class QuintRowsModel(unittest.TestCase):
    def test_padded_rows_ring_wrap_and_atomic_clock(self):
        self.assertEqual(firmware_program("quint_row_start"), [0, 1, 4, 6])
        self.assertEqual(firmware_program("quint_row_initial_x"), [3, 4, 0, 0])
        for axis in "iq":
            self.assertEqual(len(firmware_program(f"quint_row8_{axis}_code")), 7)
        for padding in (0, 0xa5b6c7, 0xffffff):
            for cpw in (1, 2):
                for invert in (0, 1):
                    with self.subTest(padding=padding, cpw=cpw, invert=invert):
                        check_trace(machines(padding), 32 * 64 * 10, invert, cpw)

    def test_fifo_runway_and_no_startup_padding_discard(self):
        first_out = (0, 1, 10, 11)
        for limit, runway in ((8, 80), (9, 88)):
            for writer, first in zip(machines(word_limit=limit), first_out):
                outs, stall = [], None
                for tick in range(128):
                    value, _, blocked = writer.step()
                    if value is not None:
                        outs.append(tick)
                    if blocked:
                        stall = tick
                        break
                self.assertEqual(outs[:5], [first + 2 * k for k in range(5)])
                self.assertEqual(stall, first + runway)
                self.assertEqual(writer.words, limit)

    def test_negative_controls_detect_padding_threshold_and_startup_faults(self):
        controls = []
        wrong_discard = machines()
        wrong_discard[0].code = wrong_discard[0].code.copy()
        wrong_discard[0].code[3] = 0x6070  # discard16 leaves one padding byte
        controls.append(wrong_discard)
        wrong_start = machines()
        wrong_start[2].pc = 2  # fifth OUT would execute before odd row starts
        controls.append(wrong_start)
        wrong_count = machines()
        wrong_count[0].x = 4  # six outputs collide with the next symbol
        controls.append(wrong_count)
        controls.append(machines(threshold=24))
        dropped_word = machines()
        dropped_word[0].words += 1
        controls.append(dropped_word)
        for number, writers in enumerate(controls):
            with self.subTest(control=number):
                with self.assertRaises(AssertionError):
                    check_trace(writers, 640)

    def test_actual_native_dma_api_and_old_mode_regressions(self):
        source = SOURCE.read_text()
        functions = [
            ("static void", "claim_init", "void"),
            ("static void", "axis_data_dma", "int control, int data, uint sm"),
            ("static void", "axis_dma", "int control, int data, uint sm, const uint32_t *descriptors, uint ring_bits"),
            ("static void", "axis_fifo_dma", "int control, int data, uint sm, const volatile uint32_t *rx, unsigned dreq"),
            ("static void", "axis_alternating_fifo_dma", "unsigned axis"),
            ("static bool", "valid_rx_source", "const volatile uint32_t *rx, unsigned dreq"),
            ("bool", "dual_dma_start_div256", "const uint32_t *desc_i, const uint32_t *desc_q, unsigned count_log2, unsigned divider256"),
            ("bool", "dual_dma_start_fifo_div256", "const volatile uint32_t *rx_i, const volatile uint32_t *rx_q, unsigned dreq_i, unsigned dreq_q, bool control_bswap, unsigned divider256"),
            ("bool", "dual_dma_start_fifo", "const volatile uint32_t *rx_i, const volatile uint32_t *rx_q, unsigned dreq_i, unsigned dreq_q, bool control_bswap, unsigned cpw"),
            ("static void", "sample_reads", "bool check_time"),
            ("bool", "dual_dma_start_quad_rows", "const uint32_t *const desc[4], unsigned count_log2, unsigned cpw"),
            ("bool", "dual_dma_start_quad5_rows", "const uint32_t *const desc[4], unsigned count_log2, unsigned cpw"),
            ("bool", "dual_dma_start_quad_fifo", "const volatile uint32_t *const rx[4], const unsigned dreq[4], unsigned cpw"),
        ]
        fixture = FIXTURE.replace("bool shared_axis_row, quad_rows, quad_fifo;",
                                  "bool shared_axis_row, quad_rows, quad_fifo, quad_n5;")
        fixture = fixture.replace("static bool start_output(unsigned divider, unsigned log) {",
                                  "static bool reject_start;\nstatic bool start_output(unsigned divider, unsigned log) {")
        fixture = fixture.replace("starts++; divider_arg = divider; log_arg = log; return true;",
                                  "starts++; divider_arg = divider; log_arg = log; "
                                  "if (reject_start) return start_failed(\"mock start failure\", DUAL_DMA_ERR_STARTUP); "
                                  "return true;")
        code = fixture + "\n".join(f"{kind} {name}({args}) {{\n{function_body(source, name)}\n}}"
                                    for kind, name, args in functions)
        output = function_body(source, "start_output")
        controls = output[output.index("uint32_t control_mask = 0;"):output.index("const uint32_t priority =")]
        code += f"\nstatic uint32_t output_controls(unsigned count_log2) {{\n{controls}\nreturn control_mask;\n}}\n"
        checks = CHECKS.replace("int main(void) {", NATIVE_CHECKS + "\nint main(void) {")
        checks = checks.replace("quad_checks(); quad_fifo_checks();", "quad_checks(); quad_fifo_checks(); quint_checks();")
        compile_run(code + checks, "quint-rows", experiment=True)


if __name__ == "__main__":
    unittest.main()
