"""Execute the actual LDPC entry leaf in a checked instruction/AAPCS model.

The independent bit-at-a-time extraction checks every register shift, all source
word offsets and repeated/dense row updates. This models ISA semantics from the
real source, not Arm silicon execution or M33 timing.
"""
import ast
import json
from pathlib import Path
import random
import re
import struct

ROOT = Path(__file__).resolve().parents[1]
MASK = 0xFFFFFFFF
END = 0xEEEEEEE0
CANARY = 0x9AD4C781
BASE = dict(matrix=0x20000000, source=0x21000000, rows=0x22000000,
            offsets=0x23000000, stack=0x24000000)


def operands(text):
    return [x.strip() for x in re.split(r',\s*(?![^\[\{]*[\]\}])', text)]


def reg(name):
    return {'sp': 13, 'lr': 14, 'pc': 15}.get(name, int(name[1:]) if name.startswith('r') else -1)


def reglist(text):
    result = []
    for part in text.strip('{}').split(','):
        if '-' in part:
            first, last = map(reg, part.strip().split('-'))
            result.extend(range(first, last + 1))
        else:
            result.append(reg(part.strip()))
    assert result == sorted(result)
    return result


def integer(text):
    """Only arithmetic immediate expressions used by the assembler macros."""
    node = ast.parse(text.lstrip('#'), mode='eval').body

    def evaluate(value):
        if isinstance(value, ast.Constant) and type(value.value) is int:
            return value.value
        assert isinstance(value, ast.BinOp)
        a, b = evaluate(value.left), evaluate(value.right)
        if isinstance(value.op, ast.Add):
            return a + b
        if isinstance(value.op, ast.Sub):
            return a - b
        assert isinstance(value.op, ast.Mult)
        return a * b

    return evaluate(node)


def program(path):
    macros, code, labels = {}, [], {}
    active = None
    for line in path.read_text().splitlines():
        line = line.split('//')[0].strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('.macro '):
            name, args = line[7:].split(maxsplit=1)
            active = (name, operands(args), [])
            continue
        if line == '.endm':
            assert active is not None
            macros[active[0]] = active[1:]
            active = None
            continue
        if active is not None:
            active[2].append(line)
            continue
        parts = line.split(maxsplit=1)
        name, args = parts[0], parts[1] if len(parts) > 1 else ''
        if name in macros:
            parameters, body = macros[name]
            values = operands(args)
            assert len(parameters) == len(values)
            for instruction in body:
                for parameter, value in zip(parameters, values):
                    instruction = instruction.replace('\\' + parameter, value)
                code.append(instruction)
            continue
        if ':' in line:
            label, line = line.split(':', 1)
            assert label not in labels
            labels[label] = len(code) * 4
            line = line.strip()
        if line and not line.startswith('.'):
            code.append(line)
    assert active is None
    return code, labels


class Memory:
    def __init__(self):
        self.regions = []
        self.reads = self.writes = 0

    def add(self, name, data, writable=False):
        data = bytearray(data)
        self.regions.append((BASE[name], data, writable))
        return data

    def access(self, address, size, value=None):
        assert address % size == 0, ('unaligned', hex(address), size)
        for base, data, writable in self.regions:
            off = address - base
            if 0 <= off and off + size <= len(data):
                if value is None:
                    self.reads += 1
                    return int.from_bytes(data[off:off + size], 'little')
                assert writable, ('read-only store', hex(address))
                self.writes += 1
                data[off:off + size] = (value & MASK).to_bytes(size, 'little')
                return None
        raise AssertionError(('unmapped access', hex(address), size, value))


class Machine:
    def __init__(self, code, labels, memory):
        self.code, self.labels, self.mem = code, labels, memory
        self.r = [0] * 16
        self.zero = False
        self.steps = 0

    def value(self, text):
        return integer(text) if text.startswith('#') else self.r[reg(text)]

    def address(self, text):
        a = operands(text.strip('[]'))
        return (self.value(a[0]) + (self.value(a[1]) if len(a) > 1 else 0)) & MASK

    def run(self):
        r, mem = self.r, self.mem
        while r[15] != END:
            assert self.steps < 100000, 'nonterminating assembly'
            assert r[15] % 4 == 0 and 0 <= r[15] // 4 < len(self.code)
            text = self.code[r[15] // 4]
            parts = text.split(maxsplit=1)
            op, tail = parts[0], parts[1] if len(parts) > 1 else ''
            op = op.removesuffix('.w')
            a = operands(tail) if tail else []
            r[15] += 4
            self.steps += 1
            if op in ('ldr', 'ldrh', 'str'):
                address = self.address(a[1])
                if op == 'str':
                    mem.access(address, 4, self.value(a[0]))
                else:
                    r[reg(a[0])] = mem.access(address, 2 if op == 'ldrh' else 4)
                if len(a) == 3:
                    r[reg(operands(a[1].strip('[]'))[0])] += self.value(a[2])
            elif op in ('add', 'sub', 'rsb', 'and', 'orr', 'eor', 'adds', 'subs', 'eors'):
                dest = reg(a[0])
                x = self.value(a[1]) if len(a) > 2 else r[dest]
                y = self.value(a[2]) if len(a) > 2 else self.value(a[1])
                if op in ('add', 'adds'): result = x + y
                elif op in ('sub', 'subs'): result = x - y
                elif op == 'rsb': result = y - x
                elif op == 'and': result = x & y
                elif op == 'orr': result = x | y
                else: result = x ^ y
                r[dest] = result & MASK
                if op in ('adds', 'subs', 'eors'): self.zero = r[dest] == 0
            elif op in ('lsl', 'lsr', 'lsls', 'lsrs'):
                x, shift = self.value(a[1]), self.value(a[2])
                if not a[2].startswith('#'): shift &= 255
                r[reg(a[0])] = ((x << shift if op.startswith('lsl') else x >> shift) & MASK) if shift < 32 else 0
                if op.endswith('s'): self.zero = r[reg(a[0])] == 0
            elif op in ('mov', 'movs'):
                r[reg(a[0])] = self.value(a[1])
            elif op == 'cmp':
                self.zero = self.value(a[0]) == self.value(a[1])
            elif op in ('beq', 'bne', 'b'):
                if op == 'b' or self.zero == (op == 'beq'):
                    r[15] = self.labels[a[0]]
            elif op in ('push', 'pop'):
                registers = reglist(a[0])
                if op == 'push':
                    r[13] -= 4 * len(registers)
                    for i, index in enumerate(registers):
                        mem.access(r[13] + 4 * i, 4, r[index])
                else:
                    values = [mem.access(r[13] + 4 * i, 4) for i in range(len(registers))]
                    r[13] += 4 * len(registers)
                    for index, value in zip(registers, values):
                        r[index] = value & ~1 if index == 15 else value
            elif op == 'bx':
                r[15] = self.value(a[0]) & ~1
            else:
                raise AssertionError(('unsupported actual instruction', text))
        return self.steps


def pack_words(words):
    return struct.pack('<' + 'I' * len(words), *words)


def execute(code, labels, matrix, source, entries):
    mem = Memory()
    output = mem.add('matrix', pack_words([CANARY] + matrix + [CANARY]), True)
    source_bytes = mem.add('source', pack_words(source))
    rows = mem.add('rows', struct.pack('<' + 'H' * len(entries), *(48 * row for row, _, _ in entries)))
    offsets = mem.add('offsets', struct.pack('<' + 'H' * len(entries),
                                         *((offset << 8) | shift for _, offset, shift in entries)))
    stack = mem.add('stack', bytes(256), True)
    machine = Machine(code, labels, mem)
    machine.r = [0x12340000 + i * 0x111 for i in range(16)]
    machine.r[:4] = [BASE['matrix'] + 4, BASE['source'], BASE['rows'], BASE['offsets']]
    if not entries:
        machine.r[:4] = [0] * 4  # count zero must not dereference the data arguments
    machine.r[13:16] = [BASE['stack'] + 248, END | 1, labels['dvbs2_ldpc_xor_entries']]
    mem.access(machine.r[13], 4, len(entries))
    saved, sp = machine.r[4:12].copy(), machine.r[13]
    machine.run()
    assert machine.r[4:12] == saved and machine.r[13] == sp, 'AAPCS preserved registers/stack'
    assert stack[:212] == bytes(212), 'stack write beyond the 36-byte leaf frame'
    assert stack[248:] == struct.pack('<II', len(entries), 0), 'caller stack clobbered'
    assert source_bytes == pack_words(source)
    assert rows == struct.pack('<' + 'H' * len(entries), *(48 * row for row, _, _ in entries))
    assert offsets == struct.pack('<' + 'H' * len(entries),
                                  *((offset << 8) | shift for _, offset, shift in entries))
    expected = matrix.copy()
    for row, offset, shift in entries:
        for k in range(12):
            first = 8 * offset + shift + 32 * k
            value = 0
            for bit in range(first, first + 32):
                value = (value << 1) | ((source[bit // 32] >> (31 - bit % 32)) & 1)
            expected[12 * row + k] ^= value
    assert output == pack_words([CANARY] + expected + [CANARY]), 'row result or canary mismatch'
    return machine.steps


def main():
    code, labels = program(ROOT / 'firmware/src/dvbs2_ldpc_asm.S')
    rng = random.Random(0xC1FEC)
    cases = instructions = 0
    for shift in range(32):
        for offset in range(0, 48, 4):
            matrix = [rng.getrandbits(32) for _ in range(135 * 12)]
            source = [rng.getrandbits(32) for _ in range(25)]
            for count in (1, 2, 7):
                # Repeated rows test dependencies across consecutive entries.
                entries = [(134 if j & 1 else 0, offset, shift) for j in range(count)]
                instructions += execute(code, labels, matrix, source, entries)
                cases += 1
        entries = [(rng.randrange(135), rng.randrange(12) * 4, shift) for _ in range(64)]
        instructions += execute(code, labels, matrix, source, entries)
        cases += 1
    for value in (0, MASK, 0xAAAAAAAA, 0x55555555):
        for shift in range(32):
            instructions += execute(code, labels, [value] * (135 * 12), [value] * 25,
                                    [(row, 4 * (row % 12), shift) for row in range(135)])
            cases += 1
    instructions += execute(code, labels, [0] * 12, [0] * 25, [])
    cases += 1
    print(json.dumps(dict(status='PASS', profile='dvbs2_ldpc_xor_entries', cases=cases,
                          executed_instructions=instructions, shifts=32, source_word_offsets=12,
                          rows=135, stack_frame_bytes=36,
                          model='actual source ISA/AAPCS semantics', silicon_timing=False)))


if __name__ == '__main__':
    main()
