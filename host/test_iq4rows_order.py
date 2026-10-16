"""Execute B3's L7 pointer and alternating two-word writer opcodes; no USB.

The real portable C table is compared against a direct frozen-tap FIR. The event
schedule covers FIFO/backpressure, row ownership, credits and stop, but it does
not model RP2350 arbitration or measure sustained device timing.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np

from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS
import test_iq4ptr as pointer_reference
from test_iq4hist import scalar_waveform
from test_quad_fifo_order import (AxisDMA, MASK, Word, Writer, instructions,
                                  router_targets)

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x20000000


@lru_cache(maxsize=1)
def native_table():
    """Actual firmware C builder; independent scalar FIR supplies expected data."""
    with tempfile.TemporaryDirectory(prefix='iq4rows-native-') as temporary:
        executable = Path(temporary) / 'iq4hist_test'
        source = ROOT / 'firmware/src'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        f'-I{source}', str(source / 'iq4hist.c'), str(source / 'iq8.c'),
                        str(ROOT / 'host/native/iq4hist_test.c'), '-o', str(executable)], check=True)
        result = np.frombuffer(subprocess.check_output([str(executable)]), dtype='<u4')
    return result.reshape(16384, 2).tolist()


@dataclass
class Pointer:
    previous: int
    pc: int = 0
    shifted: int = 32
    issued: int = 0
    pushed: int = 0
    rx_stalls: int = 0
    tx: deque = field(default_factory=deque)
    rx: deque = field(default_factory=deque)

    def __post_init__(self):
        self.code = instructions('iq4ptr.h', 'IQ4PTR_PROGRAM')
        self.regs = dict(x=pointer_reference.seed(self.previous), y=BASE >> 17, isr=0, osr=0)

    def step(self):
        instruction = self.code[self.pc]
        nxt = (self.pc + 1) % len(self.code)
        major, arg, count = instruction >> 13, (instruction >> 5) & 7, instruction & 31
        sources = {1: 'x', 2: 'y', 3: None, 6: 'isr', 7: 'osr'}
        if major == 0:
            assert arg == 7
            if self.shifted < 32:
                nxt = count
        elif major == 4:
            if instruction & 0x80:
                if not self.tx:
                    return
                self.regs['osr'], self.shifted = self.tx.popleft(), 0
            else:
                if len(self.rx) == 4:
                    self.rx_stalls += 1
                    return
                self.rx.append((self.pushed, self.regs['isr']))
                self.pushed += 1
                self.regs['isr'] = 0
        elif major == 5:
            assert (instruction >> 3) & 3 == 0
            self.regs[sources[arg]] = self.regs[sources[instruction & 7]]
        elif major == 2:
            count = count or 32
            value = self.regs[sources[arg]] if sources[arg] else 0
            self.regs['isr'] = ((self.regs['isr'] << count) |
                                (value & ((1 << count) - 1))) & MASK
        elif major == 3:
            assert arg == 3
            self.regs['osr'] >>= count or 32
            self.shifted = min(32, self.shifted + (count or 32))
        else:
            raise AssertionError(f'unsupported pointer opcode 0x{instruction:04x}')
        self.pc = nxt


class Model:
    def __init__(self, words=512, seed=0x2350, delay=0, skew=False, grant_limit=None,
                 divider=2, wrong_route=False, invert=False, completion_delay=None, writers=4):
        rng = np.random.default_rng(seed)
        self.input = rng.integers(0, 2**32, size=(2, words), dtype=np.uint32)
        self.pointers = [Pointer(0), Pointer(0xaaaaaaaa)]
        prefix = 'row_four' if writers == 4 else 'paired'
        codes = [instructions('dual_dma.c', prefix + '_i_code'),
                 instructions('dual_dma.c', prefix + '_q_code')]
        self.writers = [Writer(codes[stream & 1], stream, pc, x)
                        for stream, (pc, x) in enumerate(zip((0, 1, 2, 1), (3, 4, 0, 0)))
                        if stream < writers]
        self.writer_count = writers
        self.axes = [AxisDMA(), AxisDMA()]
        self.router = tuple(reversed(router_targets())) if wrong_route else router_targets()
        self.delay, self.skew = delay, skew
        self.divider, self.invert = divider, invert
        self.completion_delay = completion_delay
        self.time = self.bus_tick = self.next_axis = self.published = self.granted = 0
        self.enabled_start = 0
        self.ring = [None] * 4096
        self.grant_limit = words if grant_limit is None else grant_limit
        self.enabled = self.stopped = False
        self.stalls, self.clock_mismatches = [], 0
        self.max_source_index = 0
        self.table = native_table()
        self.expected_addresses = [pointer_reference.reference(
            list(map(int, axis)), machine.previous, BASE)
            for axis, machine in zip(self.input, self.pointers)]
        shifts = 2 * np.arange(16, dtype=np.uint32)
        codes = ((self.input[:, :, None] >> shifts) & 3).reshape(2, -1)
        packed = scalar_waveform(codes).reshape(-1, 2)
        self.expected = [np.stack((packed[:, axis] & 0x3fff,
                                  packed[:, axis] >> 16), axis=-1).ravel()
                         for axis in range(2)]
        # The FIR uses the frozen independently recorded tap/level constants.
        assert len(L7_COEFFICIENTS) == 28 and len(FOUR_LEVELS_Q30) == 4

    def handoff(self, axis, stage, row):
        return 1 + ((row * 13 + axis * 7 + len(stage)) % (self.delay + 1))

    def source(self):
        reusable = max(0, min(machine.issued for machine in self.pointers) - 8)
        end = min(self.input.shape[1], reusable + len(self.ring))
        while self.published < end:
            index = self.published
            self.ring[index & 4095] = index, self.input[:, index]
            self.published += 1
        self.granted = min(self.published // 512 * 512, self.grant_limit)
        for axis, machine in enumerate(self.pointers):
            if len(machine.tx) == 4 or machine.issued >= self.granted:
                continue
            if self.skew and (self.time + 3 * axis) % (13 + 4 * axis) < 3:
                continue
            index, values = self.ring[machine.issued & 4095]
            assert index == machine.issued, 'input overwritten before both readers retired it'
            machine.tx.append(int(values[axis]))
            machine.issued += 1
            self.max_source_index = max(self.max_source_index, machine.issued)
        assert self.max_source_index <= self.granted <= self.published

    def transfer(self, axis):
        dma, pointer = self.axes[axis], self.pointers[axis]
        if self.time < dma.ready:
            return False
        if self.skew and (self.time + 5 * axis) % (97 + 4 * axis) < 2:
            return False
        completed = False
        if dma.stage == 'control':
            if not pointer.rx:
                return False
            row, address = pointer.rx.popleft()
            assert address == self.expected_addresses[axis][row], 'incorrect numerical LUT address'
            assert dma.selected == (row % 2 if self.writer_count == 4 else 0), \
                'router assigned a row to the wrong writer'
            dma.row, dma.address, dma.word = row, address, 0
            dma.controls += 1
            dma.stage = 'data'
        elif dma.stage == 'data':
            writer = self.writers[axis + 2 * dma.selected]
            if len(writer.fifo) == 8:
                return False
            key = (dma.address - BASE) // 8
            assert 0 <= key < 16384 and not (dma.address & 7)
            writer.fifo.append(Word(dma.row, dma.word, self.table[key][dma.word]))
            dma.word += 1
            dma.transfers += 1
            if dma.word == 2:
                dma.stage = 'router' if self.writer_count == 4 else 'control'
                completed = True
        elif dma.stage == 'router':
            dma.selected = self.router[dma.router_cursor & 1]
            dma.router_cursor += 1
            dma.stage = 'control'
        else:
            raise AssertionError(f'invalid DMA stage {dma.stage}')
        latency = (self.completion_delay(axis, dma.row) if completed and self.completion_delay
                   else self.handoff(axis, dma.stage, dma.row))
        dma.ready = self.time + latency
        return True

    def step(self):
        if self.stopped:
            self.time += 1
            return
        self.source()
        for machine in self.pointers:
            machine.step()
        # One transfer/slot is a scheduling model, not the shared FASTPERI arbiter.
        for axis in (self.next_axis, 1 - self.next_axis):
            if self.transfer(axis):
                self.next_axis = 1 - axis
                break
        if self.enabled and (self.time - self.enabled_start) % self.divider == 0:
            data, flags = [], []
            for writer in self.writers:
                sample, flag, stalled = writer.step()
                if sample is not None:
                    data.append((writer.stream, sample))
                if flag is not None:
                    flags.append((writer.stream, flag))
                if stalled:
                    self.stalls.append((self.bus_tick, writer.stream))
            if not self.stalls:
                axis, row = self.bus_tick & 1, self.bus_tick // 8
                owner = axis + (2 * (row & 1) if self.writer_count == 4 else 0)
                within = (self.bus_tick // 2) & 3
                sample = int(self.expected[axis][self.bus_tick // 2])
                assert data == [(owner, (row, within, sample))], f'data order at slot {self.bus_tick}'
                assert flags == [(owner, 1 - axis)], f'IQ_FLAG order at slot {self.bus_tick}'
                clock_code = instructions_cached_clock[(self.bus_tick + self.invert) & 1]
                self.clock_mismatches += ((clock_code >> 12) & 1) != 1 - axis
            self.bus_tick += 1
        self.time += 1

    def start(self):
        for _ in range(10000):
            if all(len(writer.fifo) == 8 for writer in self.writers):
                break
            self.step()
        else:
            raise AssertionError('alternating output FIFOs failed to prefill')
        for writer in self.writers:
            writer.pull()
        self.enabled_start, self.enabled = self.time, True

    def run(self, slots):
        target = self.bus_tick + slots
        while self.bus_tick < target and not self.stalls:
            self.step()

    def stop(self):
        self.enabled, self.stopped = False, True


instructions_cached_clock = instructions('dual_dma.c', 'ddr_clock_code')


class FourLevelRowsOrdering(unittest.TestCase):
    def test_actual_opcodes_and_eight_slot_silent_interval(self):
        self.assertEqual(instructions('iq4ptr.h', 'IQ4PTR_PROGRAM'),
                         [0x00e2, 0x80a0, 0xa0c1, 0x40e2, 0x6062,
                          0xa026, 0xa0c2, 0x402e, 0x4063, 0x8020])
        self.assertEqual(instructions('dual_dma.c', 'row_four_i_code'),
                         [0x7810, 0x0040, 0xe323, 0x0300])
        self.assertEqual(instructions('dual_dma.c', 'row_four_q_code'),
                         [0x7010, 0x0040, 0xe323, 0x0300])
        writer = Writer(instructions('dual_dma.c', 'row_four_i_code'), 0, pc=2, x=0)
        for _ in range(8):
            self.assertFalse(writer.step()[2])
        self.assertEqual(writer.pc, 0)
        self.assertTrue(writer.step()[2])

    def test_frozen_l7_waveform_router_wraps_and_writer_parity(self):
        for seed, delay, skew, divider in ((0, 0, False, 2), (0x2350, 1, True, 2),
                                           (0xffffffff, 2, True, 4)):
            with self.subTest(seed=seed, delay=delay, skew=skew, divider=divider):
                model = Model(seed=seed, delay=delay, skew=skew, divider=divider)
                model.start()
                model.run(512 * 128)
                self.assertFalse(model.stalls)
                self.assertEqual(model.clock_mismatches, 0)
                self.assertTrue(all(axis.transfers == 512 * 32 for axis in model.axes))
                self.assertTrue(all(axis.router_cursor == 512 * 16 for axis in model.axes))
                self.assertTrue(all(machine.rx_stalls for machine in model.pointers))

    def test_live_4096_word_ring_wrap_retains_history(self):
        model = Model(words=9 * 512, seed=0x1234abcd, skew=True)
        model.start()
        model.run(9 * 512 * 128)
        self.assertFalse(model.stalls)
        self.assertEqual(model.published, 9 * 512)
        self.assertEqual(model.max_source_index, 9 * 512)
        self.assertGreater(min(axis.router_cursor for axis in model.axes), 4096 * 16)

    def test_credit_exhaustion_stops_at_exact_authorized_word(self):
        model = Model(words=1024, grant_limit=512, skew=True)
        model.start()
        model.run(1024 * 128)
        self.assertTrue(model.stalls)
        self.assertEqual(model.stalls[0][0], 512 * 128)
        self.assertEqual([machine.issued for machine in model.pointers], [512, 512])
        self.assertEqual([machine.pushed for machine in model.pointers], [8192, 8192])
        self.assertEqual(model.max_source_index, model.granted)

    def test_inverted_clock_negative_control_preserves_exact_data(self):
        model = Model(invert=True, skew=True)
        model.start()
        model.run(8192)
        self.assertFalse(model.stalls)
        self.assertEqual(model.clock_mismatches, 8192)

    def test_isolated_completion_blackout_uses_additional_fifo_runway(self):
        def passes(writers, latency):
            model = Model(writers=writers, completion_delay=lambda axis, row:
                          latency if axis == 0 and row == 64 else 1)
            model.start()
            model.run(4096)
            return not model.stalls

        thresholds = []
        for writers in (2, 4):
            low, high = 1, 256
            while low + 1 < high:
                middle = (low + high) // 2
                if passes(writers, middle):
                    low = middle
                else:
                    high = middle
            self.assertTrue(passes(writers, low))
            self.assertFalse(passes(writers, high))
            thresholds.append(low)
        self.assertGreater(thresholds[1], thresholds[0])
        print(f'B3 isolated completion blackout, simulated SYS slots: two={thresholds[0]}, '
              f'serialized-four={thresholds[1]} (event model, not device timing)')

    def test_wrong_router_and_excessive_latency_are_detected(self):
        with self.assertRaisesRegex(AssertionError, 'wrong writer'):
            Model(wrong_route=True).start()
        model = Model(delay=32, skew=True)
        model.start()
        model.run(512 * 128)
        self.assertTrue(model.stalls)
        self.assertLess(model.stalls[0][0], 512 * 128)

    def test_stop_quiesces_control_first_word_second_word_and_router(self):
        for stage, word in (('control', 0), ('data', 0), ('data', 1), ('router', 2)):
            with self.subTest(stage=stage, word=word):
                model = Model(delay=2, skew=True)
                model.start()
                for _ in range(10000):
                    if model.axes[0].stage == stage and (stage == 'control' or model.axes[0].word == word):
                        break
                    model.step()
                else:
                    self.fail('requested handoff phase was not reached')
                model.stop()
                snapshot = ([machine.issued for machine in model.pointers],
                            [(axis.stage, axis.router_cursor, axis.transfers) for axis in model.axes],
                            model.bus_tick)
                for _ in range(100):
                    model.step()
                self.assertEqual(snapshot, ([machine.issued for machine in model.pointers],
                                            [(axis.stage, axis.router_cursor, axis.transfers) for axis in model.axes],
                                            model.bus_tick))


if __name__ == '__main__':
    unittest.main()
