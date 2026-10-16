"""Packed runtime8PSK source against independent scalar constellation codes."""
import ctypes
from pathlib import Path
import random
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def next32(x):
    x ^= (x << 13) & 0xffffffff
    x ^= x >> 17
    return (x ^ (x << 5)) & 0xffffffff


def main():
    with tempfile.TemporaryDirectory(prefix='iq4prod-source-') as tmp:
        library = Path(tmp) / 'source.so'
        src = ROOT / 'firmware/src'
        subprocess.run(['cc', '-O3', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(src / 'iq4prod_source.c'), '-o', str(library)], check=True)
        native = ctypes.CDLL(str(library))
        word = ctypes.c_uint32
        native.iq4prod_source.argtypes = [ctypes.POINTER(word), ctypes.POINTER(word),
                                         ctypes.c_uint, ctypes.POINTER(word)]
        rng = random.Random(0xb62350)
        groups = 0
        # Scalar valid coordinate pairs, derived from the rotated eight angles:
        # [C,-S,S,-C] for each axis, never both large or both small.
        valid = {(0, 2), (2, 0), (1, 0), (3, 2), (3, 1), (1, 3), (2, 3), (0, 1)}
        for seed in [1, 0x1234abcd, 0xffffffff] + [rng.randrange(1, 2**32) for _ in range(20)]:
            count = 4099
            i, q = (word * (count + 2))(), (word * (count + 2))()
            i[count] = q[count] = 0xabcddcba
            state = word(seed)
            # Arbitrary publication partitions must continue exactly, with no reset.
            done = 0
            while done < count:
                n = min(count - done, rng.randrange(1, 130))
                native.iq4prod_source(ctypes.cast(ctypes.byref(i, 4*done), ctypes.POINTER(word)),
                                     ctypes.cast(ctypes.byref(q, 4*done), ctypes.POINTER(word)), n,
                                     ctypes.byref(state))
                done += n
            x = seed
            for n in range(count):
                x = next32(x)
                expected_i = x
                x = next32(x)
                expected_q = 0
                for s in range(16):
                    ci = (expected_i >> (2*s)) & 3
                    sign_q = (x >> (2*s)) & 1
                    cq = next(c for c in range(4) if (ci, c) in valid and (c & 1) == sign_q)
                    expected_q |= cq << (2*s)
                assert (i[n], q[n]) == (expected_i, expected_q)
            assert state.value == x and i[count] == q[count] == 0xabcddcba
            groups += count
        native.iq4prod_source(i, q, 0, ctypes.byref(state))
        assert state.value == x
    print(f'Runtime packed8PSK: {groups} groups, {16*groups} valid constellation symbols; partition continuity and guards pass')


if __name__ == '__main__':
    main()
