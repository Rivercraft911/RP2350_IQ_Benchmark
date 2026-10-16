"""Execute the actual pvproto_asm.S source against an independent bitwise CRC-8 and byte funnel.

The instruction-semantics model covers all four output phases, counts 1..64, arbitrary
CRC/accumulator states, exact memory bounds, AAPCS registers and stack balance. The same
source is assembled with the firmware toolchain to check that the copied range carries no
relocation or literal-pool load and that the model executed every assembled instruction.
This is ISA semantics, not Arm execution or cycle timing.
"""
from __future__ import annotations

from pathlib import Path
import random
import re
import subprocess
import tempfile

from test_bch128_asm import operands, register, registers

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'firmware/src/ingress/pvproto_asm.S'
TOOLS = Path.home() / '.pico-sdk/toolchain/14_2_Rel1/bin'
MASK, END = 0xFFFFFFFF, 0xEEEEEEE0


def program():
    macros, code, labels = {}, [], {}
    name, params, body = None, [], []

    def emit(line):
        head = line.split(maxsplit=1)[0]
        if head in macros:
            names, lines = macros[head]
            args = operands(line.split(maxsplit=1)[1]) if names else []
            assert len(args) == len(names), line
            for expanded in lines:
                for key, value in zip(names, args):
                    expanded = expanded.replace('\\' + key, value)
                emit(expanded)
        elif line.endswith(':'):
            assert line[:-1] not in labels
            labels[line[:-1]] = len(code)
        elif not line.startswith(('.', '#')):
            code.append(line)

    for raw in SOURCE.read_text().splitlines():
        line = raw.split('//')[0].strip()
        if not line:
            continue
        if line.startswith('.macro '):
            declaration = line.split(maxsplit=2)
            name, body = declaration[1], []
            params = operands(declaration[2]) if len(declaration) > 2 else []
        elif line == '.endm':
            macros[name] = params, body
            name = None
        elif name:
            body.append(line)
        else:
            emit(line)
    return code, labels


def crc8(state, data):
    for byte in data:
        state ^= byte
        for _ in range(8):
            state = ((state << 1) ^ 0xD5) & 0xFF if state & 0x80 else (state << 1) & 0xFF
    return state


def tables():
    t0 = [crc8(0, [x]) for x in range(256)]
    rows = [t0]
    for _ in range(3):
        rows.append([crc8(value, [0]) for value in rows[-1]])
    return bytes(sum(rows, []))


def reference(words, acc, crc, phase):
    data = b''.join(w.to_bytes(4, 'little') for w in words)
    pending = acc.to_bytes(4, 'big')[:phase] + data
    out = [int.from_bytes(pending[4 * i:4 * i + 4], 'big') for i in range(len(words))]
    rest = pending[4 * len(words):]
    new_acc = int.from_bytes(rest + bytes(4 - len(rest)), 'big') if rest else 0
    return out, new_acc, crc8(crc, data)


def execute(code, labels, words, acc, crc, phase, table):
    memory, writable = {}, set()

    def region(base, data, write):
        for i, value in enumerate(data):
            memory[base + i] = value
            if write:
                writable.add(base + i)

    guard = 0xCA85319D
    src, dst, state, tab, stack = 0x100000, 0x200004, 0x300000, 0x400000, 0x500000
    region(src, b''.join(w.to_bytes(4, 'little') for w in words), False)
    region(dst - 4, b''.join(v.to_bytes(4, 'little') for v in [guard] + [0x5A5A5A5A] * len(words) + [guard]), True)
    region(state, b''.join(v.to_bytes(4, 'little') for v in (acc, crc, phase, tab)), True)
    region(tab, table, False)
    region(stack - 256, bytes(256), True)

    def load(address, size):
        assert all(address + k in memory for k in range(size)), ('unmapped load', hex(address))
        return int.from_bytes(bytes(memory[address + k] for k in range(size)), 'little')

    def store(address, value, size=4):
        assert address % 4 == 0 and all(address + k in writable for k in range(size)), ('bad store', hex(address))
        for k in range(size):
            memory[address + k] = (value >> (8 * k)) & 255

    r = [0x1234_0000 + 0x111 * i for i in range(16)]
    r[:4] = [src, dst, len(words), state]
    r[13], r[14], r[15] = stack, END | 1, labels['pv_words_pic']
    saved = r[4:12] + [r[13]]
    flags = dict(z=False)
    executed, steps = set(), 0

    def operand(text):
        if not text.startswith('#'):
            return r[register(text)]
        expression = text[1:]
        assert re.fullmatch(r'[0-9x+\-*() ]+', expression), text     # assembler constant arithmetic only
        return int(eval(expression, {'__builtins__': {}}))

    def address(text):
        parts = operands(text.strip('[]'))
        return r[register(parts[0])] + (operand(parts[1]) if len(parts) > 1 else 0)

    while r[15] != END:
        steps += 1
        assert steps < 100_000
        pc = r[15]
        executed.add(pc)
        op, tail = code[pc].split(maxsplit=1)
        args = operands(tail)
        r[15] += 1
        if op == 'push':
            selected = registers(args[0])
            r[13] -= 4 * len(selected)
            for i, k in enumerate(selected):
                store(r[13] + 4 * i, r[k])
        elif op == 'pop':
            selected = registers(args[0])
            for i, k in enumerate(selected):
                r[k] = load(r[13] + 4 * i, 4)
            r[13] += 4 * len(selected)
            if 15 in selected:
                r[15] &= ~1
        elif op in ('ldr', 'str', 'ldrb'):
            if len(args) == 3:                                   # post-index [rn], #imm
                base = register(args[1].strip('[]'))
                target = r[base]
                r[base] = (r[base] + operand(args[2])) & MASK
            else:
                target = address(args[1])
            if op == 'str':
                store(target, r[register(args[0])])
            else:
                r[register(args[0])] = load(target, 1 if op == 'ldrb' else 4)
        elif op == 'ubfx':
            lsb, width = operand(args[2]), operand(args[3])
            r[register(args[0])] = (r[register(args[1])] >> lsb) & ((1 << width) - 1)
        elif op == 'uxtb':
            r[register(args[0])] = r[register(args[1])] & 255
        elif op == 'rev':
            r[register(args[0])] = int.from_bytes(r[register(args[1])].to_bytes(4, 'little'), 'big')
        elif op in ('eor', 'orr', 'add'):
            second = operand(args[2])
            if len(args) == 4:
                kind, amount = args[3].split()
                assert kind == 'lsr', args
                second >>= int(amount[1:])
            a = r[register(args[1])]
            r[register(args[0])] = (a ^ second if op == 'eor' else a | second if op == 'orr' else a + second) & MASK
        elif op == 'lsl':
            r[register(args[0])] = (r[register(args[1])] << operand(args[2])) & MASK
        elif op in ('subs', 'cmp'):
            value = (r[register(args[1 if op == 'subs' else 0])] - operand(args[-1])) & MASK
            if op == 'subs':
                r[register(args[0])] = value
            flags['z'] = value == 0
        elif op in ('b', 'beq', 'bne'):
            if op == 'b' or flags['z'] == (op == 'beq'):
                r[15] = labels[args[0]]
        else:
            raise AssertionError(('unmodelled instruction', code[pc]))
    assert r[4:12] + [r[13]] == saved, 'callee-saved registers or stack'
    out = [load(dst + 4 * i, 4) for i in range(len(words))]
    assert load(dst - 4, 4) == guard and load(dst + 4 * len(words), 4) == guard
    return out, load(state, 4), load(state + 4, 4), load(state + 8, 4), executed, steps


def assembled():
    """Instructions and relocations of the copied range, from the firmware toolchain."""
    with tempfile.TemporaryDirectory(prefix='pv-words-asm-') as directory:
        obj = Path(directory) / 'pvproto_asm.o'
        subprocess.run([str(TOOLS / 'arm-none-eabi-gcc'), '-mcpu=cortex-m33', '-mthumb',
                        '-march=armv8-m.main+fp+dsp', '-DIQ_ON_DEVICE=1', '-c', str(SOURCE), '-o', str(obj)],
                       check=True)
        disassembly = subprocess.check_output([str(TOOLS / 'arm-none-eabi-objdump'), '-d', '--no-show-raw-insn',
                                               '-j', '.text.pv_words_pic', str(obj)], text=True)
        relocations = subprocess.check_output([str(TOOLS / 'arm-none-eabi-objdump'), '-r',
                                               '-j', '.text.pv_words_pic', str(obj)], text=True)
    lines = [line for line in disassembly.splitlines() if re.match(r'\s+[0-9a-f]+:\s', line)]
    return lines, relocations


def main():
    code, labels = program()
    table = tables()
    rng = random.Random(0x5056)
    covered, cases, steps = set(), 0, 0
    for phase in range(4):
        for count in list(range(1, 9)) + [46, 47, 64]:
            for _ in range(24):
                words = [rng.getrandbits(32) for _ in range(count)]
                acc = (rng.getrandbits(8 * phase) << (32 - 8 * phase)) if phase else 0
                crc = rng.randrange(256)
                out, new_acc, new_crc, new_phase, executed, used = execute(code, labels, words, acc, crc, phase, table)
                want, want_acc, want_crc = reference(words, acc, crc, phase)
                assert (out, new_acc, new_crc, new_phase) == (want, want_acc, want_crc, phase), (phase, count)
                covered |= executed
                cases += 1
                steps += used
    assert covered == set(range(len(code))), sorted(set(range(len(code))) - covered)
    lines, relocations = assembled()
    assert 'R_ARM' not in relocations, relocations
    assert not any(re.search(r'\[pc|\.word|ldr\.?w?\s+\w+, \[pc', line) for line in lines), 'literal pool load'
    assert len(lines) == len(code), (len(lines), len(code))
    print(f'pvproto_asm.S: {cases} cases, {steps} model instructions, all {len(code)} source instructions '
          f'executed and equal to the {len(lines)} assembled ones; no relocation or literal load in the copied range')


if __name__ == '__main__':
    main()
