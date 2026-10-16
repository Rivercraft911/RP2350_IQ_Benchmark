"""Exact16KiB two-symbol key mapper, including all token histories/chunks."""
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_iq32mapkeys as base
import fwsrc


def execute(lib, kernel, table, tokens, previous, partitions):
    output = np.full(len(tokens)+2, base.CANARY, np.uint32)
    state = base.GuardedState(base.CANARY, base.State(), base.CANARY)
    lib.iq32mapkeys_init(ctypes.byref(state.state), None if previous is None else base.p8(previous))
    offset = 0
    for count in partitions:
        if offset == len(tokens):
            break
        count = min(count, len(tokens)-offset)
        kernel(base.p32(output[1+offset:]), base.p8(tokens[offset:]), count,
               ctypes.byref(state.state), base.p32(table))
        offset += count
    assert offset == len(tokens)
    assert output[0] == output[-1] == state.before == state.after == base.CANARY
    return output[1:-1], state.state


def main():
    source = base.ROOT/'firmware/src'
    rng = np.random.default_rng(2026101601)
    checks = 0
    with tempfile.TemporaryDirectory(prefix='iq32mapkeys-pair-') as temp:
        library = Path(temp)/'native.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', *fwsrc.INCLUDES, str(source / 'apsk/iq32mapkeys.c'),
                        str(source / 'apsk/iq32mapkeys_pair.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iq32mapkeys_init.argtypes = [ctypes.POINTER(base.State), base.U8P]
        lib.iq32mapkeys_canonical.argtypes = [ctypes.c_uint8]
        lib.iq32mapkeys_canonical.restype = ctypes.c_uint8
        lib.iq32mapkeys_pair_build.argtypes = [base.U32P]
        for name in ('iq32mapkeys_pair_encode', 'iq32mapkeys_pair_canonical'):
            getattr(lib, name).argtypes = [base.U32P, base.U8P, ctypes.c_uint32,
                                           ctypes.POINTER(base.State), base.U32P]
            getattr(lib, name)(None, None, 0, None, None)
        canonical = np.array([lib.iq32mapkeys_canonical(t) for t in range(132)], np.uint8)
        lookup = base.coordinate_reference(2)
        np.testing.assert_array_equal(lookup, lookup[canonical])
        table = np.full(4098, base.CANARY, np.uint32)
        lib.iq32mapkeys_pair_build(base.p32(table[1:-1]))
        assert table[0] == table[-1] == base.CANARY
        rows = table[1:-1].reshape(32, 32, 4)
        for a in range(32):
            for b in range(32):
                c, d = int(lookup[a]), int(lookup[b])
                np.testing.assert_array_equal(rows[a, b],
                                               [289*c, 17*c+289*d, c+17*d, d])
                checks += 4
        original = table.copy()
        tokens = rng.integers(0, 132, 32787, dtype=np.uint8)
        for previous in (None, np.array([128, 131], np.uint8),
                         np.array([127, 0], np.uint8),
                         rng.integers(0, 132, 2, dtype=np.uint8)):
            reference = base.expected(tokens, previous, lookup)
            for canonical_input in (False, True):
                encoded = canonical[tokens] if canonical_input else tokens
                history = canonical[previous] if canonical_input and previous is not None else previous
                kernel = lib.iq32mapkeys_pair_canonical if canonical_input else lib.iq32mapkeys_pair_encode
                for size in (1, 2, 3, 5, 31, 256, 1024, len(tokens)):
                    result, state = execute(lib, kernel, table[1:-1], encoded, history,
                                            [size]*((len(tokens)+size-1)//size))
                    np.testing.assert_array_equal(result, reference)
                    assert state.last == lookup[tokens[-1]]
                    assert state.pair == int(lookup[tokens[-2]]) + 17*int(lookup[tokens[-1]])
                    checks += len(tokens)
                irregular = rng.integers(0, 250, 2000).tolist() + [len(tokens)]
                result, _ = execute(lib, kernel, table[1:-1], encoded, history, irregular)
                np.testing.assert_array_equal(result, reference)
                checks += len(tokens)
        exhaustive = base.de_bruijn(132)
        for canonical_input in (False, True):
            stream = canonical[exhaustive] if canonical_input else exhaustive
            kernel = lib.iq32mapkeys_pair_canonical if canonical_input else lib.iq32mapkeys_pair_encode
            result, _ = execute(lib, kernel, table[1:-1], stream, stream[-2:], [len(stream)])
            np.testing.assert_array_equal(result, base.expected(exhaustive, exhaustive[-2:], lookup))
            checks += len(stream)
        np.testing.assert_array_equal(table, original)
    print(json.dumps(dict(status='PASS', scope='native exact pair-key component; no device timing',
                          checked_values=checks, exhaustive_token_triples_per_leaf=132**3,
                          pair_table_bytes=16384, canonical_table_bytes=132,
                          mutable_global_bytes=0, pair_table_crc=base.crc(table[1:-1])), indent=2))


if __name__ == '__main__':
    main()
