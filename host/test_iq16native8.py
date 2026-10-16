"""Exact independent FIR and instruction checks for native L8 16APSK chunks.

Compiles the real C in a temporary directory, checks every partial row/all six
radius ratios, native-coordinate/history/partition boundaries and rounding bounds.
The shared checked ISA model runs the actual assembler source; this does not
execute Arm silicon or qualify DMA throughput, the AFE or the sampling images.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import re
import tempfile
import zlib
import numpy as np

import test_iq16chunk as old
import test_iq16chunk_asm as vm
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
PTR = old.PTR
ROWS = (512, 4096, 4096)
PARTS = ((1, 3), (4, 4), (8, 4))
COEFFICIENTS = np.array([6658, -48202, 6455, 124501, -58274, -258197, 258053, 1108522,
                       1108522, 258053, -258197, -58274, 124501, 6455, -48202, 6658], np.int64)
COEFFICIENT_SHA = '16936cb50f5de62e68b10367624b868afea4ad49f8dfc4481526de4712fda5ae'


class Tables(ctypes.Structure):
    _fields_ = [(name, PTR) for name in ('old', 'middle', 'future')]


def ptr(array):
    return array.ctypes.data_as(PTR)


def direct_rows(offset, count, scaled):
    keys = np.arange(8**count, dtype=np.int64)
    codes = (keys[:, None] >> (3*np.arange(count))) & 7
    output = np.zeros((len(keys), 8), np.int64)
    for symbol in range(4):
        for phase in range(2):
            for j in range(count):
                age = 8 + symbol - (offset + j)
                if 0 <= age < 8:
                    output[:, 2*symbol + phase] += scaled[codes[:, j], 2*age + phase]
    return ((output + 128) >> 8).astype('<i2')


def direct_output(words, previous, scaled, parts=PARTS):
    axes = []
    for axis in (0, 1):
        history = np.zeros(2, np.uint32) if previous is None else previous
        data = np.concatenate([history, words])
        codes = ((data[:, None] >> (16*axis + 3*np.arange(4))) & 7).ravel()
        windows = np.lib.stride_tricks.sliding_window_view(codes, 12)[::4]
        output = np.zeros((len(words), 8), np.int64)
        for offset, count in parts:
            partial = np.zeros_like(output)
            for symbol in range(4):
                for phase in range(2):
                    for j in range(count):
                        age = 8 + symbol - (offset + j)
                        if 0 <= age < 8:
                            partial[:, 2*symbol + phase] += scaled[windows[:, offset+j], 2*age+phase]
            output += (partial + 128) >> 8
        axes.append(output.ravel())
    i, q = [axis.astype(np.uint16).astype(np.uint32) for axis in axes]
    raw = np.stack([i[::2] | i[1::2] << 16, q[::2] | q[1::2] << 16], axis=1).ravel()
    return axes, raw


def native_output(lib, words, previous, tables, partition):
    output = np.full(len(words)*8 + 2, old.CANARY, np.uint32)
    for start in range(0, len(words), partition):
        count = min(partition, len(words)-start)
        if start == 0:
            history = previous
        elif start == 1:
            history = np.array([0 if previous is None else previous[-1], words[0]], np.uint32)
        else:
            history = words[start-2:start]
        lib.iq16native8_kernel_c(ptr(output[1+8*start:]), ptr(words[start:]), count,
                                 None if history is None else ptr(history), ctypes.byref(tables))
    assert output[0] == output[-1] == old.CANARY
    return output[1:-1].copy()


class Machine(vm.Machine):
    def c_leaf(self):
        r, mem = self.r, self.mem
        assert r[13] % 8 == 0, 'AAPCS stack alignment at C prefix'
        tables = mem.read(r[13])
        bases = [mem.read(tables+4*i) for i in range(3)]
        native = Tables(*(mem.ptr(base, rows*16) for base, rows in zip(bases, ROWS)))
        self.lib.iq16native8_kernel_c(mem.ptr(r[0], r[2]*32), mem.ptr(r[1], r[2]*4), r[2],
                                      mem.ptr(r[3], 8) if r[3] else None, ctypes.byref(native))
        return_pc = r[14] & ~1
        for i in (0, 1, 2, 3, 12):
            r[i] = 0xdec00000+i
        r[14], r[15] = 0xdec0000e, return_pc
        self.zero = self.borrow = False
        self.c_calls += 1


def assembly_output(code, labels, lib, tables, words, previous):
    # Separate non-overlapping modeled regions for 8/64/64KiB; names are the
    # shared VM's memory-slot names, not assumptions about physical placement.
    vm.BASE['tables'] = 0x20040000
    mem = vm.Memory()
    for name, data in zip(('edge', 'middle', 'reverse'), tables):
        mem.add(name, data.tobytes())
    mem.add('tables', np.array([vm.BASE[n] for n in ('edge', 'middle', 'reverse')], '<u4').tobytes())
    source = mem.add('input', words.astype('<u4').tobytes())
    before = np.zeros(2, np.uint32) if previous is None else previous
    history = mem.add('previous', before.astype('<u4').tobytes())
    output = mem.add('output', np.full(len(words)*8+2, old.CANARY, '<u4').tobytes(), True)
    stack = mem.add('stack', bytes(4096), True)
    machine = Machine(code, labels, mem, lib)
    machine.r = [0x12340000+i*0x111 for i in range(16)]
    machine.r[:4] = [vm.BASE['output']+4, vm.BASE['input'], len(words),
                     vm.BASE['previous'] if previous is not None else 0]
    machine.r[13:16] = [vm.BASE['stack']+4088, vm.END | 1, labels['iq16native8_kernel_asm']]
    mem.write(machine.r[13], vm.BASE['tables'])
    saved, sp = machine.r[4:12].copy(), machine.r[13]
    machine.run()
    assert machine.r[4:12] == saved and machine.r[13] == sp
    assert source == words.astype('<u4').tobytes() and history == before.astype('<u4').tobytes()
    assert stack[:4000] == bytes(4000)
    result = np.frombuffer(output, '<u4')
    assert result[0] == result[-1] == old.CANARY
    assert machine.c_calls == 1
    return result[1:-1].copy(), machine.steps


def main():
    source = ROOT/'firmware/src'
    assert hashlib.sha256(COEFFICIENTS.astype('<i4').tobytes()).hexdigest() == COEFFICIENT_SHA
    header = (source / 'apsk/iq16native8.h').read_text()
    def constant(name):
        return int(re.search(r'#define ' + name + r' (\d+)u', header)[1])
    assert COEFFICIENT_SHA in header
    code, labels = vm.program(source / 'apsk/iq16native8_asm.S')
    # Only the native-C hook symbol is translated; instructions/labels remain
    # those expanded from the real new assembly file by the shared interpreter.
    code = [line.replace('iq16native8_kernel_c', 'iq16chunk_kernel_c') for line in code]
    cases = instructions = asm_cases = 0
    records = []
    with tempfile.TemporaryDirectory(prefix='iq16native8-native-') as temp:
        library = Path(temp)/'native8.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        *fwsrc.INCLUDES, str(source / 'apsk/iq16native8.c'), str(source / 'apsk/iq16chunk.c'),
                        '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iq16native8_build.argtypes = [ctypes.POINTER(Tables), ctypes.c_uint]
        lib.iq16native8_build.restype = ctypes.c_bool
        lib.iq16native8_coefficients.restype = ctypes.POINTER(ctypes.c_int32)
        lib.iq16native8_kernel_c.argtypes = [PTR, PTR, ctypes.c_uint32, PTR, ctypes.POINTER(Tables)]
        lib.iq16chunk_fill.argtypes = [PTR, ctypes.c_uint32, PTR]
        np.testing.assert_array_equal(np.ctypeslib.as_array(lib.iq16native8_coefficients(), shape=(16,)), COEFFICIENTS)
        guarded = [np.full(rows*4+2, old.CANARY, np.uint32) for rows in ROWS]
        arrays = [a[1:-1] for a in guarded]
        tables = Tables(*(ptr(a) for a in arrays))
        for bad in (0, 271, 0xffffffff):
            assert not lib.iq16native8_build(ctypes.byref(tables), bad)
        assert not lib.iq16native8_build(None, 270)
        assert all(np.all(a == old.CANARY) for a in guarded)
        rng = np.random.default_rng(20261015)
        keys = np.arange(4096, dtype=np.uint32)
        all_keys = keys | (((keys*17+123) & 4095) << 16) | np.uint32(0xf000f000)
        for gamma in (315, 285, 275, 270, 260, 257):
            assert lib.iq16native8_build(ctypes.byref(tables), gamma)
            _, q30 = old.levels(gamma)
            scaled = (q30[:, None]*COEFFICIENTS[None, :] + (1 << 29)) >> 30
            for array, (offset, count) in zip(arrays, PARTS):
                np.testing.assert_array_equal(array.view('<i2').reshape(-1, 8), direct_rows(offset, count, scaled))
            bounds = sum(abs(a.view('<i2').astype(np.int64).reshape(-1, 8)).max(axis=0) for a in arrays)
            assert bounds.max() <= 7052
            fixture = np.zeros(8192, np.uint32)
            seed = ctypes.c_uint32(0x1234abcd)
            lib.iq16chunk_fill(ptr(fixture), len(fixture), ctypes.byref(seed))
            assert zlib.crc32(fixture.astype('<u4').tobytes()) == 787286031
            fixtures = [rng.integers(0, 1 << 32, size=n, dtype=np.uint32) for n in (1, 2, 3, 7, 64, 256)]
            fixtures += [all_keys, fixture]
            for index, words in enumerate(fixtures):
                previous = None if index & 1 else rng.integers(0, 1 << 32, size=2, dtype=np.uint32)
                axes, expected = direct_output(words, previous, scaled)
                symmetric, _ = direct_output(words, previous, scaled, ((1, 4), (5, 3), (8, 4)))
                # Moving one coordinate between rounded partials changes only a
                # rounding carry: floor(A)+floor(B+T)-floor(A+T)-floor(B) in[-1,1].
                difference = np.concatenate(axes)-np.concatenate(symmetric)
                assert abs(difference).max() <= 1
                for partition in (1, 7, 256, len(words)):
                    np.testing.assert_array_equal(native_output(lib, words, previous, tables, partition), expected)
                    cases += 1
                actual, steps = assembly_output(code, labels, lib, arrays, words, previous)
                np.testing.assert_array_equal(actual, expected)
                asm_cases += 1; instructions += steps
                if index == len(fixtures)-1:
                    packed = (axes[0] & 0x3fff) | (axes[1] & 0x3fff) << 16
                    record = dict(gamma100=gamma, table_crc=[zlib.crc32(a.astype('<u4').tobytes()) for a in arrays],
                                  raw_crc=zlib.crc32(expected.astype('<u4').tobytes()),
                                  packed_crc=zlib.crc32(packed.astype('<u4').tobytes()),
                                  worst_history_sum_bound_lsb=int(bounds.max()),
                                  symmetric_rms_axis_difference_lsb=float(np.sqrt(np.mean(difference**2))))
                    if gamma == 270:
                        assert record['table_crc'] == [constant('IQ16NATIVE8_'+name+'_CRC')
                                                        for name in ('OLD', 'MIDDLE', 'FUTURE')]
                        assert record['raw_crc'] == constant('IQ16NATIVE8_FIXTURE_RAW_PAIRS_CRC')
                        assert record['packed_crc'] == constant('IQ16NATIVE8_FIXTURE_PACKED_CRC')
                    records.append(record)
            assert all(a[0] == a[-1] == old.CANARY for a in guarded)
        lib.iq16native8_kernel_c(None, None, 0, None, None)
        machine = Machine(code, labels, vm.Memory(), lib)
        machine.r[2], machine.r[14] = 0, vm.END | 1
        machine.run(); assert machine.c_calls == 0
    print(json.dumps(dict(profile='L8_native_16APSK_3_4_4', bytes=139264, reverse_bytes=0,
                          partial_rows_per_gamma=sum(ROWS), native_partition_cases=cases,
                          assembly_model_cases=asm_cases+1, executed_instructions=instructions,
                          coefficient_sha256=COEFFICIENT_SHA, stack_frame_bytes=48,
                          silicon_execution=False, fixtures=records)))

if __name__ == '__main__':
    main()
