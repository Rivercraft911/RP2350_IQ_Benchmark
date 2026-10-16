"""Model the GPIO28 DDR clock SM against every dual-DMA writer layout.

Run: python3 host/test_dacclk.py

Parses the actual opcode arrays and start state from firmware/src/dual_dma.c and steps
all state machines one enabled slot at a time from a common first slot, which is what one
PIO1 CTRL write with NEXTPREV_SM_ENABLE + CLKDIV_RESTART provides. Checks that the clock is
high in exactly the I slots, so DDR rising edges launch with I and falling edges with Q.
It assumes preloaded OSRs, non-stalling FIFOs and equal output latency for PIO0 and PIO2;
the device capture (dual_trial.py --dac-clock ddr) tests that last assumption to one
SYS cycle. It does not establish pad skew, SN65 delay or AFE setup/hold.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "firmware/src/dual_dma.c"
U32 = 0xffffffff


def firmware_program(name: str) -> list[int]:
    source = SOURCE.read_text()
    match = re.search(r"\b" + re.escape(name) + r"\[\]\s*=\s*\{([^}]+)\}", source)
    if not match:
        raise AssertionError(f"missing firmware instruction array: {name}")
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", match.group(1), flags=re.S)
    return [int(value.strip(), 0) for value in body.split(",") if value.strip()]


@dataclass
class Machine:
    """One SM. side_bits counts the enable bit when optional (SDK sm_config_set_sideset)."""
    code: list[int]
    pc: int
    x: int = 0
    side_bits: int = 0
    optional: bool = False
    delay: int = 0
    outs: int = 0
    side: int | None = None

    def step(self) -> tuple[bool, int | None]:
        """Return (OUT executed, side-set value written) for one enabled slot."""
        if self.delay:
            self.delay -= 1
            return False, None
        insn = self.code[self.pc]
        field = (insn >> 8) & 31
        delay_bits = 5 - self.side_bits
        self.delay = field & ((1 << delay_bits) - 1)
        side_field = field >> delay_bits
        side = None
        if self.side_bits:
            if self.optional:
                if side_field >> (self.side_bits - 1):
                    side = side_field & ((1 << (self.side_bits - 1)) - 1)
            else:
                side = side_field
        if side is not None:
            self.side = side
        next_pc = (self.pc + 1) % len(self.code)
        opcode, out = insn >> 13, False
        if opcode == 3:                                    # OUT PINS,16
            if ((insn >> 5) & 7) != 0 or (insn & 31) != 16:
                raise AssertionError("writers must OUT one complete 16-bit slot")
            out = True
            self.outs += 1
        elif opcode == 0:                                  # JMP always / X--
            condition, target = (insn >> 5) & 7, insn & 31
            if condition == 0:
                next_pc = target
            elif condition == 2:
                old_x, self.x = self.x, (self.x - 1) & U32
                if old_x:
                    next_pc = target
            else:
                raise AssertionError(f"unsupported JMP condition {condition}")
        elif opcode == 7 and ((insn >> 5) & 7) == 1:       # SET X
            self.x = insn & 31
        elif opcode == 5 and (insn & 0xff) == 0x42:        # MOV Y,Y (NOP)
            pass
        else:
            raise AssertionError(f"unsupported instruction 0x{insn:04x}")
        self.pc = next_pc
        return out, side


def writers(layout: str) -> list[Machine]:
    """Start PCs/X exactly as start_output() programs them; streams I0, Q0, I1, Q1."""
    if layout == "two-row2":
        code = firmware_program("out_code")
        return [Machine(code, 0), Machine(code, 1)]
    if layout == "two-paired":
        return [Machine(firmware_program("paired_i_code"), 0, side_bits=2, optional=True),
                Machine(firmware_program("paired_q_code"), 1, side_bits=2, optional=True)]
    prefix, samples = {"four-row4": ("four", 8), "four-row8": ("quad_four", 16)}[layout]
    i_code, q_code = firmware_program(f"{prefix}_i_code"), firmware_program(f"{prefix}_q_code")
    starts, xs = (0, 1, 2, 1), (samples - 1, samples, 0, 0)
    return [Machine(i_code if s % 2 == 0 else q_code, starts[s], xs[s], 2, True) for s in range(4)]


def run(layout: str, clock_pc: int, slots: int) -> list[tuple[int, int]]:
    """(writer stream, clock level) for every slot; asserts exactly one OUT per slot."""
    ws = writers(layout)
    clock = Machine(firmware_program("ddr_clock_code"), clock_pc, side_bits=1)
    trace = []
    for slot in range(slots):
        owners = [s for s, w in enumerate(ws) if w.step()[0]]
        _, level = clock.step()
        if len(owners) != 1 or level is None:
            raise AssertionError(f"{layout} slot {slot}: writers {owners}, clock {level}")
        trace.append((owners[0], level))
    return trace


LAYOUTS = ("two-row2", "two-paired", "four-row4", "four-row8")


class DdrClockModel(unittest.TestCase):
    def test_firmware_start_state_matches_model(self):
        source = SOURCE.read_text()
        self.assertRegex(source, r"uint start = offset \+ \(s == 2 \? 2u : s & 1u\);\s*"
                         r"#ifdef IQ4HIST_EXPERIMENT\s*"
                         r"if \(state.quad_rows\) start = offset \+ \(state.quad_n5 \? quint_row_start\[s\] : quad_row_start\[s\]\);\s*"
                         r"#endif\s*pio_sm_init\(state.output_pio, state.sm\[s\], start, &pc\);")
        self.assertIn("const unsigned initial_x[] = {samples - 1, samples, 0, 0};", source)
        self.assertRegex(source, r"unsigned x = initial_x\[s\];\s*"
                         r"#ifdef IQ4HIST_EXPERIMENT\s*"
                         r"if \(state.quad_rows\) x = state.quad_n5 \? quint_row_initial_x\[s\] : quad_row_initial_x\[s\];\s*"
                         r"#endif\s*pio_sm_exec_wait_blocking\(state.output_pio, state.sm\[s\], "
                         r"pio_encode_set\(pio_x, x\)\);")
        self.assertEqual(firmware_program("quad_row_start"), [0, 1, 2, 1])
        self.assertEqual(firmware_program("quad_row_initial_x"), [3, 4, 0, 0])
        self.assertIn("sm_config_set_sideset(&cc, 1, false, false);", source)
        self.assertIn("state.clock_offset + (state.dac_clock == DUAL_DMA_DAC_CLOCK_DDR_PIO_INVERTED)",
                      source)
        self.assertEqual(firmware_program("ddr_clock_code"), [0xb042, 0xa042])

    def test_clock_high_exactly_in_i_slots(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                trace = run(layout, 0, 4096)
                for slot, (stream, level) in enumerate(trace):
                    self.assertEqual(stream % 2, slot % 2, f"I/Q slot order at {slot}")
                    self.assertEqual(level, 1 - stream % 2, f"clock level at slot {slot}")
                # A rising edge launches every I word: DACCLK = bus word rate / 2.
                rising = [s for s in range(1, len(trace)) if trace[s][1] and not trace[s - 1][1]]
                self.assertEqual(rising, list(range(2, len(trace), 2)))

    def test_inverted_control_is_high_in_q_slots(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                trace = run(layout, 1, 1024)
                self.assertTrue(all(level == stream % 2 for stream, level in trace))

    def test_one_slot_skew_is_visible_at_both_dividers(self):
        """Capture model: bus word per slot, divider SYS cycles per slot, stride 17 cycles."""
        trace = run("four-row8", 0, 4096)
        for divider in (1, 2):
            flags = [1 - stream % 2 for stream, _ in trace for _ in range(divider)]
            clock = [level for _, level in trace for _ in range(divider)]
            for skew in (0, 1):
                late = [0] * skew + clock[:len(clock) - skew]
                sampled = range(skew, len(flags), 17)
                bad = sum(late[c] != flags[c] for c in sampled)
                self.assertEqual(bad == 0, skew == 0, f"divider {divider}, skew {skew}")
                self.assertTrue(skew == 0 or bad >= len(sampled) // (2 * divider) - 1)


if __name__ == "__main__":
    unittest.main()
