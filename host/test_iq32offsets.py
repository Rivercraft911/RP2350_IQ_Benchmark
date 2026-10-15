"""Exact byte-offset32APSK C/ASM, partitions, and direct pair-mapper proof.

The checked ISA model expands UXTAH and shifted ADD into their integer semantics.
The Arm assembler independently checks that the actual instructions are legal.
These checks do not predict cycle timing or establish a full modem/RF result.
"""
from __future__ import annotations

import ctypes as ct
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np

import test_iq16chunk_asm as vm
import test_iq32keys as keys
import test_iq32native8 as native


class State(ct.Structure):
    _fields_ = [('last', ct.c_uint32), ('pair', ct.c_uint32)]


class Machine(keys.Machine):
    def c_leaf(self):
        r, mem = self.r, self.mem
        assert r[13] % 8 == 0
        address = mem.read(r[13])
        tables = native.Tables(*(mem.ptr(mem.read(address + 4*i), native.ROWS*8)
                                 for i in range(3)))
        self.lib.iq32offsets_kernel_c(mem.ptr(r[0], r[2]*16), mem.ptr(r[1], r[2]*8), r[2],
                                      mem.ptr(r[3], 20) if r[3] else None, ct.byref(tables))
        target = r[14] & ~1
        for i in (0, 1, 2, 3, 12):
            r[i] = 0xdec00000 + i
        r[14], r[15] = 0xdec0000e, target
        self.zero = self.borrow = False
        self.c_calls += 1


def semantic_program(path):
    actual, labels = vm.program(path)
    code, positions = [], {}
    for index, text in enumerate(actual):
        positions[4*index] = 4*len(code)
        op, *tail = text.split(maxsplit=1)
        args = vm.operands(tail[0]) if tail else []
        if op == 'uxtah':
            dst, base, source = args
            assert dst != base
            code += [f'uxth {dst},{source}', f'add {dst},{base},{dst}']
        elif op == 'add' and len(args) == 4 and args[3].startswith('lsr #'):
            dst, base, source, shift = args
            assert dst != base
            code += [f'lsr {dst},{source},#{shift[5:]}', f'add {dst},{base},{dst}']
        else:
            code.append(text.replace('iq32offsets_kernel_c', 'iq32native8_kernel_c'))
    positions[4*len(actual)] = 4*len(code)
    return actual, code, {name: positions[value] for name, value in labels.items()}


def execute_asm(code, labels, lib, arrays, offsets, previous):
    mem = vm.Memory()
    for name, data in zip(('edge', 'middle', 'reverse'), arrays):
        mem.add(name, data.tobytes())
    mem.add('tables', np.array([vm.BASE[n] for n in ('edge', 'middle', 'reverse')], '<u4').tobytes())
    history = np.full(5, 14736 | 14736 << 16, np.uint32) if previous is None else previous
    source = mem.add('input', offsets.astype('<u4').tobytes())
    old = mem.add('previous', history.astype('<u4').tobytes())
    output = mem.add('output', np.full(2*len(offsets)+2, native.CANARY, '<u4').tobytes(), True)
    stack = mem.add('stack', bytes(4096), True)
    machine = Machine(code, labels, mem, lib)
    machine.r = [0x12340000+i*0x111 for i in range(16)]
    machine.r[:4] = [vm.BASE['output']+4, vm.BASE['input'], len(offsets)//2,
                      vm.BASE['previous'] if previous is not None else 0]
    machine.r[13:16] = [vm.BASE['stack']+4088, vm.END | 1, labels['iq32offsets_kernel_asm']]
    mem.write(machine.r[13], vm.BASE['tables'])
    saved, sp = machine.r[4:12].copy(), machine.r[13]
    machine.run()
    assert machine.r[4:12] == saved and machine.r[13] == sp
    assert source == offsets.astype('<u4').tobytes() and old == history.astype('<u4').tobytes()
    assert stack[:4040] == bytes(4040)
    output = np.frombuffer(output, '<u4')
    assert output[0] == output[-1] == native.CANARY
    assert machine.c_calls == int(bool(len(offsets)))
    return output[1:-1], machine.steps


def execute_c(lib, offsets, previous, tables, partition):
    history = np.full(5, 14736 | 14736 << 16, np.uint32) if previous is None else previous
    data = np.concatenate([history, offsets])
    output = np.full(2*len(offsets)+2, native.CANARY, np.uint32)
    for start in range(0, len(offsets)//2, partition):
        lib.iq32offsets_kernel_c(native.ptr(output[1+4*start:]), native.ptr(offsets[2*start:]),
                                  min(partition, len(offsets)//2-start),
                                  native.ptr(data[2*start:2*start+5]), ct.byref(tables))
    assert output[0] == output[-1] == native.CANARY
    return output[1:-1]


def scaled_pair_proof(lib, rng):
    u8p = ct.POINTER(ct.c_uint8)
    p8 = lambda a: a.ctypes.data_as(u8p)
    lib.iq32mapkeys_init.argtypes = [ct.POINTER(State), u8p]
    lib.iq32mapkeys_encode.argtypes = [native.PTR, u8p, ct.c_uint32, ct.POINTER(State)]
    lib.iq32mapkeys_pair_build.argtypes = [native.PTR]
    lib.iq32mapkeys_pair_canonical.argtypes = [native.PTR, u8p, ct.c_uint32, ct.POINTER(State), native.PTR]
    lib.iq32mapkeys_canonical.argtypes = [ct.c_uint8]
    lib.iq32mapkeys_canonical.restype = ct.c_uint8
    table = np.empty(4096, np.uint32)
    lib.iq32mapkeys_pair_build(native.ptr(table))
    scaled = table << np.uint32(3)
    assert np.max(table & 65535) <= 4896 and np.max(table >> 16) <= 4896
    assert np.array_equal(scaled & 65535, (table & 65535)*8)
    assert np.array_equal(scaled >> 16, (table >> 16)*8)
    tokens = np.r_[np.arange(132, dtype='u1'), rng.integers(0, 132, 2048-132, dtype='u1')]
    canonical = np.array([lib.iq32mapkeys_canonical(int(t)) for t in tokens], dtype='u1')
    previous = rng.integers(0, 132, 2, dtype='u1')
    state = State()
    lib.iq32mapkeys_init(ct.byref(state), p8(previous))
    reference = np.empty(len(tokens), np.uint32)
    lib.iq32mapkeys_encode(native.ptr(reference), p8(tokens), len(tokens), ct.byref(state))
    checks = 0
    for partition in (2, 4, 6, 32, 512, 1024, 2048):
        state = State()
        lib.iq32mapkeys_init(ct.byref(state), p8(previous))
        state.last <<= 3; state.pair <<= 3
        output = np.full(len(tokens)+2, native.CANARY, np.uint32)
        for start in range(0, len(tokens), partition):
            count = min(partition, len(tokens)-start)
            assert not count % 2  # Existing odd tail uses unscaled weighted rows.
            lib.iq32mapkeys_pair_canonical(native.ptr(output[1+start:]), p8(canonical[start:]),
                                           count, ct.byref(state), native.ptr(scaled))
        assert output[0] == output[-1] == native.CANARY
        np.testing.assert_array_equal(output[1:-1], reference << np.uint32(3))
        checks += len(tokens)
    return checks


def main():
    source = native.ROOT/'firmware/src'
    vm.BASE['tables'] = 0x20040000
    actual, code, labels = semantic_program(source/'iq32offsets_asm.S')
    original_code, original_labels = vm.program(source/'iq32offsets_asm.S')
    loop = original_code[original_labels['5']//4:original_labels['2']//4]
    assert len(loop) == 27 and sum(s.startswith('uxtah ') for s in loop) == 3
    rng = np.random.default_rng(2026101502)
    cases = steps = 0
    with tempfile.TemporaryDirectory(prefix='iq32offsets-') as temp:
        library = Path(temp)/'native.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        f'-I{source}', *(str(source/f) for f in ('iq32native8.c', 'iq32keys.c',
                        'iq32offsets.c', 'iq32mapkeys.c', 'iq32mapkeys_pair.c')), '-o', str(library)], check=True)
        lib = ct.CDLL(str(library))
        lib.iq32native8_build.argtypes = [ct.POINTER(native.Tables), ct.c_uint]
        lib.iq32native8_build.restype = ct.c_bool
        lib.iq32native8_fill.argtypes = [native.PTR, ct.c_uint32, native.PTR]
        lib.iq32keys_encode.argtypes = [native.PTR, native.PTR, ct.c_uint32, native.PTR]
        lib.iq32keys_history.argtypes = [native.PTR, native.PTR]
        lib.iq32offsets_encode.argtypes = [native.PTR, native.PTR, ct.c_uint32]
        lib.iq32offsets_kernel_c.argtypes = [native.PTR, native.PTR, ct.c_uint32, native.PTR,
                                           ct.POINTER(native.Tables)]
        lib.iq32offsets_encode(None, None, 0)
        lib.iq32offsets_kernel_c(None, None, 0, None, None)
        all_keys = np.arange(4913, dtype=np.uint32)
        lanes = all_keys | rng.permutation(all_keys) << np.uint32(16)
        converted = np.full(len(lanes)+2, native.CANARY, np.uint32)
        lib.iq32offsets_encode(native.ptr(converted[1:]), native.ptr(lanes), len(lanes))
        assert converted[0] == converted[-1] == native.CANARY
        np.testing.assert_array_equal(converted[1:-1] & 65535, (lanes & 65535)*8)
        np.testing.assert_array_equal(converted[1:-1] >> 16, (lanes >> 16)*8)
        inplace = lanes.copy()
        lib.iq32offsets_encode(native.ptr(inplace), native.ptr(inplace), len(inplace))
        np.testing.assert_array_equal(inplace, converted[1:-1])
        pair_symbols = scaled_pair_proof(lib, rng)
        arrays = [np.empty(2*native.ROWS, np.uint32) for _ in range(3)]
        tables = native.Tables(*(native.ptr(a) for a in arrays))
        words = np.empty(8192, np.uint32)
        seed = ct.c_uint32(0x1234abcd)
        lib.iq32native8_fill(native.ptr(words), len(words), ct.byref(seed))
        coordinates = rng.integers(0, 17, (4, 2, 2), dtype=np.uint32)
        previous = sum(coordinates[:, j, a] << (16*a+5*j) for j in range(2) for a in range(2))
        for rate in range(5):
            assert lib.iq32native8_build(ct.byref(tables), rate)
            floating, quantized = native.levels(rate)
            scaled = (quantized[:, None]*native.TAPS+(1 << 29)) >> 30
            for history in (None, previous):
                normal_keys = keys.encode(lib, words, history, 8192)
                offsets = normal_keys << np.uint32(3)
                key_history = np.empty(5, np.uint32)
                lib.iq32keys_history(native.ptr(key_history), None if history is None else native.ptr(history))
                offset_history = key_history << np.uint32(3)
                expected, _, _ = native.expected(words, history, scaled, floating)
                for partition in (1, 2, 3, 5, 7, 31, 256, 8192):
                    np.testing.assert_array_equal(execute_c(lib, offsets, offset_history, tables, partition), expected)
                    cases += 1
                for count in (0, 1, 2, 3, 4, 5, 6, 7, 16, 31, 64, 257):
                    output, executed = execute_asm(code, labels, lib, arrays, offsets[:2*count],
                                                   None if history is None else offset_history)
                    np.testing.assert_array_equal(output, expected[:4*count])
                    cases += 1; steps += executed
        # All4913 row addresses, including offsets with their sign bit set.
        offsets = np.concatenate([converted[1:-1], converted[1:2]])
        reference = execute_c(lib, offsets, None, tables, 17)
        output, executed = execute_asm(code, labels, lib, arrays, offsets, None)
        np.testing.assert_array_equal(output, reference)
        cases += 1; steps += executed
    print(json.dumps(dict(status='PASS', scope='native C and actual-source ISA semantics; no timing',
                          cases=cases, executed_semantic_operations=steps, rate_profiles=5,
                          input_bytes_per_symbol=4, input_offset_lane_max=39296,
                          address_keys_exhaustive=4913, scaled_pair_symbols=pair_symbols,
                          pair_scaled_input_requires_even_count=True, asm_frame_bytes=48,
                          actual_loop_instructions_per_two_symbols=len(loop),
                          previous_loop_instructions_per_two_symbols=33), indent=2))


if __name__ == '__main__':
    main()
