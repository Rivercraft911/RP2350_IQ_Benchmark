"""Check the actual copied C6 mapper source in a bounded Arm ISA model.

The oracle extracts FE column bits individually, uses the DVB-S2 Gray phase
table, adds the Gold quarter-turn and looks up the pi/8 axis levels. It does
not reuse the mapper's bit-plane equations. This is neither Arm execution nor
device timing. No serial device is opened.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile

from test_bch128_asm import operands, register, registers
from test_c4_mapper_pic import tool

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "firmware/src/dvbs2_map8_asm.S"
MASK, END, GUARD = 0xffffffff, 0xeeeeeee0, 0x9ad4c781
# Integer phase index in units of pi/4, label bits read MSB first.
GRAY_PHASE = (1, 0, 4, 5, 2, 7, 3, 6)
# Axis codes are (sign, sign XOR small-magnitude), low bit first.
# Entries are phases pi/8 + k*pi/4, so no floating-point boundary is involved.
AXIS_CODES = ((0, 2), (2, 0), (1, 0), (3, 2),
              (3, 1), (1, 3), (2, 3), (0, 1))


def program():
    """Expand actual assembler macros; refuse unsupported executable syntax."""
    source = [line.split("//")[0].strip() for line in SOURCE.read_text().splitlines()]
    code, labels, macros = [], {}, {}

    def emit(lines):
        cursor = 0
        while cursor < len(lines):
            line = lines[cursor]
            cursor += 1
            if not line:
                continue
            if line.startswith((".macro ", ".irp ")):
                macro = line.startswith(".macro ")
                declaration = line.split(maxsplit=2)
                name = declaration[1].rstrip(",")
                args = operands(declaration[2]) if len(declaration) > 2 else []
                body, end, depth = [], ".endm" if macro else ".endr", 0
                while depth or lines[cursor] != end:
                    item = lines[cursor]
                    if item.startswith((".macro ", ".irp ")):
                        depth += 1
                    elif item in (".endm", ".endr"):
                        depth -= 1
                    body.append(item)
                    cursor += 1
                cursor += 1
                if macro:
                    macros[name] = args, body
                else:
                    for value in args:
                        emit([item.replace("\\" + name, value) for item in body])
            elif line.endswith(":"):
                assert line[:-1] not in labels, line
                labels[line[:-1]] = len(code)
            elif line.split()[0] in macros:
                name, *tail = line.split(maxsplit=1)
                names, body = macros[name]
                args = operands(tail[0]) if tail else []
                assert len(names) == len(args), line
                for item in body:
                    for key, value in zip(names, args):
                        item = item.replace("\\" + key, value)
                    emit([item])
            elif not line.startswith((".", "#")):
                code.append(line)
    emit(source)
    return code, labels


class Memory:
    def __init__(self):
        self.regions = []
        self.loads = self.stores = 0

    def add(self, base, values, size=4, writable=False):
        data = bytearray(b"".join(value.to_bytes(size, "little") for value in values))
        self.regions.append((base, data, writable))
        return data

    def access(self, address, size=4, value=None):
        assert address % size == 0, ("unaligned", hex(address), size)
        for base, data, writable in self.regions:
            offset = address - base
            if 0 <= offset <= len(data) - size:
                if value is None:
                    self.loads += 1
                else:
                    assert writable, ("read-only write", hex(address))
                    self.stores += 1
                    data[offset:offset+size] = (value & ((1 << (8*size))-1)).to_bytes(size, "little")
                return int.from_bytes(data[offset:offset+size], "little")
        raise AssertionError(("out-of-bounds access", hex(address), size))


def execute(code, labels, columns, gold, fill, n, mask, carries, initial):
    """Model the leaf's positive-count, even-fill, contiguous-output contract."""
    count = len(gold)
    assert count >= 0 and all(len(x) == count for x in columns)
    assert count == 0 or fill in range(0, 16, 2)
    assert count <= (mask - (n & mask) + 1)//2
    mem = Memory()
    input_bytes = [mem.add(base, words) for base, words in
                   zip((0x100000, 0x200000, 0x300000), columns)]
    gold_bytes = mem.add(0x400000, [x for g in gold for x in g])
    table = [sum(((byte >> bit) & 1) << (2*bit) for bit in range(8)) for byte in range(256)]
    table_bytes = mem.add(0x500000, table, 2)
    i_bytes = mem.add(0x600000, [GUARD, *initial[0], GUARD], writable=True)
    q_bytes = mem.add(0x700000, [GUARD, *initial[1], GUARD], writable=True)
    stream = [0x600004, 0x700004, mask, n, *carries, fill,
              0x00000101, 0xc631aaa1, 0xc631aaa2]
    stream_bytes = mem.add(0x800000, [GUARD, *stream, GUARD], writable=True)
    stack_bytes = mem.add(0x900000, [0xa5] * 512, 1, writable=True)
    r = [0x12340000 + i*0x111 for i in range(16)]
    r[:4] = [0x100000, 0x200000, 0x300000, 0x400000]
    entry = "dvbs2_map8_c6"
    r[13:] = [0x900180, END | 1, labels[entry]]
    for offset, value in enumerate((0x500000, 0x800004, count)):
        mem.access(r[13] + 4*offset, value=value)
    saved, sp, steps, zero, borrow = r[4:12].copy(), r[13], 0, False, False
    lowest_sp = sp

    def value(arg):
        if not arg.startswith("#"):
            k = register(arg)
            assert k >= 0, arg
            return r[k]
        literal = arg[1:].strip("()")
        assert re.fullmatch(r"(?:0x[0-9a-fA-F]+|\d+)(?:[-+](?:0x[0-9a-fA-F]+|\d+))*", literal), literal
        return sum(int(x, 0) for x in re.findall(r"[-+]?(?:0x[0-9a-fA-F]+|\d+)", literal))

    def shifted(x, description):
        op, amount = description.split(maxsplit=1)
        shift = value(amount)
        if not amount.startswith("#"):
            shift &= 255
        if op == "lsl":
            return (x << shift) & MASK if shift < 32 else 0
        if op == "lsr":
            return x >> shift if shift < 32 else 0
        assert op == "ror", description
        shift &= 31
        return ((x >> shift) | (x << ((-shift) & 31))) & MASK

    while r[15] != END:
        assert steps < 200000, "nontermination"
        assert 0 <= r[15] < len(code), ("branch escaped copied section", r[15])
        line = code[r[15]]
        r[15] += 1
        steps += 1
        op, *tail = line.split(maxsplit=1)
        args = operands(tail[0]) if tail else []
        op = re.sub(r"\.[nw]$", "", op)
        if op in ("push", "pop"):
            selected = registers(args[0])
            if op == "push":
                r[13] -= 4*len(selected)
                for i, k in enumerate(selected):
                    mem.access(r[13] + i*4, value=r[k])
            else:
                for i, k in enumerate(selected):
                    r[k] = mem.access(r[13] + i*4)
                    if k == 15:
                        r[k] &= ~1
                r[13] += 4*len(selected)
        elif op in ("ldr", "ldrh", "ldrb", "str", "strh", "strb"):
            address = operands(args[1].strip("[]"))
            base = register(address[0])
            offset = value(address[1]) if len(address) > 1 else 0
            if len(address) == 3:
                offset = shifted(offset, address[2])
            at = (r[base] + offset) & MASK
            size = 1 if op.endswith("b") else 2 if op.endswith("h") else 4
            if op.startswith("str"):
                mem.access(at, size, value(args[0]))
            else:
                r[register(args[0])] = mem.access(at, size)
            if len(args) == 3:
                r[base] = (r[base] + value(args[2])) & MASK
        elif op in ("ldm", "ldmia", "stm", "stmia"):
            base = register(args[0].rstrip("!"))
            selected, start = registers(args[1]), r[base]
            for i, k in enumerate(selected):
                if op.startswith("ldm"):
                    r[k] = mem.access(start + 4*i)
                else:
                    mem.access(start + 4*i, value=r[k])
            if args[0].endswith("!"):
                r[base] = start + 4*len(selected)
        elif op in ("mov", "movs", "movw", "movt", "mvn", "uxtb", "uxth", "rbit", "rev", "ubfx", "bfi"):
            dest, x = register(args[0]), value(args[1])
            if op == "movt":
                result = (r[dest] & 0xffff) | (x << 16)
            elif op == "mvn":
                result = ~x
            elif op == "uxtb":
                result = shifted(x, args[2]) & 255 if len(args) == 3 else x & 255
            elif op == "uxth":
                result = x & 65535
            elif op == "rbit":
                result = int(f"{x:032b}"[::-1], 2)
            elif op == "rev":
                result = int.from_bytes(x.to_bytes(4, "little"), "big")
            elif op in ("ubfx", "bfi"):
                shift, width = value(args[2]), value(args[3])
                bits = (1 << width) - 1
                result = (x >> shift) & bits if op == "ubfx" else (r[dest] & ~(bits << shift)) | ((x & bits) << shift)
            else:
                result = x
            r[dest] = result & MASK
            if op == "movs":
                zero = r[dest] == 0
        elif op in ("add", "adds", "sub", "subs", "rsb", "and", "ands", "orr", "orrs", "eor", "eors", "bic", "bics", "orn", "lsl", "lsls", "lsr", "lsrs", "ror", "rors", "mul"):
            dest = register(args[0])
            x, y = (r[dest], value(args[1])) if len(args) == 2 else (value(args[1]), value(args[2]))
            if len(args) == 4:
                y = shifted(y, args[3])
            plain = op.rstrip("s")
            if plain in ("lsl", "lsr", "ror"):
                amount = args[1] if len(args) == 2 else args[2]
                result = shifted(x, plain + " " + amount)
            elif plain == "add": result = x + y
            elif plain == "sub": result = x - y
            elif plain == "rsb": result = y - x
            elif plain == "and": result = x & y
            elif plain == "orr": result = x | y
            elif plain == "eor": result = x ^ y
            elif plain == "bic": result = x & ~y
            elif plain == "orn": result = x | ~y
            else: result = x * y
            r[dest] = result & MASK
            if op.endswith("s"):
                zero, borrow = r[dest] == 0, x < y
        elif op == "cmp":
            x, y = value(args[0]), value(args[1])
            zero, borrow = x == y, x < y
        elif op in ("b", "beq", "bne", "bhs", "blo"):
            take = op == "b" or op == "beq" and zero or op == "bne" and not zero or op == "bhs" and not borrow or op == "blo" and borrow
            if take:
                r[15] = labels[args[0]]
        elif op == "bx":
            assert args == ["lr"]
            r[15] = r[14] & ~1
        else:
            raise AssertionError(("unsupported actual instruction", line))
        lowest_sp = min(lowest_sp, r[13])
    assert r[4:12] == saved and r[13] == sp, "AAPCS register/stack damage"
    assert stack_bytes[:lowest_sp - 0x900000] == bytes([0xa5]) * (lowest_sp - 0x900000)
    assert stack_bytes[sp-0x900000:sp-0x900000+12] == b"".join(x.to_bytes(4, "little") for x in (0x500000, 0x800004, count)), "stack argument damage"
    assert [bytes(x) for x in input_bytes] == [b"".join(w.to_bytes(4, "little") for w in c) for c in columns]
    assert gold_bytes == b"".join(w.to_bytes(4, "little") for g in gold for w in g)
    assert table_bytes == b"".join(w.to_bytes(2, "little") for w in table)
    words = lambda raw: [int.from_bytes(raw[k:k+4], "little") for k in range(0, len(raw), 4)]
    assert all(words(raw)[0] == words(raw)[-1] == GUARD for raw in (i_bytes, q_bytes, stream_bytes))
    actual_stream = words(stream_bytes)[1:-1]
    assert actual_stream[:3] == stream[:3] and actual_stream[6:] == stream[6:], "stream metadata changed"
    output = words(i_bytes)[1:-1], words(q_bytes)[1:-1]
    return output, tuple(actual_stream[4:6]), actual_stream[3], steps, sp-lowest_sp


def reference(columns, gold, fill, n, mask, carries, initial):
    out = [list(initial[0]), list(initial[1])]
    carry = list(carries)
    position = fill
    for word, rotation in enumerate(gold):
        for bit in range(32):
            label = sum(((column[word] >> (31-bit)) & 1) << (2-k) for k, column in enumerate(columns))
            quarter_turn = ((rotation[0] >> bit) & 1) + 2*((rotation[1] >> bit) & 1)
            codes = AXIS_CODES[(GRAY_PHASE[label] + 2*quarter_turn) & 7]
            for axis in (0, 1):
                carry[axis] |= codes[axis] << (2*position)
            position += 1
            if position == 16:
                for axis in (0, 1):
                    out[axis][n & mask] = carry[axis]
                    carry[axis] = 0
                position, n = 0, (n+1) & MASK
    assert position == fill
    return tuple(out), tuple(carry), n


def inspect():
    cc, dump = tool("arm-none-eabi-gcc"), tool("arm-none-eabi-objdump")
    assert cc and dump, "ARM GCC/objdump required; this check never accesses hardware"
    with tempfile.TemporaryDirectory(prefix="c6-map8-asm-") as temp:
        obj = Path(temp) / "map.o"
        subprocess.run([cc, "-Wall", "-Wextra", "-Werror", "-mcpu=cortex-m33", "-mthumb",
                        "-DIQ_ON_DEVICE=1", "-c", str(SOURCE), "-o", str(obj)], check=True)
        headers = subprocess.check_output([dump, "-h", str(obj)], text=True)
        match = re.search(r"\s+(c6_mapper)\s+([0-9a-f]+)\s", headers)
        assert match, headers
        section, size = match[1], int(match[2], 16)
        assert 0 < size <= 3072, f"{size} bytes exceeds copied-leaf arena"
        relocations = subprocess.check_output([dump, "-r", "-j", section, str(obj)], text=True)
        assert "R_ARM_" not in relocations, relocations
        disassembly = subprocess.check_output([dump, "-d", "-j", section, str(obj)], text=True)
        assert re.search(r"^00000000 <dvbs2_map8_c6>:$", disassembly, re.MULTILINE), "copied entry must begin the section"
        instructions, backwards = [], []
        for line in disassembly.splitlines():
            match = re.match(r"\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s+)+\s*(\S+)\s*(.*)", line)
            if not match:
                continue
            address, op, args = int(match[1], 16), match[2], match[3]
            instructions.append((address, op, args))
            assert op not in ("bl", "bl.w", "blx"), line
            assert not re.search(r"\[pc\b", args) and op not in ("adr", "adr.w"), line
            if re.fullmatch(r"b(?:eq|ne|cs|cc|hs|lo|mi|pl|vs|vc|hi|ls|ge|lt|gt|le)?(?:\.[nw])?", op):
                target = int(args.split()[0], 16)
                assert 0 <= target < size, line
                if target < address:
                    backwards.append((target, address))
        assert backwards, "no counted mapper loop"
        loops = []
        for start, end in backwards:
            loop = [(op, args) for address, op, args in instructions if start <= address <= end]
            histogram = Counter(op for op, _ in loop)
            loops.append(dict(start=start, instructions=len(loop),
                              loads=sum(op.startswith("ldr") for op, _ in loop),
                              stores=sum(op.startswith("str") for op, _ in loop),
                              histogram=dict(sorted(histogram.items()))))
        return dict(section_bytes=size, loops=loops)


def main():
    code, labels = program()
    rng, cases, instructions, stack = random.Random(0xc62350), 0, 0, 0
    leaf_calls = crossing_pairs = 0

    def check(columns, gold, fill, mask, n=None, chunks=None):
        nonlocal cases, instructions, stack, leaf_calls, crossing_pairs
        if n is None:
            n = 3 if mask == MASK else (MASK-2 if cases & 1 else mask)
        size = n+2*len(gold)+1 if mask == MASK else mask+1
        initial = tuple([rng.getrandbits(32) for _ in range(size)] for _ in range(2))
        carries = tuple(rng.getrandbits(2*fill) for _ in range(2))
        expected = reference(columns, gold, fill, n, mask, carries, initial)
        actual, word, state = None, 0, (initial, carries, n)
        # Model the production wrapper's run clamp. The one input word whose
        # output pair crosses the ring uses C4; the native complete-frame test
        # checks that C path against its independent Python PL reference.
        # Each other piece runs these actual assembly-source instructions.
        while word < len(gold):
            output, carry, position = state
            capacity = (mask - (position & mask) + 1)//2
            budget = chunks[word % len(chunks)] if chunks else len(gold)-word
            count = min(capacity, len(gold)-word, budget) if capacity else 1
            sub_columns = [column[word:word+count] for column in columns]
            sub_gold = gold[word:word+count]
            if capacity:
                actual = execute(code, labels, sub_columns, sub_gold, fill, position, mask, carry, output)
                state = actual[:3]
                instructions += actual[3]
                stack = max(stack, actual[4])
                leaf_calls += 1
            else:
                state = reference(sub_columns, sub_gold, fill, position, mask, carry, output)
                crossing_pairs += 1
            word += count
        assert state == expected, (fill, mask, n, len(gold))
        cases += 1
        return expected

    # One 32-symbol word enumerates every label x Gold-rotation combination.
    exhaustive_columns = [sum(((bit & 7) >> (2-axis) & 1) << (31-bit) for bit in range(32)) for axis in range(3)]
    gold = tuple(sum(((bit >> 3) >> axis & 1) << bit for bit in range(32)) for axis in range(2))
    for fill in range(0, 16, 2):
        for mask in (0, 1, 3, 15, MASK):
            check([[word] for word in exhaustive_columns], [gold], fill, mask)
        for count in (2, 3, 45):
            columns = [[rng.getrandbits(32) for _ in range(count)] for _ in range(3)]
            rotations = [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(count)]
            for mask in (1, 7, MASK):
                check(columns, rotations, fill, mask)
                check(columns[::-1], rotations, fill, mask)
            # Split the same chronological stream at varying call boundaries.
            check(columns, rotations, fill, 15, n=MASK-1, chunks=(1, 2, 7))
            check(columns, rotations, fill, MASK, n=0, chunks=(1, 3, 2))
    for pattern in (0, MASK, 0xaaaaaaaa, 0x55555555):
        check([[pattern]*675 for _ in range(3)], [(pattern, pattern)]*675, 2*(cases % 8), 2047, n=0)
    # The caller skips zero runs. The assembly's defensive zero branch must
    # still return with all state intact, including an odd fill it never maps.
    for fill in (0, 1, 14, 15):
        initial = ([rng.getrandbits(32) for _ in range(4)], [rng.getrandbits(32) for _ in range(4)])
        carries = tuple(rng.getrandbits(2*fill) for _ in range(2))
        actual = execute(code, labels, [[], [], []], [], fill, 3, 3, carries, initial)
        assert actual[:3] == (initial, carries, 3)
        cases += 1
        instructions += actual[3]
        stack = max(stack, actual[4])
    summary = inspect()
    summary.update(result="PASS", scope="actual assembly source ISA/AAPCS model; no device timing",
                   cases=cases, leaf_calls=leaf_calls, interpreted_instructions=instructions,
                   stack_bytes=stack, even_fills=list(range(0, 16, 2)),
                   ring_masks=[0, 1, 3, 7, 15, 2047, MASK],
                   crossing_pairs_modeled=crossing_pairs,
                   leaf_contract="positive count, even fill, contiguous output pair runs",
                   fallbacks="C4 odd fills/crossing pairs covered by native full-PL tests")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
