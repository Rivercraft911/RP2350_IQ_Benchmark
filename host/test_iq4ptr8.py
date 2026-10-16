"""Execute B4's exact seven-opcode pointer program and OUT8/DDR schedule.

The model admits arbitrary source starvation and RX backpressure. It proves
address/history ordering, not DMA arbitration or device throughput.
"""
import ctypes
import json
from pathlib import Path
import random
import subprocess
import tempfile

from test_dacclk import Machine, firmware_program
from test_iq4ptr import reference
import fwsrc

ROOT = Path(__file__).resolve().parents[1]


def execute(code, words, previous, base, delayed=False):
    regs = dict(x=sum(((previous >> (30-2*a)) & 3) << (2*a) for a in range(6)),
                y=base >> 16, isr=0, osr=words[0])
    names = {1: 'x', 2: 'y', 3: None, 6: 'isr', 7: 'osr'}
    shifted, in_count, pc, used, cycles, outputs = 0, 0, 0, 1, 0, []
    while len(outputs) < 16*len(words):
        insn = code[pc]
        major, arg, count = insn >> 13, (insn >> 5) & 7, insn & 31
        cycles += 1
        if major == 5:
            regs[names[arg]] = regs[names[insn & 7]]
            if arg == 6:
                in_count = 0
        elif major == 3:
            # A starved OUT cannot use the stale OSR. Arbitrary late arrivals
            # exercise every16-symbol seam without assuming opportunistic refill.
            if shifted == 32:
                if delayed:
                    cycles += 1 + used % 11
                regs['osr'], shifted = words[used], 0
                used += 1
            regs[names[arg]] = regs['osr'] & ((1 << count)-1)
            regs['osr'] >>= count
            shifted += count
        elif major == 2:
            value = regs[names[arg]] if names[arg] else 0
            regs['isr'] = ((regs['isr'] << count) | (value & ((1 << count)-1))) & 0xffffffff
            in_count += count
            if in_count >= 16:
                if delayed:
                    cycles += (len(outputs)*7) % 13
                outputs.append(regs['isr'])
                regs['isr'], in_count = 0, 0
        else:
            raise AssertionError(hex(insn))
        pc = (pc+1) % len(code)
    return outputs, cycles, used


def check_writers():
    for inverted in (False, True):
        machines = []
        for name, pc in (('byte_i_code', 0), ('byte_q_code', 1)):
            code = firmware_program(name)
            assert (code[0] & 0xff) == 8  # OUT PINS,8
            # Machine's scheduler intentionally accepts16-bit OUT only; width
            # does not change instruction cadence or side-set semantics.
            code[0] = (code[0] & ~31) | 16
            machines.append(Machine(code, pc, side_bits=2, optional=True))
        clock = Machine(firmware_program('ddr_clock_code'), int(inverted), side_bits=1)
        for slot in range(16384):
            writes = [(axis, flag) for axis, machine in enumerate(machines)
                      for out, flag in [machine.step()] if out]
            _, level = clock.step()
            assert writes == [(slot & 1, 1-(slot & 1))]
            assert level == (1-(slot & 1)) ^ int(inverted)
    return 32768


def main():
    source = ROOT/'firmware/src'
    with tempfile.TemporaryDirectory(prefix='iq4ptr8-') as name:
        directory = Path(name)
        shim = directory/'shim.c'
        shim.write_text('#include "iq4ptr.h"\n'
                        'const uint16_t *program8(void){return IQ4PTR8_PROGRAM;}\n'
                        'unsigned length8(void){return IQ4PTR8_PROGRAM_LENGTH;}\n'
                        'unsigned cycles8(void){return IQ4PTR8_CYCLES_PER_WORD;}\n')
        library = directory/'pointer.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        *fwsrc.INCLUDES, str(shim), '-o', str(library)], check=True)
        native = ctypes.CDLL(str(library))
        native.program8.restype = ctypes.POINTER(ctypes.c_uint16)
        code = list(native.program8()[:native.length8()])
        assert native.cycles8() == 112 and len(code) == 7
        for key in range(16384):
            previous = sum(((key >> (2+2*age)) & 3) << (30-2*age) for age in range(6))
            for base in range(0x20000000, 0x20080000, 0x10000):
                actual, cycles, _ = execute(code, [key & 3], previous, base)
                assert actual[0] == base + 4*key and cycles == 112
        rng = random.Random(0xb4_2350)
        cases = 0
        for length in (1, 2, 7, 31, 257):
            words = [rng.getrandbits(32) for _ in range(length)]
            for previous in (0, 0xffffffff, 0x935a41e3):
                for base in (0x20000000, 0x20040000, 0x20070000):
                    expected = [base+(p-base)//2 for p in reference(words, previous, base)]
                    for delayed in (False, True):
                        actual, cycles, used = execute(code, words, previous, base, delayed)
                        assert actual == expected and used == length
                        assert cycles >= length*112
                        if not delayed:
                            assert cycles == length*112
                        cases += 1
        words = [rng.getrandbits(32) for _ in range(17)]
        got, _, _ = execute(code, words*5, words[-1], 0x20000000, True)
        assert got == got[:16*17]*5
    print(json.dumps(dict(all_keys=16384, table_bases=8, stream_cases=cases,
                          cyclic_laps=5, pio_cycles_per_symbol=7,
                          writer_clock_slots=check_writers(), device_rate_measured=False)))


if __name__ == '__main__':
    main()
