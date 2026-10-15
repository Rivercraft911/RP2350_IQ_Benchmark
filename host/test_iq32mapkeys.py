"""Geometric-label to rolling-key proofs; native component, no Arm timing."""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
U8P = ctypes.POINTER(ctypes.c_uint8)
U32P = ctypes.POINTER(ctypes.c_uint32)
CANARY = 0xa75c12e9


class State(ctypes.Structure):
    _fields_ = [('last', ctypes.c_uint32), ('pair', ctypes.c_uint32)]


class GuardedState(ctypes.Structure):
    _fields_ = [('before', ctypes.c_uint32), ('state', State),
                ('after', ctypes.c_uint32)]


def p8(a):
    return a.ctypes.data_as(U8P)


def p32(a):
    return a.ctypes.data_as(U32P)


def crc(a):
    return zlib.crc32(a.tobytes()) & 0xffffffff


def coordinate_reference(rate):
    """Independent polar ring construction and nearest axis-code match."""
    g1, g2 = np.array(((284,527), (272,487), (264,464), (254,433),
                      (253,430)), dtype=float)[rate] / 100
    levels = np.array((0, 1/(g2*np.sqrt(2)), np.sin(np.pi/12)*g1/g2,
                       g1/(g2*np.sqrt(2)), np.cos(np.pi/12)*g1/g2,
                       np.sin(np.pi/8), 1/np.sqrt(2), np.cos(np.pi/8), 1))
    levels = np.concatenate((levels, -levels[1:]))
    points = np.concatenate((
        np.exp(1j*(np.pi/4 + np.arange(4)*np.pi/2))/g2,
        g1/g2*np.exp(1j*(np.pi/12 + np.arange(12)*np.pi/6)),
        np.exp(1j*np.arange(16)*np.pi/8)))
    points = np.concatenate([points*(1j)**q for q in range(4)] +
                            [np.exp(1j*(np.pi/4 + np.arange(4)*np.pi/2))])
    coordinates = []
    for axis in (points.real, points.imag):
        nearest = np.argmin(np.abs(axis[:, None] - levels), axis=1)
        assert np.max(np.abs(axis-levels[nearest])) < 1e-14
        coordinates.append(nearest.astype(np.uint32))
    return coordinates[0] | coordinates[1] << 16


def expected(tokens, previous, lookup):
    history = np.full(2, 6 | 6<<16, np.uint32) if previous is None else lookup[previous]
    data = np.concatenate((history, lookup[tokens]))
    axes = []
    for shift in (0, 16):
        code = (data >> shift) & 0xffff
        axes.append(code[:-2] + 17*code[1:-1] + 289*code[2:])
    return axes[0] | axes[1]<<16


def execute(lib, tokens, previous, partition):
    output = np.full(len(tokens)+2, CANARY, np.uint32)
    state = GuardedState(CANARY, State(), CANARY)
    lib.iq32mapkeys_init(ctypes.byref(state.state), None if previous is None else p8(previous))
    offset = 0
    for count in partition:
        if offset == len(tokens):
            break
        count = min(count, len(tokens)-offset)
        lib.iq32mapkeys_encode(p32(output[1+offset:]), p8(tokens[offset:]),
                              count, ctypes.byref(state.state))
        offset += count
    assert offset == len(tokens)
    assert output[0] == output[-1] == state.before == state.after == CANARY
    return output[1:-1], state.state


def de_bruijn(alphabet):
    # Every possible three-token history appears once in this cyclic stream.
    a, output = [0]*(alphabet*3), bytearray()

    def visit(t, period):
        if t > 3:
            if 3 % period == 0:
                output.extend(a[1:period+1])
            return
        a[t] = a[t-period]
        visit(t+1, period)
        for symbol in range(a[t-period]+1, alphabet):
            a[t] = symbol
            visit(t+1, t)
    visit(1, 1)
    assert len(output) == alphabet**3
    return np.frombuffer(output, np.uint8)


def main():
    source = ROOT/'firmware/src'
    rng = np.random.default_rng(20261016)
    checks = 0
    with tempfile.TemporaryDirectory(prefix='iq32mapkeys-') as temp:
        library = Path(temp)/'native.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', f'-I{source}', str(source/'iq32mapkeys.c'),
                        str(source/'iq32keys.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iq32mapkeys_coordinate.argtypes = [ctypes.c_uint8]
        lib.iq32mapkeys_coordinate.restype = ctypes.c_uint32
        lib.iq32mapkeys_init.argtypes = [ctypes.POINTER(State), U8P]
        lib.iq32mapkeys_encode.argtypes = [U32P, U8P, ctypes.c_uint32, ctypes.POINTER(State)]
        lib.iq32mapkeys_history.argtypes = [U32P, U8P]
        lib.iq32keys_encode.argtypes = [U32P, U32P, ctypes.c_uint32, U32P]
        lib.iq32mapkeys_encode(None, None, 0, None)
        lookup = np.array([lib.iq32mapkeys_coordinate(i) for i in range(132)], np.uint32)
        for rate in range(5):
            np.testing.assert_array_equal(lookup, coordinate_reference(rate))
            checks += 132
        tokens = rng.integers(0, 132, 16403, dtype=np.uint8)
        original = tokens.copy()
        for previous in (None, np.array([128, 131], np.uint8),
                         np.array([127, 0], np.uint8),
                         rng.integers(0, 132, 2, dtype=np.uint8)):
            reference = expected(tokens, previous, lookup)
            for size in (1, 2, 3, 5, 31, 256, 1024, len(tokens)):
                result, state = execute(lib, tokens, previous, [size]*((len(tokens)+size-1)//size))
                np.testing.assert_array_equal(result, reference)
                assert state.last == lookup[tokens[-1]]
                assert state.pair == int(lookup[tokens[-2]]) + 17*int(lookup[tokens[-1]])
                checks += len(tokens)
            irregular = rng.integers(0, 250, 2000).tolist()
            irregular += [len(tokens)]
            result, _ = execute(lib, tokens, previous, irregular)
            np.testing.assert_array_equal(result, reference)
            checks += len(tokens)
        np.testing.assert_array_equal(tokens, original)
        for previous in (None, rng.integers(0, 132, 7, dtype=np.uint8)):
            result = np.full(7, CANARY, np.uint32)
            lib.iq32mapkeys_history(p32(result[1:-1]), None if previous is None else p8(previous))
            reference = np.full(5, 1842 | 1842<<16, np.uint32) if previous is None else expected(previous[2:], previous[:2], lookup)
            np.testing.assert_array_equal(result[1:-1], reference)
            assert result[0] == result[-1] == CANARY
            checks += 5
        # Cross-check the established coordinate-word format, with custom tails.
        even_tokens = tokens[:16384]
        coordinates = lookup[even_tokens]
        words = coordinates[::2] | coordinates[1::2]<<5
        previous = np.array([99, 131], np.uint8)
        history = np.array([int(lookup[previous[0]]) | int(lookup[previous[1]])<<5], np.uint32)
        keys = np.empty(len(even_tokens), np.uint32)
        lib.iq32keys_encode(p32(keys), p32(words), len(words), p32(history))
        np.testing.assert_array_equal(keys, expected(even_tokens, previous, lookup))
        checks += len(keys)
        exhaustive = de_bruijn(132)
        actual, _ = execute(lib, exhaustive, exhaustive[-2:], [len(exhaustive)])
        np.testing.assert_array_equal(actual, expected(exhaustive, exhaustive[-2:], lookup))
        checks += len(exhaustive)
        # Match iq32native8_fill's xorshift point order, without storing coordinates.
        seed, fixture = 0x1234abcd, np.empty(16384, np.uint8)
        for i in range(len(fixture)):
            seed ^= (seed << 13) & 0xffffffff
            seed ^= seed >> 17
            seed ^= (seed << 5) & 0xffffffff
            fixture[i] = seed & 31
        fixture_keys, _ = execute(lib, fixture, None, [len(fixture)])
    print(json.dumps(dict(status='PASS', scope='native exact geometric/key component; no device timing',
                          coordinate_checks=660, rolling_key_checks=checks-660,
                          exhaustive_token_triples=132**3, valid_tokens=132,
                          state_bytes=ctypes.sizeof(State), const_table_bytes=132*12,
                          fixture_symbols=len(fixture), fixture_label_crc=crc(fixture),
                          fixture_key_crc=crc(fixture_keys), fixture_final_seed=seed,
                          fixture_history='repeated unitpilot'), indent=2))


if __name__ == '__main__':
    main()
