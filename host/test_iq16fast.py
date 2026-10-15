"""Exact actual-assembler/AAPCS proofs for the sparse 16APSK schedules.

This executes the checked instruction semantics, not Arm timing. The existing
independent native-L8 FIR test establishes the shared tables and quantization.
"""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_iq16native8 as native
import test_iq16chunk_asm as vm

ROOT = Path(__file__).resolve().parents[1]


def program(path):
    body, code, labels = [], [], {}
    in_macro = False
    for line in path.read_text().splitlines():
        line = line.split('//')[0].strip()
        if not line:
            continue
        if line.startswith('.macro'):
            assert line == '.macro AXIS shift, offset, source'
            in_macro = True
            continue
        if line == '.endm':
            in_macro = False
            continue
        if in_macro:
            body.append(line)
            continue
        if line.startswith('AXIS '):
            shift, offset, source = line.split()[1].split(',')
            code.extend(s.replace('\\shift', shift).replace('\\offset', offset)
                        .replace('\\source', source) for s in body)
            continue
        if ':' in line:
            label, line = line.split(':', 1)
            assert label not in labels
            labels[label] = len(code)*4
            line = line.strip()
        if line and not line.startswith('.'):
            code.append(line.replace('iq16native8_kernel_c', 'iq16chunk_kernel_c'))
    return code, labels


def main():
    source = ROOT/'firmware/src'
    code, labels = program(source/'iq16fast_asm.S')
    baseline, baseline_labels = vm.program(source/'iq16native8_asm.S')
    baseline = [s.replace('iq16native8_kernel_c', 'iq16chunk_kernel_c') for s in baseline]
    count = instructions = 0
    steady = {}
    with tempfile.TemporaryDirectory(prefix='iq16fast-proof-') as temp:
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
        rng = np.random.default_rng(20261015)
        keys = np.arange(4096, dtype=np.uint32)
        fixtures = [rng.integers(0, 1 << 32, size=n, dtype=np.uint32)
                    for n in (1, 2, 3, 4, 5, 6, 7, 8, 63, 64, 65, 255, 256, 257)]
        fixtures += [keys | (((keys*17+123) & 4095) << 16) | np.uint32(0xf000f000)]
        for gamma in (315, 285, 275, 270, 260, 257):
            assert lib.iq16native8_build(ctypes.byref(tables), gamma)
            # The omitted row word is not an approximation or a zero-tap claim:
            # all three old symbols are outside support at output symbol three.
            assert np.all(arrays[0].reshape(-1, 4)[:, 3] == 0)
            _, levels = native.old.levels(gamma)
            scaled = (levels[:, None]*native.COEFFICIENTS[None, :] + (1 << 29)) >> 30
            for words in fixtures:
                for previous in (None, rng.integers(0, 1 << 32, size=2, dtype=np.uint32)):
                    _, expected = native.direct_output(words, previous, scaled)
                    for kernel in ('sparse', 'pair'):
                        entry = dict(labels, iq16native8_kernel_asm=labels[f'iq16fast_{kernel}_asm'])
                        actual, steps = native.assembly_output(code, entry, lib, arrays, words, previous)
                        np.testing.assert_array_equal(actual, expected)
                        count += 1
                        instructions += steps
                        if gamma == 270 and len(words) == 256 and previous is None:
                            steady[kernel] = steps
            # Zero blocks must not dereference any pointer, including tables.
            for kernel in ('sparse', 'pair'):
                machine = native.Machine(code, labels, vm.Memory(), lib)
                machine.r[2], machine.r[14] = 0, vm.END | 1
                machine.r[15] = labels[f'iq16fast_{kernel}_asm']
                machine.run()
                assert machine.c_calls == 0
                count += 1
        actual, steps = native.assembly_output(baseline, baseline_labels, lib, arrays,
                                              fixtures[12], None)
        steady['baseline'] = steps
    print(json.dumps(dict(profile='native16_sparse_exact', gamma_count=6, cases=count,
                          executed_instructions=instructions, extra_ram_bytes=0,
                          stack_frame_bytes=48, timed=False,
                          modeled_instructions_for_256_words=steady)))


if __name__ == '__main__':
    main()
