"""Execute pointer/writer opcodes around the alternating DMA router; no USB.

One control DMA per axis owns that pointer RX DREQ. Its sample DMA completion
starts a FORCE-paced router, which selects the other sample channel and triggers
the same control again. The model includes finite FIFO capacity, independently
delayed handoffs, source-ring reuse and exact L8 samples. Event slots here are a
simulation, not measured DMA arbitration or a bound on live M33 throughput.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
import re
import unittest

import numpy as np

import test_iqquad as quad
import test_iqquadptr as ptr
from test_dual_faults import function_body
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
MASK = 0xffffffff
BASE = 0x20020000


def instructions(filename, name):
    source = fwsrc.path(filename).read_text()
    match = re.search(r'\b' + re.escape(name) + r'\[[^]]*\]\s*=\s*\{([^}]+)\}', source)
    if not match:
        raise AssertionError(f'missing production instruction array {name}')
    body = re.sub(r'/\*.*?\*/|//[^\n]*', '', match[1], flags=re.S)
    return [int(value.strip(), 0) for value in body.split(',') if value.strip()]


def router_targets():
    source = (ROOT / 'firmware/src/output/dual_dma.c').read_text()
    body = function_body(source, 'axis_alternating_fifo_dma')
    targets = re.findall(r'fifo_targets\[axis\]\[([01])\]\s*=\s*\(uintptr_t\)\s*'
                         r'&dma_hw->ch\[(odd|even)\]\.al3_read_addr_trig', body)
    assert [index for index, _ in targets] == ['0', '1'], 'missing production router entries'
    return tuple(int(owner == 'odd') for _, owner in targets)


@dataclass
class Pointer:
    code: list[int]
    previous: int
    pc: int = 0
    shifted: int = 32
    issued: int = 0
    pushed: int = 0
    rx_stalls: int = 0
    tx: deque = field(default_factory=deque)
    rx: deque = field(default_factory=deque)

    def __post_init__(self):
        upper = BASE >> 16
        self.regs = dict(x=((self.previous >> 12) & 15) | ((self.previous >> 4) & 0xf0),
                         y=((upper & 255) << 8) | (upper >> 8), isr=0, osr=0)

    def step(self):
        insn = self.code[self.pc]
        nxt = (self.pc + 1) % len(self.code)
        major, arg, count = insn >> 13, (insn >> 5) & 7, insn & 31
        sources = {1: 'x', 2: 'y', 3: None, 6: 'isr', 7: 'osr'}
        if major == 0:
            assert arg == 7
            if self.shifted < 16:
                nxt = count
        elif major == 4:
            if insn & 0x80:
                if not self.tx:
                    return
                value = self.tx.popleft()
                self.regs['osr'], self.shifted = value | value << 16, 0
            else:
                if len(self.rx) == 4:
                    self.rx_stalls += 1
                    return
                self.rx.append((self.pushed, self.regs['isr']))
                self.pushed += 1
                self.regs['isr'] = 0
        elif major == 5:
            operation = (insn >> 3) & 3
            assert operation in (0, 2)
            value = self.regs[sources[insn & 7]]
            self.regs[sources[arg]] = (int(f'{value:032b}'[::-1], 2)
                                      if operation == 2 else value)
        elif major == 2:
            count = count or 32
            value = self.regs[sources[arg]] if sources[arg] else 0
            self.regs['isr'] = ((self.regs['isr'] << count) |
                                (value & ((1 << count) - 1))) & MASK
        elif major == 3:
            assert arg == 3
            count = count or 32
            self.regs['osr'] >>= count
            self.shifted = min(32, self.shifted + count)
        else:
            raise AssertionError(f'unsupported pointer opcode 0x{insn:04x}')
        self.pc = nxt


@dataclass(frozen=True)
class Word:
    row: int
    index: int
    value: int


@dataclass
class Writer:
    code: list[int]
    stream: int
    pc: int
    x: int
    delay: int = 0
    half: int = 0
    osr: Word | None = None
    fifo: deque = field(default_factory=deque)

    def pull(self):
        if self.osr is None and self.fifo:
            self.osr, self.half = self.fifo.popleft(), 0

    def step(self):
        # RP2350 §11.5.4.2: non-OUT cycles can refill without stalling; an OUT
        # that starts empty stalls even if it can pull that cycle. The last OUT
        # from a nonempty OSR may refill simultaneously with its final shift.
        if self.delay:
            self.pull()
            self.delay -= 1
            return None, None, False
        insn = self.code[self.pc]
        flag = (insn >> 11) & 1 if insn & 0x1000 else None
        major = insn >> 13
        nxt = (self.pc + 1) % len(self.code)
        sample = None
        if major == 3:
            assert ((insn >> 5) & 7, insn & 31) == (0, 16)
            if self.osr is None:
                self.pull()
                return None, flag, True
            sample = (self.osr.row, 2 * self.osr.index + self.half,
                      (self.osr.value >> (16 * self.half)) & 0x3fff)
            self.half += 1
            if self.half == 2:
                self.osr = None
                self.pull()
        elif major == 0:
            condition, target = (insn >> 5) & 7, insn & 31
            if condition == 0:
                nxt = target
            elif condition == 2:
                old = self.x
                self.x = (old - 1) & MASK
                if old:
                    nxt = target
            else:
                raise AssertionError(f'unsupported writer branch {condition}')
        elif major == 7:
            assert (insn >> 5) & 7 == 1
            self.x = insn & 31
        elif major == 5:
            assert insn & 0xff == 0x42       # MOV Y,Y, including delayed NOP
        else:
            raise AssertionError(f'unsupported writer opcode 0x{insn:04x}')
        if major != 3:
            self.pull()
        self.delay = (insn >> 8) & 7
        self.pc = nxt
        return sample, flag, False


@dataclass
class AxisDMA:
    selected: int = 0
    stage: str = 'control'
    ready: int = 0
    row: int = 0
    address: int = 0
    word: int = 0
    router_cursor: int = 0
    controls: int = 0
    transfers: int = 0


class Model:
    def __init__(self, words=512, seed=0x2350, delay=0, skew=False, grant_limit=None, divider=1,
                 wrong_route=False, control_bswap=True, writers=4, completion_delay=None):
        self.input = quad.m.xorshift32(seed, words)
        self.previous = 0x716bc429
        self.pointers = [Pointer(instructions('iqquadptr.h', 'IQQUADPTR_L8_PROGRAM'),
                                 (self.previous >> (16 * axis)) & 0xffff) for axis in range(2)]
        prefix = 'quad_four' if writers == 4 else 'paired'
        codes = [instructions('dual_dma.c', prefix + '_i_code'),
                 instructions('dual_dma.c', prefix + '_q_code')]
        self.writers = [Writer(codes[s & 1], s, pc, x)
                        for s, (pc, x) in enumerate(zip((0, 1, 2, 1), (15, 16, 0, 0)))
                        if s < writers]
        self.writer_count, self.completion_delay = writers, completion_delay
        self.axes = [AxisDMA(), AxisDMA()]
        self.router = tuple(reversed(router_targets())) if wrong_route else router_targets()
        self.delay, self.skew, self.control_bswap = delay, skew, control_bswap
        self.divider, self.enabled_start = divider, 0
        self.time = self.bus_tick = self.next_axis = self.published = self.granted = 0
        self.ring = [None] * 8192
        self.grant_limit = words if grant_limit is None else grant_limit
        self.enabled = self.stopped = False
        self.stalls = []
        self.max_source_index = 0
        pulse = SimpleNamespace(L=8, sps=4, coef=quad.L8_COEFFICIENTS)
        permutation = [ptr.l8_reverse_groups(key) for key in range(2048)]
        self.table = quad.fir_rows(pulse)[permutation].tolist()
        self.expected_addresses = [ptr.reference(
            [(int(word) >> (16 * axis)) & 0xffff for word in self.input],
            (self.previous >> (16 * axis)) & 0xffff, BASE, 8) for axis in range(2)]
        packed = quad.scalar_waveform(self.input, self.previous, quad.L8_COEFFICIENTS).reshape(-1, 2)
        self.expected = [np.stack((packed[:, axis] & 0x3fff,
                                  (packed[:, axis] >> 16) & 0x3fff), axis=-1).ravel()
                         for axis in range(2)]

    def handoff(self, axis, stage, row):
        # Independent, deterministic latency. There is no cycle guarantee here.
        if not self.delay:
            return 1
        return 1 + ((row * 13 + axis * 7 + len(stage)) % (self.delay + 1))

    def source(self):
        reusable = max(0, min(p.issued for p in self.pointers) - 8)
        end = min(len(self.input), reusable + len(self.ring))
        while self.published < end:
            n = self.published
            self.ring[n & 8191] = n, int(self.input[n])
            self.published += 1
        # Actual credit geometry; the C leaf/sentinel publication is native-tested
        # separately. Here only complete, immutable credited batches can be read.
        self.granted = min(self.published // 512 * 512, self.grant_limit)
        for axis, machine in enumerate(self.pointers):
            if len(machine.tx) == 4 or machine.issued >= self.granted:
                continue
            if self.skew and (self.time + 3 * axis) % (13 + 4 * axis) < 3:
                continue
            index, value = self.ring[machine.issued & 8191]
            assert index == machine.issued, 'source ring overwritten before both axes retired it'
            machine.tx.append((value >> (16 * axis)) & 0xffff)
            machine.issued += 1
            self.max_source_index = max(self.max_source_index, machine.issued)
        assert self.max_source_index <= self.granted <= self.published

    def transfer(self, axis):
        dma, pointer = self.axes[axis], self.pointers[axis]
        completed = False
        if self.time < dma.ready:
            return False
        if self.skew and (self.time + 5 * axis) % (97 + 4 * axis) < 2:
            return False
        if dma.stage == 'control':
            if not pointer.rx:
                return False
            row, raw = pointer.rx.popleft()
            address = ptr.bswap(raw) if self.control_bswap else raw
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
            key = (dma.address - BASE) // 32
            assert 0 <= key < 2048 and not (dma.address & 31)
            writer.fifo.append(Word(dma.row, dma.word, self.table[key][dma.word]))
            dma.word += 1
            dma.transfers += 1
            if dma.word == 8:
                dma.stage = 'router' if self.writer_count == 4 else 'control'
                completed = True
        elif dma.stage == 'router':
            # R's read address wraps in its two-entry, eight-byte SRAM ring.
            # Its one alias write changes C.WRITE_ADDR and triggers that same C.
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
        # At most one output-DMA transfer per simulated SYS slot. The shared
        # source, SPI/capture traffic and real bus read/write scheduling are not
        # cycle modeled; skew/latency deliberately perturb this event schedule.
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
                axis, row = self.bus_tick & 1, self.bus_tick // 32
                owner = axis + (2 * (row & 1) if self.writer_count == 4 else 0)
                within = (self.bus_tick // 2) & 15
                value = int(self.expected[axis][self.bus_tick // 2])
                assert data == [(owner, (row, within, value))], f'data order at slot {self.bus_tick}'
                assert flags == [(owner, 1 - axis)], f'flag order at slot {self.bus_tick}'
                # The production DDR program starts high with I and low with Q.
                clock = instructions_cached_clock[self.bus_tick & 1]
                assert (clock >> 12) & 1 == 1 - axis
            self.bus_tick += 1
        self.time += 1

    def start(self):
        for _ in range(10000):
            if all(len(writer.fifo) == 8 for writer in self.writers):
                break
            self.step()
        else:
            raise AssertionError('four output FIFOs failed to prefill')
        for writer in self.writers:
            writer.pull()                  # production's explicit initial PULL
        self.enabled_start = self.time
        self.enabled = True

    def run(self, slots):
        target = self.bus_tick + slots
        while self.bus_tick < target and not self.stalls:
            self.step()

    def stop(self):
        # Output is disabled before pointer/DMA claims can be released.
        self.enabled = False
        self.stopped = True


instructions_cached_clock = instructions('dual_dma.c', 'ddr_clock_code')


class QuadFIFOOrdering(unittest.TestCase):
    def test_pointer_and_writer_program_shapes(self):
        self.assertEqual(len(instructions('iqquadptr.h', 'IQQUADPTR_L8_PROGRAM')), 13)
        self.assertEqual(len(instructions('dual_dma.c', 'quad_four_i_code')), 6)
        self.assertEqual(len(instructions('dual_dma.c', 'quad_four_q_code')), 6)
        self.assertEqual(instructions_cached_clock, [0xb042, 0xa042])

    def test_autopull_non_out_progress_and_empty_out_fence(self):
        code = instructions('dual_dma.c', 'paired_i_code')
        writer = Writer(code, stream=0, pc=1, x=0)
        self.assertEqual(writer.step(), (None, None, False))
        self.assertEqual(writer.pc, 0)             # empty NOP does not stall
        writer.fifo.append(Word(0, 0, 0x00020001))
        self.assertEqual(writer.step(), (None, 1, True))
        self.assertEqual(writer.pc, 0)             # pull and OUT need separate cycles
        self.assertEqual(writer.step(), ((0, 0, 1), 1, False))
        writer.step()                             # intervening NOP
        writer.fifo.append(Word(0, 1, 0x00040003))
        self.assertEqual(writer.step(), ((0, 1, 2), 1, False))
        self.assertEqual(writer.osr, Word(0, 1, 0x00040003))  # final OUT refills in parallel
        silent = Writer(instructions('dual_dma.c', 'quad_four_i_code'), 0, pc=2, x=0)
        for _ in range(32):
            self.assertFalse(silent.step()[2])     # entire silent interval tolerates empty TX
        self.assertEqual(silent.pc, 0)
        self.assertTrue(silent.step()[2])          # first mandatory OUT is the deadline

    def test_production_router_links_and_startup_ownership(self):
        source = (ROOT / 'firmware/src/output/dual_dma.c').read_text()
        body = function_body(source, 'axis_alternating_fifo_dma')
        compact = re.sub(r'\s+', '', re.sub(r'//[^\n]*|/\*.*?\*/', '', body, flags=re.S))
        self.assertEqual(router_targets(), (1, 0))
        for call in ('axis_fifo_dma(control,even,state.sm[axis],',
                     'axis_data_dma(router,even,state.sm[axis]);',
                     'axis_data_dma(router,odd,state.sm[axis+2]);',
                     'channel_config_set_ring(&dc,false,3);',
                     'channel_config_set_dreq(&dc,DREQ_FORCE);',
                     'channel_config_set_chain_to(&dc,router);',
                     'dma_channel_configure(router,&dc,&dma_hw->ch[control].al2_write_addr_trig,'):
            self.assertIn(call, compact)
        start = function_body(source, 'start_output')
        self.assertRegex(start, r'if\s*\(!state\.fifo_source\s*\|\|\s*state\.writers\s*==\s*2'
                         r'\s*\|\|\s*s\s*<\s*2\)\s*control_mask\s*\|=')

    def test_full_waveform_and_router_wraps_under_skew(self):
        for seed, delay, skew, divider in ((0, 0, False, 2), (0x2350, 1, True, 1),
                                           (0xffffffff, 2, True, 1)):
            with self.subTest(seed=seed, handoff_delay=delay, skew=skew, divider=divider):
                model = Model(seed=seed, delay=delay, skew=skew, divider=divider)
                model.start()
                model.run(512 * 128)
                self.assertFalse(model.stalls)
                self.assertTrue(all(axis.router_cursor >= 2047 for axis in model.axes))
                self.assertTrue(all(pointer.rx_stalls for pointer in model.pointers))
                self.assertTrue(all(axis.transfers == 512 * 32 for axis in model.axes))

    def test_actual_8192_word_input_ring_wrap(self):
        model = Model(words=17 * 512, seed=0x1234abcd, skew=True)
        model.start()
        model.run(len(model.input) * 128)
        self.assertFalse(model.stalls)
        self.assertGreater(model.max_source_index, 8192)
        self.assertEqual(model.published, len(model.input))
        self.assertGreater(min(axis.router_cursor for axis in model.axes), 16 * 2048)

    def test_finite_credits_drain_without_reading_ungranted_input(self):
        model = Model(words=1024, grant_limit=512, skew=True)
        model.start()
        model.run(1024 * 128)
        self.assertTrue(model.stalls)
        self.assertEqual(model.stalls[0][0], 512 * 128)
        self.assertEqual([p.issued for p in model.pointers], [512, 512])
        self.assertEqual([p.pushed for p in model.pointers], [2048, 2048])
        self.assertEqual(model.max_source_index, model.granted)

    def test_excessive_handoff_delay_is_detected_as_output_starvation(self):
        model = Model(delay=32, skew=True)
        model.start()
        model.run(512 * 128)
        self.assertTrue(model.stalls)
        self.assertLess(model.stalls[0][0], 512 * 128)
        self.assertLessEqual(model.max_source_index, model.granted)

    def test_serialized_router_compared_with_two_writer_handoff_margin(self):
        def passes(writers, latency):
            # One blackout after one row completes, with identical data service
            # and all other handoffs. The four-writer router gets no independent
            # credit or prefetched pointer while its previous sample row waits.
            model = Model(writers=writers, completion_delay=lambda axis, row:
                          latency if axis == 0 and row == 32 else 1)
            model.start()
            model.run(2048)
            return not model.stalls

        thresholds = []
        for writers in (2, 4):
            low, high = 1, 128
            while low + 1 < high:
                middle = (low + high) // 2
                if passes(writers, middle):
                    low = middle
                else:
                    high = middle
            self.assertTrue(passes(writers, low))
            self.assertFalse(passes(writers, high))
            thresholds.append(low)
        # An empty OSR can wait throughout the writer's silent interval: its
        # first mandatory OUT is the actual deadline, not its earlier autopull.
        self.assertGreater(thresholds[1], thresholds[0])
        blackout = (thresholds[0] + thresholds[1]) // 2
        self.assertFalse(passes(2, blackout))
        self.assertTrue(passes(4, blackout))
        print(f'isolated completion blackout, simulated SYS slots: two={thresholds[0]}, '
              f'serialized-four={thresholds[1]} (event model, not device timing)')

    def test_wrong_router_phase_and_missing_bswap_fail(self):
        for options, message in ((dict(wrong_route=True), 'wrong writer'),
                                 (dict(control_bswap=False), 'LUT address')):
            with self.subTest(options=options), self.assertRaisesRegex(AssertionError, message):
                model = Model(**options)
                model.start()

    def test_repeated_completion_latency_costs_sustained_margin(self):
        # Contrast a delay after EVERY row with the isolated blackout above.
        # The extra router uses bandwidth; extra FIFOs cannot absorb a sustained
        # producer deficit. These thresholds belong only to this event schedule.
        for writers, limit in ((2, 16), (4, 14)):
            for latency in (limit, limit + 1):
                with self.subTest(writers=writers, latency=latency):
                    model = Model(writers=writers,
                                  completion_delay=lambda axis, row: latency)
                    model.start()
                    model.run(512 * 128)
                    self.assertEqual(not model.stalls, latency == limit)

    def test_stop_quiesces_every_handoff_phase(self):
        for stage in ('control', 'data', 'router'):
            model = Model(delay=2, skew=True)
            model.start()
            for _ in range(1000):
                if model.axes[0].stage == stage:
                    break
                model.step()
            self.assertEqual(model.axes[0].stage, stage)
            model.stop()
            snapshot = ([p.issued for p in model.pointers],
                        [(a.stage, a.router_cursor, a.transfers) for a in model.axes], model.bus_tick)
            for _ in range(100):
                model.step()
            self.assertEqual(snapshot, ([p.issued for p in model.pointers],
                                        [(a.stage, a.router_cursor, a.transfers) for a in model.axes],
                                        model.bus_tick))

    def test_two_readers_have_stale_credits_even_with_alternating_start(self):
        # DREQ is credited to every attached DMA channel, not to whichever is
        # currently active. Starting readers alternately cannot transfer sole
        # ownership of the shared FIFO: an inactive reader retains old credit.
        fifo, credits = deque(), [0, 0]
        fifo.append(0)
        credits = [credit + 1 for credit in credits]
        self.assertEqual(fifo.popleft(), 0)        # only C-even was started
        credits[0] -= 1
        self.assertEqual(credits, [0, 1])
        self.assertFalse(fifo)
        self.assertGreater(credits[1], 0)         # C-odd would read an empty RX FIFO
        # With two queued pointers, arbitrary competing-reader arbitration can
        # assign the chronological even row to the odd writer immediately.
        fifo.extend((0, 1))
        stolen = fifo.popleft()
        self.assertNotEqual(stolen & 1, 1)


if __name__ == '__main__':
    unittest.main()
