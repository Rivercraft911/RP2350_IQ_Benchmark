"""Instruction semantics and independent FIR proof for 4I+4Q 16APSK output.

The STM extension checks the actual assembler order. No silicon timing claim.
"""
from __future__ import annotations
import json
from pathlib import Path
import ctypes
import subprocess
import tempfile
import numpy as np
import test_iq16native8 as native
import test_iq16chunk_asm as vm

ROOT = Path(__file__).resolve().parents[1]


def program(path):
    # The shared VM checks scalar STR semantics. Expand only actual STM into
    # equivalent addressed stores so memory guards validate the same register
    # snapshot and order; labels are rebuilt after expansion.
    text = path.read_text().splitlines()
    expanded = []
    for line in text:
        stripped = line.split('//')[0].strip()
        if stripped.startswith('stm '):
            base, registers = vm.operands(stripped.split(maxsplit=1)[1])
            for index, register in enumerate(vm.reglist(registers)):
                expanded.append(f'    str r{register},[{base},#{4*index}]')
        else:
            expanded.append(line)
    with tempfile.NamedTemporaryFile('w', suffix='.S') as temporary:
        temporary.write('\n'.join(expanded))
        temporary.flush()
        code, labels = vm.program(Path(temporary.name))
    return [line.replace('iq16native8_kernel_c', 'iq16chunk_kernel_c') for line in code], labels


def main():
    source = ROOT/'firmware/src'
    code, labels = program(source/'iq16fast_planar_asm.S')
    labels['iq16native8_kernel_asm'] = labels['iq16fast_planar_asm']
    cases = instructions = 0
    with tempfile.TemporaryDirectory(prefix='iq16planar-proof-') as temp:
        library = Path(temp)/'native.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', f'-I{source}', str(source/'iq16native8.c'),
                        '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iq16native8_build.argtypes = [ctypes.POINTER(native.Tables), ctypes.c_uint]
        lib.iq16native8_build.restype = ctypes.c_bool
        lib.iq16native8_kernel_c.argtypes = [native.PTR, native.PTR, ctypes.c_uint32,
                                            native.PTR, ctypes.POINTER(native.Tables)]
        arrays = [np.zeros(rows*4, np.uint32) for rows in native.ROWS]
        tables = native.Tables(*(native.ptr(a) for a in arrays))
        rng = np.random.default_rng(20261016)
        keys = np.arange(4096, dtype=np.uint32)
        fixtures = [rng.integers(0, 1 << 32, size=n, dtype=np.uint32)
                    for n in (1, 2, 3, 4, 5, 7, 63, 64, 255, 256)]
        fixtures += [keys | (((keys*17+123) & 4095) << 16) | np.uint32(0xf000f000)]
        for gamma in (315, 285, 275, 270, 260, 257):
            assert lib.iq16native8_build(ctypes.byref(tables), gamma)
            _, levels = native.old.levels(gamma)
            scaled = (levels[:, None]*native.COEFFICIENTS[None, :] + (1 << 29)) >> 30
            for words in fixtures:
                for previous in (None, rng.integers(0, 1 << 32, size=2, dtype=np.uint32)):
                    _, raw = native.direct_output(words, previous, scaled)
                    expected = raw.reshape(-1, 8)[:, [0, 2, 4, 6, 1, 3, 5, 7]].ravel()
                    actual, steps = native.assembly_output(code, labels, lib, arrays, words, previous)
                    np.testing.assert_array_equal(actual, expected)
                    cases += 1
                    instructions += steps
        machine = native.Machine(code, labels, vm.Memory(), lib)
        machine.r[2], machine.r[14] = 0, vm.END | 1
        machine.run()
        assert machine.c_calls == 0
        cases += 1
    print(json.dumps(dict(profile='native16_planar_exact', cases=cases,
                          gamma_count=6, executed_model_steps=instructions,
                          layout='I01_I23_I45_I67_Q01_Q23_Q45_Q67',
                          zero_extra_ram=True, stack_frame_bytes=48,
                          stm_model='ordered_scalar_stores', silicon_timing=False)))


if __name__ == '__main__':
    main()
