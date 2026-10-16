"""Instruction-level exactness check for the 16APSK burst assembler.

Runs the actual assembly source (including its AXIS macro), with checked memory
and AAPCS registers. Calls to the short C prefix/fallback execute the native C
leaf. This is an ISA semantics model, not Arm silicon execution or a timing test.
The existing test_iq16chunk.py independently checks the mathematical FIR/table.
"""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))
from test_iq16chunk import Tables, PTR, P16, CANARY, ROWS, filter_coefficients, levels, direct_output, decode_words
import fwsrc  # noqa: E402  firmware/src/<area>/ layout

MASK = (1 << 32) - 1
END = 0xeeeeeee0
BASE = dict(edge=0x20000000, middle=0x20010000, reverse=0x20020000,
            tables=0x20023000, input=0x21000000, previous=0x22000000,
            output=0x23000000, stack=0x24000000)


def program(path):
    """Expand the real macro and retain real instruction order/branch labels."""
    lines = path.read_text().splitlines()
    macro, body, code, labels = None, [], [], {}
    for line in lines:
        line = line.split('//')[0].strip()
        if not line:
            continue
        if line.startswith('.macro'):
            assert macro is None
            macro = line.split()[1]
            continue
        if line == '.endm':
            assert macro == 'AXIS'
            macro = None
            continue
        if macro:
            body.append(line)
            continue
        if line.startswith('AXIS '):
            shift, offset = line.split()[1].split(',')
            code.extend(s.replace('\\shift', shift).replace('\\offset', offset) for s in body)
            continue
        if ':' in line:
            label, line = line.split(':', 1)
            assert label not in labels
            labels[label] = len(code) * 4
            line = line.strip()
        if line and not line.startswith('.'):
            code.append(line)
    return code, labels


class Memory:
    def __init__(self):
        self.regions = []
        self.reads = self.writes = 0

    def add(self, name, data, writable=False):
        data = bytearray(data)
        self.regions.append((BASE[name], data, writable))
        return data

    def locate(self, address, size, write=False):
        assert address % min(size, 4) == 0, ('unaligned', hex(address), size)
        for base, data, writable in self.regions:
            off = address - base
            if 0 <= off and off + size <= len(data):
                assert not write or writable, ('read-only store', hex(address))
                return data, off
        raise AssertionError(('unmapped access', hex(address), size, write))

    def read(self, address, size=4):
        data, off = self.locate(address, size)
        self.reads += 1
        return int.from_bytes(data[off:off+size], 'little')

    def write(self, address, value, size=4):
        data, off = self.locate(address, size, True)
        data[off:off+size] = (value & ((1 << (size*8)) - 1)).to_bytes(size, 'little')
        self.writes += 1

    def ptr(self, address, size):
        data, off = self.locate(address, size)
        return ctypes.cast(ctypes.byref((ctypes.c_uint8 * len(data)).from_buffer(data), off), PTR)


def operands(text):
    return [x.strip() for x in re.split(r',\s*(?![^\[\{]*[\]\}])', text)]


def reg(name):
    return {'sp': 13, 'lr': 14, 'pc': 15}.get(name, int(name[1:]) if name.startswith('r') else -1)


def reglist(text):
    result = []
    for part in text.strip('{}').split(','):
        if '-' in part:
            a, b = map(reg, part.split('-'))
            result.extend(range(a, b+1))
        else:
            result.append(reg(part.strip()))
    assert result == sorted(result)
    return result


class Machine:
    def __init__(self, code, labels, memory, lib):
        self.code, self.labels, self.mem, self.lib = code, labels, memory, lib
        self.r = [0] * 16
        self.zero = self.borrow = False
        self.steps = self.c_calls = 0

    def value(self, text):
        if text.startswith('#'):
            return sum(int(x.strip(), 0) for x in text[1:].split('+'))
        return self.r[reg(text)]

    def address(self, text):
        a = operands(text.strip('[]'))
        address = self.value(a[0])
        if len(a) >= 2:
            offset = self.value(a[1])
            if len(a) == 3:
                assert a[2].startswith('lsl #')
                offset <<= int(a[2][5:])
            address += offset
        return address & MASK

    def c_leaf(self):
        r, mem = self.r, self.mem
        assert r[13] % 8 == 0, 'AAPCS stack alignment at C call'
        tables = mem.read(r[13])
        t = Tables(mem.ptr(mem.read(tables), ROWS*16), mem.ptr(mem.read(tables+4), ROWS*16),
                   ctypes.cast(mem.ptr(mem.read(tables+8), ROWS*2), P16) if mem.read(tables+8) else None)
        self.lib.iq16chunk_kernel_c(mem.ptr(r[0], r[2]*32), mem.ptr(r[1], r[2]*4), r[2],
                                   mem.ptr(r[3], 8) if r[3] else None, ctypes.byref(t))
        return_pc = r[14] & ~1
        # The ABI permits the real C leaf to clobber every caller-saved register.
        for i in (0, 1, 2, 3, 12):
            r[i] = 0xdec00000 + i
        r[14] = 0xdec0000e
        r[15] = return_pc
        self.zero = self.borrow = False
        self.c_calls += 1

    def run(self):
        r, mem = self.r, self.mem
        while r[15] != END:
            assert self.steps < 2_000_000, 'nonterminating assembly'
            pc = r[15]
            assert pc % 4 == 0 and 0 <= pc//4 < len(self.code)
            text = self.code[pc//4]
            op, *tail = text.split(maxsplit=1)
            a = operands(tail[0]) if tail else []
            r[15] += 4
            self.steps += 1
            if op in ('cmp', 'subs'):
                x, y = map(self.value, a[-2:])
                self.zero, self.borrow = x == y, x < y
                if op == 'subs':
                    if len(a) == 2:
                        x = self.value(a[0]); y = self.value(a[1])
                        self.zero, self.borrow = x == y, x < y
                    r[reg(a[0])] = (x-y) & MASK
            elif op in ('beq', 'bne', 'b'):
                take = op == 'b' or self.zero == (op == 'beq')
                if take:
                    if a[0] == 'iq16chunk_kernel_c':
                        self.c_leaf()
                    else:
                        assert a[0][-1] in 'bf'
                        r[15] = self.labels[a[0][:-1]]
            elif op == 'bl':
                assert a == ['iq16chunk_kernel_c']
                r[14] = r[15] | 1
                self.c_leaf()
            elif op in ('mov', 'movs', 'movlo'):
                if op != 'movlo' or self.borrow:
                    r[reg(a[0])] = self.value(a[1])
            elif op == 'it':
                assert a == ['lo']
                assert self.code[r[15]//4].startswith('movlo ')
            elif op in ('add', 'sub'):
                dest = reg(a[0]); src = self.value(a[1]) if len(a) > 2 else r[dest]
                value = self.value(a[2]) if len(a) > 2 else self.value(a[1])
                if len(a) == 4:
                    assert a[3].startswith('lsl #')
                    value <<= int(a[3][5:])
                r[dest] = (src + value if op == 'add' else src-value) & MASK
            elif op in ('ldr', 'ldrh', 'str'):
                address = self.address(a[1])
                if op == 'str':
                    mem.write(address, self.value(a[0]))
                else:
                    r[reg(a[0])] = mem.read(address, 2 if op == 'ldrh' else 4)
            elif op == 'ldm':
                base = self.value(a[0])
                values = [mem.read(base + 4*i) for i, _ in enumerate(reglist(a[1]))]
                for index, value in zip(reglist(a[1]), values):
                    r[index] = value
            elif op == 'ubfx':
                r[reg(a[0])] = (self.value(a[1]) >> self.value(a[2])) & ((1 << self.value(a[3]))-1)
            elif op == 'sadd16':
                x, y = self.value(a[1]), self.value(a[2])
                r[reg(a[0])] = ((x+y) & 0xffff) | ((((x >> 16) + (y >> 16)) & 0xffff) << 16)
            elif op == 'ror':
                assert a[2] == '#16'
                x = self.value(a[1]); r[reg(a[0])] = ((x >> 16) | (x << 16)) & MASK
            elif op in ('push', 'pop'):
                registers = reglist(a[0])
                if op == 'push':
                    r[13] -= 4*len(registers)
                    for i, index in enumerate(registers):
                        mem.write(r[13] + 4*i, r[index])
                else:
                    values = [mem.read(r[13] + 4*i) for i in range(len(registers))]
                    r[13] += 4*len(registers)
                    for index, value in zip(registers, values):
                        r[index] = value & ~1 if index == 15 else value
            elif op == 'bx':
                r[15] = self.value(a[0]) & ~1
            else:
                raise AssertionError(('unsupported actual instruction', text))
        return self.steps


def execute(code, labels, lib, edge, middle, reverse, data, previous, use_map=True):
    mem = Memory()
    mem.add('edge', edge.tobytes()); mem.add('middle', middle.tobytes())
    mem.add('reverse', reverse.tobytes())
    mem.add('tables', np.array([BASE['edge'], BASE['middle'], BASE['reverse'] if use_map else 0], '<u4').tobytes())
    input_bytes = mem.add('input', data.astype('<u4').tobytes())
    previous_bytes = mem.add('previous', (np.zeros(2, np.uint32) if previous is None else previous).astype('<u4').tobytes())
    output = mem.add('output', np.full(len(data)*8 + 2, CANARY, '<u4').tobytes(), True)
    stack = mem.add('stack', bytes(4096), True)
    machine = Machine(code, labels, mem, lib)
    machine.r = [(0x12340000 + i*0x111) for i in range(16)]
    machine.r[:4] = [BASE['output']+4, BASE['input'], len(data), BASE['previous'] if previous is not None else 0]
    machine.r[13:16] = [BASE['stack']+4096-8, END | 1, labels['iq16chunk_kernel_asm']]
    mem.write(machine.r[13], BASE['tables'])
    saved = machine.r[4:12].copy(); sp = machine.r[13]
    machine.run()
    assert machine.r[4:12] == saved and machine.r[13] == sp, 'callee-saved registers/stack'
    assert input_bytes == data.astype('<u4').tobytes()
    if previous is not None:
        assert previous_bytes == previous.astype('<u4').tobytes()
    words = np.frombuffer(output, '<u4')
    assert words[0] == words[-1] == CANARY, 'output overrun'
    assert stack[:4000] == bytes(4000), 'stack outside the 48-byte frame'
    return words[1:-1].copy(), machine.steps, machine.c_calls


def main():
    source = ROOT / 'firmware/src'
    assembly = source / 'apsk/iq16chunk_asm.S'
    code, labels = program(assembly)
    cases = instructions = 0
    with tempfile.TemporaryDirectory(prefix='iq16chunk-asm-') as temp:
        library = Path(temp) / 'chunk.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        *fwsrc.INCLUDES, str(source / 'apsk/iq16chunk.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iq16chunk_build.argtypes = [ctypes.POINTER(Tables), ctypes.c_uint]
        lib.iq16chunk_build.restype = ctypes.c_bool
        lib.iq16chunk_build_reverse.argtypes = [P16]
        lib.iq16chunk_kernel_c.argtypes = [PTR, PTR, ctypes.c_uint32, PTR, ctypes.POINTER(Tables)]
        edge = np.zeros(ROWS*4, np.uint32); middle = edge.copy()
        reverse = np.zeros(ROWS, np.uint16)
        lib.iq16chunk_build_reverse(reverse.ctypes.data_as(P16))
        tables = Tables(edge.ctypes.data_as(PTR), middle.ctypes.data_as(PTR), reverse.ctypes.data_as(P16))
        rng = np.random.default_rng(20261015)
        fixtures = [rng.integers(0, 1<<32, size=n, dtype=np.uint32) for n in (1, 2, 3, 7, 64, 256)]
        # All 4096 keys occur independently in both axes and all three history jobs.
        keys = np.arange(ROWS, dtype=np.uint32)
        fixtures += [keys | (((keys*17+123)&4095)<<16) | np.uint32(0xf000f000)]
        for gamma in (315, 285, 275, 270, 260, 257):
            assert lib.iq16chunk_build(ctypes.byref(tables), gamma)
            _, q30 = levels(gamma); _, coef = filter_coefficients()
            for data in fixtures:
                for previous in (None, rng.integers(0, 1<<32, size=2, dtype=np.uint32)):
                    codes = decode_words(data, None)
                    prior = [np.zeros(8, np.int64)]*2 if previous is None else decode_words(previous, None)
                    _, expected = direct_output(codes, prior, q30, coef)
                    actual, steps, calls = execute(code, labels, lib, edge, middle, reverse, data, previous)
                    np.testing.assert_array_equal(actual, expected)
                    assert calls == 1
                    cases += 1; instructions += steps
            # Tail fallback with no reversal map must leave ABI/history unchanged.
            data = fixtures[3]; previous = rng.integers(0, 1<<32, size=2, dtype=np.uint32)
            actual, steps, calls = execute(code, labels, lib, edge, middle, reverse, data, previous, False)
            _, expected = direct_output(decode_words(data, None), decode_words(previous, None), q30, coef)
            np.testing.assert_array_equal(actual, expected); assert calls == 1
            cases += 1; instructions += steps
        # All-null zero case must return before dereferencing the fifth argument.
        machine = Machine(code, labels, Memory(), lib)
        machine.r[2] = 0; machine.r[14] = END | 1
        machine.run(); assert machine.c_calls == 0
        cases += 1; instructions += machine.steps
    print(json.dumps(dict(profile='iq16chunk_burst_asm',model='actual_assembly_source_instruction_semantics',
                          cases=cases,executed_instructions=instructions,all_axis_keys=4096,
                          gamma_count=6,stack_frame_bytes=48,silicon_timing=False)))

if __name__ == '__main__':
    main()
