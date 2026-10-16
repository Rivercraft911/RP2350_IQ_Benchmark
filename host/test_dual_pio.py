"""Model the actual four-writer PIO opcodes and descriptor progress bounds.

Run: python3 host/test_dual_pio.py

The model assumes preloaded OSRs and non-stalling input FIFOs. It checks logical
instruction slots, optional side-set, row ownership, and wrapped descriptor cursors;
it cannot establish DMA throughput, GPIO feedback latency, or external DAC timing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "firmware/src/output/dual_dma.c"
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
    code: list[int]
    stream: int                       # I-even, Q-even, I-odd, Q-odd
    pc: int
    x: int
    delay: int = 0
    samples: int = 0

    def step(self) -> tuple[int | None, int | None]:
        """Return independent data-pin and side-set writes for one SM enable slot."""
        if self.delay:
            self.delay -= 1
            return None, None
        insn = self.code[self.pc]
        # .side_set 1 opt steals bits12(enable) and11(value); delay is only bits10:8.
        flag = (insn >> 11) & 1 if insn & 0x1000 else None
        self.delay = (insn >> 8) & 7
        next_pc = (self.pc + 1) % len(self.code)
        opcode = insn >> 13
        sample = None
        if opcode == 3:               # OUT PINS,16; OUT_COUNT maps only GPIO0..13
            if ((insn >> 5) & 7) != 0 or (insn & 31) != 16:
                raise AssertionError("output must consume a complete 16-bit packed slot")
            # Each FIFO contains complete eight-sample rows, with even/odd row stride2.
            row = 2 * (self.samples // 8) + self.stream // 2
            sample = 8 * row + self.samples % 8
            self.samples += 1
        elif opcode == 0:             # JMP, including unsigned X-- underflow
            condition, target = (insn >> 5) & 7, insn & 31
            if condition == 0:
                next_pc = target
            elif condition == 2:
                old_x = self.x
                self.x = (old_x - 1) & U32
                if old_x:
                    next_pc = target
            else:
                raise AssertionError(f"unsupported JMP condition: {condition}")
        elif opcode == 7 and ((insn >> 5) & 7) == 1:  # SET X, immediate
            self.x = insn & 31
        else:
            raise AssertionError(f"unsupported instruction: 0x{insn:04x}")
        self.pc = next_pc
        return sample, flag


def progress_bounds(even: int, odd: int) -> tuple[int, int]:
    """Bounds used by the engine; checked below against explicit fetched-row sets."""
    return min(2 * even, 2 * odd + 1), max(2 * even - 1 if even else 0, 2 * odd)


class FourWriterModel(unittest.TestCase):
    def test_consecutive_slots_and_paired_row_ownership(self):
        programs = [firmware_program("four_i_code"), firmware_program("four_q_code")]
        self.assertEqual([len(code) for code in programs], [4, 4])
        machines = [Machine(programs[s & 1], s, pc, x)
                    for s, (pc, x) in enumerate(zip((0, 1, 2, 1), (7, 8, 0, 0)))]
        for tick in range(65536):
            data, flags = [], []
            for machine in machines:
                sample, flag = machine.step()
                if sample is not None:
                    data.append((machine.stream, sample))
                if flag is not None:
                    flags.append((machine.stream, flag))
            # Every slot has exactly one data writer and its matching I/Q flag writer.
            # The writer changes paired-row owner after every sixteen bus slots.
            owner = 2 * ((tick // 16) & 1) + (tick & 1)
            self.assertEqual(data, [(owner, tick // 2)], f"data at slot{tick}")
            self.assertEqual(flags, [(owner, 1 - (tick & 1))], f"flag at slot{tick}")
        self.assertEqual([m.samples for m in machines], [16384] * 4)

    def test_jmp_x_dec_branches_on_old_value_and_wraps(self):
        code = firmware_program("four_i_code")
        zero = Machine(code, 0, pc=1, x=0)
        self.assertEqual(zero.step(), (None, None))
        self.assertEqual((zero.pc, zero.x), (2, U32))
        self.assertEqual(zero.step(), (None, None))
        self.assertEqual((zero.pc, zero.x, zero.delay), (3, 7, 7))
        one = Machine(code, 0, pc=1, x=1)
        self.assertEqual(one.step(), (None, None))
        self.assertEqual((one.pc, one.x), (0, 0))

    def test_progress_bounds_against_explicit_fetched_rows(self):
        for even in range(64):
            for odd in range(64):
                fetched = set(range(0, 2 * even, 2)) | set(range(1, 2 * odd, 2))
                first_missing = next(row for row in range(129) if row not in fetched)
                exclusive_high = max(fetched, default=-1) + 1
                self.assertEqual(progress_bounds(even, odd), (first_missing, exclusive_high))
                low, high = progress_bounds(even, odd)
                for advanced in ((even + 1, odd), (even, odd + 1)):
                    new_low, new_high = progress_bounds(*advanced)
                    self.assertGreaterEqual(new_low, low)
                    self.assertGreaterEqual(new_high, high)

    def test_stride8_cursor_reconstruction_over_multiple_wraps(self):
        for count_log2 in range(5, 14):
            rows = 1 << count_log2
            steps, ring_bytes = rows // 2, rows * 4
            for aligned_base in (0x20000000, 0xffff8000):
                for odd in (0, 1):
                    base = aligned_base + 4 * odd
                    previous, total, actual = 0, 0, 0
                    # Each polling interval is strictly shorter than a complete wrap.
                    for delta in (1, steps // 3, steps - 1, 2, steps // 2) * 16:
                        actual += delta
                        address = aligned_base | ((4 * odd + 8 * actual) & (ring_bytes - 1))
                        cursor = (((address - base) & U32) >> 3) & (steps - 1)
                        total += (cursor - previous) & (steps - 1)
                        previous = cursor
                        self.assertEqual(total, actual)
                    self.assertGreater(actual, 16 * steps)
                    # One whole wrap is invisible to addresses alone: the engine's
                    # bounded elapsed-poll check remains necessary, including IRQ delays.
                    wrapped_address = aligned_base | ((4 * odd + 8 * (actual + steps)) &
                                                       (ring_bytes - 1))
                    self.assertEqual(wrapped_address, address)


if __name__ == "__main__":
    unittest.main()
