"""Native and actual-ASM proofs for equal-density32APSK radix17 pairs.

Conversion cost is real and must be timed separately from the shaper. No RF,
DMA deadline, complete-modem or silicon performance claim is made here.
"""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_iq16chunk_asm as vm
import test_iq32native8 as native
import test_iq32native8_asm as asm

ROOT = Path(__file__).resolve().parents[1]


def converted(words):
    first = words & np.uint32(0x001f001f)
    last = (words >> 5) & np.uint32(0x001f001f)
    return first + 17*last + (last << 9)


def c_output(lib, words, previous, tables, partition):
    output = np.full(4*len(words)+2, native.CANARY, np.uint32)
    pilot = converted(np.full(4, native.PILOT, np.uint32))
    all_words = np.concatenate([pilot if previous is None else previous, words])
    for start in range(0, len(words), partition):
        count = min(partition, len(words)-start)
        history = None if start == 0 and previous is None else native.ptr(all_words[start:start+4])
        lib.iq32pairkey_kernel_c(native.ptr(output[1+4*start:]), native.ptr(words[start:]),
                                 count, history, ctypes.byref(tables))
    assert output[0] == output[-1] == native.CANARY
    return output[1:-1]


def main():
    source = ROOT/'firmware/src'
    vm.BASE['tables'] = 0x20040000
    code, labels = vm.program(source/'iq32pairkey_asm.S')
    # BIC x,mask is exact AND x,~mask. The shared32-bit semantics model
    # already validates AND; these actual BIC immediates also assemble on M33.
    code = [line.replace('bic ', 'and ').replace('#0xfe00fe00', '#0x01ff01ff')
            .replace('iq32pairkey_kernel_c', 'iq32native8_kernel_c') for line in code]
    labels['iq32native8_kernel_asm'] = labels['iq32pairkey_kernel_asm']
    rng = np.random.default_rng(2026101501)
    cases = steps = algebra_checks = 0
    records = []
    # Every pair in each axis, including the full crossproduct, proves the
    # first=pair-17*last subtraction has no low-lane borrow into the Q lane.
    pair = np.arange(289, dtype=np.uint32)
    i, q = np.meshgrid(pair, pair)
    raw_pairs = (i % 17 | (i//17) << 5 | (q % 17) << 16 | (q//17) << 21).ravel()
    packed = converted(raw_pairs)
    pair_lanes = packed & np.uint32(0x01ff01ff)
    last_lanes = (packed >> 9) & np.uint32(0x001f001f)
    assert np.array_equal(pair_lanes-17*last_lanes, raw_pairs & np.uint32(0x001f001f))
    algebra_checks += len(raw_pairs)
    # All4913 triple keys, with unrelated upper/lower lanes, prove the paired
    # integer addition/multiply chain and bounds independently of the C leaf.
    k = np.arange(4913, dtype=np.uint32)
    for other in (k[::-1], rng.permutation(k), k):
        a = (k % 17) | ((other % 17) << 16)
        b = (k//17 % 17) | ((other//17 % 17) << 16)
        c = (k//289) | ((other//289) << 16)
        assert np.array_equal(a+17*(b+17*c), k | (other << 16))
        algebra_checks += len(k)
    with tempfile.TemporaryDirectory(prefix='iq32pairkey-proof-') as temp:
        library = Path(temp)/'native.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', f'-I{source}', str(source/'iq32native8.c'),
                        str(source/'iq32pairkey.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iq32native8_build.argtypes = [ctypes.POINTER(native.Tables), ctypes.c_uint]
        lib.iq32native8_build.restype = ctypes.c_bool
        lib.iq32native8_fill.argtypes = [native.PTR, ctypes.c_uint32, native.PTR]
        signature = [native.PTR, native.PTR, ctypes.c_uint32, native.PTR,
                     ctypes.POINTER(native.Tables)]
        lib.iq32pairkey_kernel_c.argtypes = signature
        # The shared assembler model's only C call now invokes the actual new
        # prefix leaf. Instructions, register clobbers and memory guards stay.
        lib.iq32native8_kernel_c = lib.iq32pairkey_kernel_c
        lib.iq32pairkey_convert.argtypes = [native.PTR, native.PTR, ctypes.c_uint32]
        lib.iq32pairkey_convert(None, None, 0)
        lib.iq32pairkey_kernel_c(None, None, 0, None, None)
        guard = np.full(len(raw_pairs)+2, native.CANARY, np.uint32)
        padding = np.uint32(0xfc00fc00)
        dirty = raw_pairs | padding
        lib.iq32pairkey_convert(native.ptr(guard[1:-1]), native.ptr(dirty), len(dirty))
        assert np.array_equal(guard[1:-1], packed)
        assert guard[0] == guard[-1] == native.CANARY
        inplace = dirty.copy()
        lib.iq32pairkey_convert(native.ptr(inplace), native.ptr(inplace), len(inplace))
        assert np.array_equal(inplace, packed)
        fixture = np.empty(8192, np.uint32)
        seed = ctypes.c_uint32(0x1234abcd)
        lib.iq32native8_fill(native.ptr(fixture), len(fixture), ctypes.byref(seed))
        fixtures = []
        for n in (1, 2, 3, 4, 5, 6, 7, 16, 31, 64, 257):
            values = rng.integers(0, 17, (n, 2, 2), dtype=np.uint32)
            fixtures.append(sum(values[:, j, a] << (16*a+5*j)
                                for j in range(2) for a in range(2)))
        fixtures += [fixture, raw_pairs[:4096], raw_pairs[-4096:]]
        arrays = [np.empty(2*native.ROWS, np.uint32) for _ in range(3)]
        tables = native.Tables(*(native.ptr(a) for a in arrays))
        for rate in range(5):
            assert lib.iq32native8_build(ctypes.byref(tables), rate)
            floating, quantized = native.levels(rate)
            scaled = (quantized[:, None]*native.TAPS + (1 << 29)) >> 30
            for raw in fixtures:
                encoded = converted(raw) | np.uint32(0xc000c000)
                prior_values = rng.integers(0, 17, (4, 2, 2), dtype=np.uint32)
                prior = sum(prior_values[:, j, a] << (16*a+5*j)
                            for j in range(2) for a in range(2))
                for original_history in (None, prior):
                    history = None if original_history is None else converted(original_history)
                    expected, _, _ = native.expected(raw, original_history, scaled, floating)
                    for partition in (1, 7, 256, len(raw)):
                        result = c_output(lib, encoded, history, tables, partition)
                        np.testing.assert_array_equal(result, expected)
                        cases += 1
                    actual, instructions = asm.execute(code, labels, lib, arrays, encoded, history)
                    np.testing.assert_array_equal(actual, expected)
                    cases += 1
                    steps += instructions
                    if len(raw) == 8192 and original_history is None:
                        records.append(dict(rate=rate, native_input_crc=native.crc(raw),
                                            pair_input_crc=native.crc(converted(raw)),
                                            raw_crc=native.crc(expected),
                                            packed_crc=native.crc(native.packed(expected))))
            empty, instructions = asm.execute(code, labels, lib, arrays, np.empty(0, np.uint32), None)
            assert not len(empty)
            cases += 1
            steps += instructions
    print(json.dumps(dict(profile='native32_radix17_pair9_last5', cases=cases,
                          algebra_checks=algebra_checks, executed_instructions=steps,
                          input_bytes_per_symbol=2, tables_bytes=117912,
                          stack_frame_bytes=48, silicon_timing=False,
                          long_loop_instructions_per_two_symbols=len(code[labels['5']//4:labels['2']//4]),
                          fixtures=records)))


if __name__ == '__main__':
    main()
