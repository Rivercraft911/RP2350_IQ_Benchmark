"""Bit-exact fused 8PSK two-bit-code mapping, slices and frame ownership.

Compare every code against both the existing C sign-plane mapper followed by
its four-level packer and the independent scalar Python PLFRAME/Gold reference.
No Pico or serial device is accessed.
"""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_dvbs2_8psk as old

ROOT = Path(__file__).resolve().parents[1]
PTR = old.PTR
CANARY = old.CANARY


class Codes(ctypes.Structure):
    _fields_ = [("i", PTR), ("q", PTR)] + [(name, ctypes.c_uint32) for name in
                ("mask", "n", "acc_i", "acc_q", "fill")] + [
                ("defer_first", ctypes.c_bool), ("first_pending", ctypes.c_bool),
                ("first_i", ctypes.c_uint32), ("first_q", ctypes.c_uint32)]


class State(ctypes.Structure):
    _fields_ = [("cfg", ctypes.c_void_p), ("frame", PTR),
                ("stream", ctypes.POINTER(Codes)), ("data", ctypes.c_uint32),
                ("pilot", ctypes.c_uint32), ("header_done", ctypes.c_bool),
                ("fast", ctypes.c_bool), ("c6", ctypes.c_bool)]


def packed_codes(points, flush=False):
    if flush and len(points) % 16:
        points = np.concatenate([points, np.full(16-len(points) % 16, np.exp(1j*np.pi/8))])
    points = points[:len(points)//16*16]
    signs = old.sign_planes(points)
    shifts = 2*np.arange(16, dtype=np.uint32)
    return np.stack([((signs[k] | signs[k+1] << 1).reshape(-1, 16) << shifts).sum(
        axis=1, dtype=np.uint32) for k in (0, 2)], axis=1)


def buffer(words):
    raw = (ctypes.c_uint32*(words+2))()
    raw[0] = raw[-1] = CANARY
    return raw, ctypes.cast(ctypes.byref(raw, 4), PTR)


def setup(lib, rate, short, pilots, gold, fast=False):
    index = old.ref.RATES.index(rate)+(11 if short else 0)
    code = lib.test_code(index)
    need = lib.dvbs2_8psk_arena_words(code, pilots)
    arena = (ctypes.c_uint32*need)()
    cfg = (ctypes.c_uint64*32)()
    assert lib.dvbs2_8psk_init(cfg, code, pilots, gold, arena, need)
    lut_words = 1252 if fast == 2 else 484
    lut_raw, lut = buffer(lut_words)
    if fast:
        init = lib.dvbs2_8psk_codes_c6_init if fast == 2 else lib.dvbs2_8psk_codes_lut_init
        before = bytes(cfg), bytes(lut_raw)
        assert not init(cfg, lut, lut_words - 1)
        assert before == (bytes(cfg), bytes(lut_raw))
        assert init(cfg, lut, lut_words)
        assert lut_raw[0] == lut_raw[-1] == CANARY
    return cfg, (arena, lut_raw)


def check(lib, rate, short, pilots, gold, prefix, ring, rng, fast=False):
    cfg, arena = setup(lib, rate, short, pilots, gold, fast)
    fec = [rng.integers(0, 2, 16200 if short else 64800, dtype=np.uint8) for _ in range(3)]
    frame_points = [old.scalar_frame(x, rate, short, pilots, gold) for x in fec]
    points = np.concatenate([np.full(prefix, np.exp(1j*np.pi/8)), *frame_points])
    groups = (len(points)+15)//16
    size = 2 if ring else groups+1
    iraw, ip = buffer(size); qraw, qp = buffer(size)
    stream = Codes(i=ip, q=qp, mask=size-1 if ring else 0xffffffff,
                   fill=prefix, acc_q=((1 << (2*prefix))-1) & 0xaaaaaaaa)
    original = (old.Input*(groups+1))()
    signed = old.Stream(out=original, mask=0xffffffff, fill=prefix, acc_bq=(1 << prefix)-1)
    consumed = prefix
    calls = 0
    for f, fp in zip(fec, frame_points):
        words = old.frame_words(f)
        before = words.copy()
        state = State()
        args = ctypes.byref(state), cfg, words.ctypes.data_as(PTR), ctypes.byref(stream)
        if fast == 2:
            assert lib.dvbs2_8psk_codes_begin_c6(*args)
        elif fast:
            before_state = bytes(state)
            assert not lib.dvbs2_8psk_codes_begin_c6(*args)
            assert bytes(state) == before_state
            assert lib.dvbs2_8psk_codes_begin_fast(*args)
        else:
            before_state = bytes(state)
            assert not lib.dvbs2_8psk_codes_begin_fast(*args)
            assert bytes(state) == before_state
            lib.dvbs2_8psk_codes_begin(*args)
        snapshot = bytes(stream)
        assert not lib.dvbs2_8psk_codes_step(ctypes.byref(state), 0)
        assert bytes(stream) == snapshot and not state.header_done and not state.data
        while True:
            budget = (1, 2, 7, 32, 64)[calls % 5]
            previous = state.data
            done = lib.dvbs2_8psk_codes_step(ctypes.byref(state), budget)
            assert 0 < state.data-previous <= 32*budget
            calls += 1
            if done: break
        snapshot = bytes(stream)
        assert lib.dvbs2_8psk_codes_step(ctypes.byref(state), 1)
        assert bytes(stream) == snapshot
        np.testing.assert_array_equal(words, before)
        lib.dvbs2_8psk_frame(cfg, words.ctypes.data_as(PTR), ctypes.byref(signed))
        consumed += len(fp)
        expected = packed_codes(points[:consumed])
        assert stream.n == signed.n == len(expected)
        assert stream.fill == signed.fill == consumed % 16
        for n in range(max(0, len(expected)-size), len(expected)):
            got = (ip[n & stream.mask], qp[n & stream.mask])
            np.testing.assert_array_equal(got, expected[n])
            i, q = ctypes.c_uint32(), ctypes.c_uint32()
            lib.iq4hist_pack_group(ctypes.byref(original[n]), ctypes.byref(i), ctypes.byref(q))
            assert got == (i.value, q.value)
        tail = points[len(expected)*16:consumed]
        bits = old.sign_planes(tail)
        assert stream.acc_i == sum((int(a) | int(b) << 1) << (2*k) for k, (a, b) in enumerate(zip(bits[0], bits[1])))
        assert stream.acc_q == sum((int(a) | int(b) << 1) << (2*k) for k, (a, b) in enumerate(zip(bits[2], bits[3])))
    lib.dvbs2_8psk_codes_flush(ctypes.byref(stream))
    lib.dvbs2_8psk_flush(ctypes.byref(signed))
    expected = packed_codes(points, flush=True)
    assert stream.n == signed.n == len(expected) and stream.fill == 0
    assert not stream.acc_i and not stream.acc_q
    for n in range(max(0, len(expected)-size), len(expected)):
        np.testing.assert_array_equal((ip[n & stream.mask], qp[n & stream.mask]), expected[n])
    assert (iraw[0], iraw[-1], qraw[0], qraw[-1]) == (CANARY,)*4
    return calls


def deferred(lib, rate, rng, prefix, start_n, fast=False):
    cfg, arena = setup(lib, rate, False, True, 99999, fast)
    fec = [rng.integers(0, 2, 64800, dtype=np.uint8) for _ in range(4)]
    points = np.concatenate([np.full(prefix, np.exp(1j*np.pi/8)),
                             *[old.scalar_frame(x, rate, False, True, 99999) for x in fec]])
    words = [old.frame_words(x) for x in fec]
    size = 8192
    iraw, ip = buffer(size); qraw, qp = buffer(size)
    streams = []
    frame_symbols = 22194
    # Complete frames out of order; only the first word belongs to the ordered
    # publisher. Later complete words have exactly one frame owner.
    for j in range(4):
        position = prefix + frame_symbols*j
        streams.append(Codes(i=ip, q=qp, mask=size-1, n=start_n+position//16,
                             fill=position % 16, defer_first=True))
    first_indices = [s.n & (size-1) for s in streams]
    for j in (3, 1, 0, 2):
        state = State()
        args = ctypes.byref(state), cfg, words[j].ctypes.data_as(PTR), ctypes.byref(streams[j])
        if fast == 2: assert lib.dvbs2_8psk_codes_begin_c6(*args)
        elif fast: assert lib.dvbs2_8psk_codes_begin_fast(*args)
        else: lib.dvbs2_8psk_codes_begin(*args)
        while not lib.dvbs2_8psk_codes_step(ctypes.byref(state), 8): pass
        assert streams[j].first_pending
        assert ip[first_indices[j]] == qp[first_indices[j]] == 0
    carry_i, carry_q = 0, ((1 << (2*prefix))-1) & 0xaaaaaaaa
    for j, stream in enumerate(streams):
        ip[first_indices[j]] = stream.first_i | carry_i
        qp[first_indices[j]] = stream.first_q | carry_q
        carry_i, carry_q = stream.acc_i, stream.acc_q
        assert stream.n == start_n + (prefix+(j+1)*frame_symbols)//16
        assert stream.fill == (prefix+(j+1)*frame_symbols) % 16
    expected = packed_codes(points)
    for n in range(len(expected)):
        np.testing.assert_array_equal((ip[(start_n+n) & (size-1)], qp[(start_n+n) & (size-1)]), expected[n])
    assert (iraw[0], iraw[-1], qraw[0], qraw[-1]) == (CANARY,)*4


def main():
    with tempfile.TemporaryDirectory(prefix="8psk-codes-native-") as temp:
        temp = Path(temp); src = ROOT/"firmware/src"
        bridge = temp/"bridge.c"; library = temp/"codes.so"
        bridge.write_text('#include "dvbs2_8psk.h"\n#include "dvbs2_codes.h"\n'
                          'const dvbs2_code_t *test_code(unsigned i){return &DVBS2_CODES[i];}\n'
                          'unsigned test_codes_size(void){return sizeof(dvbs2_8psk_codes_stream_t);}\n'
                          'unsigned test_state_size(void){return sizeof(dvbs2_8psk_codes_state_t);}\n')
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        f"-I{src}", str(src/"dvbs2_8psk.c"), str(src/"iq4hist.c"), str(bridge),
                        "-o", str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.test_code.argtypes = [ctypes.c_uint]; lib.test_code.restype = ctypes.c_void_p
        lib.dvbs2_8psk_arena_words.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        lib.dvbs2_8psk_arena_words.restype = ctypes.c_uint32
        lib.dvbs2_8psk_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool, ctypes.c_uint32, PTR, ctypes.c_uint32]
        lib.dvbs2_8psk_init.restype = ctypes.c_bool
        lib.dvbs2_8psk_codes_lut_init.argtypes = [ctypes.c_void_p, PTR, ctypes.c_uint32]
        lib.dvbs2_8psk_codes_lut_init.restype = ctypes.c_bool
        lib.dvbs2_8psk_codes_c6_init.argtypes = lib.dvbs2_8psk_codes_lut_init.argtypes
        lib.dvbs2_8psk_codes_c6_init.restype = ctypes.c_bool
        lib.dvbs2_8psk_frame.argtypes = [ctypes.c_void_p, PTR, ctypes.POINTER(old.Stream)]
        lib.dvbs2_8psk_flush.argtypes = [ctypes.POINTER(old.Stream)]
        lib.iq4hist_pack_group.argtypes = [ctypes.POINTER(old.Input), PTR, PTR]
        lib.dvbs2_8psk_codes_begin.argtypes = [ctypes.POINTER(State), ctypes.c_void_p, PTR, ctypes.POINTER(Codes)]
        lib.dvbs2_8psk_codes_begin_fast.argtypes = lib.dvbs2_8psk_codes_begin.argtypes
        lib.dvbs2_8psk_codes_begin_fast.restype = ctypes.c_bool
        lib.dvbs2_8psk_codes_begin_c6.argtypes = lib.dvbs2_8psk_codes_begin.argtypes
        lib.dvbs2_8psk_codes_begin_c6.restype = ctypes.c_bool
        lib.dvbs2_8psk_codes_step.argtypes = [ctypes.POINTER(State), ctypes.c_uint32]
        lib.dvbs2_8psk_codes_step.restype = ctypes.c_bool
        lib.dvbs2_8psk_codes_frame.argtypes = [ctypes.c_void_p, PTR, ctypes.POINTER(Codes)]
        lib.dvbs2_8psk_codes_flush.argtypes = [ctypes.POINTER(Codes)]
        assert lib.test_codes_size() == ctypes.sizeof(Codes)
        assert lib.test_state_size() == ctypes.sizeof(State)
        rng = np.random.default_rng(20261015)
        cases = calls = 0
        for fast in (0, 1, 2):
            for short in (False, True):
                for rate in old.RATES:
                    if short and rate == "9/10": continue
                    for pilots in (False, True):
                        for ring in (False, True):
                            calls += check(lib, rate, short, pilots, 0, cases % 16, ring, rng, fast)
                            cases += 1
            for prefix in range(16):
                calls += check(lib, "3/5" if prefix & 1 else "2/3", False, bool(prefix & 1),
                               (1, 99999, 262142)[prefix % 3], prefix, True, rng, fast)
                cases += 1
            for rate in ("2/3", "3/4", "5/6"):
                for prefix, start_n in ((0, 0), (7, 8190), (15, 8191)):
                    deferred(lib, rate, rng, prefix, start_n, fast)
    print(json.dumps(dict(configurations=22, cases=cases, frames=3*cases,
                          options=["baseline", "byte-lut", "c6"], slice_calls=calls,
                          deferred_cases=27, deferred_frames=108,
                          all_partial_fills=list(range(16)), result="bit-exact")))


if __name__ == "__main__": main()
