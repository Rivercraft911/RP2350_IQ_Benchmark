"""Exercise the actual output bench's cyclic block helper with real C tables/FIR.

Checks source-tail history and multiple arena/source wraps against an independent
scalar reference. This native test does not simulate DMA deadlines or Arm timing.
"""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import re
import subprocess
import tempfile

import numpy as np

import iq16out_trial as trial
from test_dual_faults import function_body
from test_iq16native8 import Tables, ROWS, ptr


def main():
    source = trial.iq.ROOT/'firmware/src'
    bench = (source/'bench16out.c').read_text()
    enum = re.search(r'enum\s*\{[\s\S]*?\};', bench)[0]
    body = function_body(bench, 'shape_block')
    fixture = f'''
#include <assert.h>
#include <stdint.h>
#include "iq16native8.h"
#define IN_WORDS8192 8192
#define IN_WORDS IN_WORDS8192
{enum}
typedef void (*kernel_fn)(uint32_t *, const uint32_t *, uint32_t,
                          const uint32_t *, const iq16native8_tables_t *);
uint32_t in_buf[IN_WORDS];
static uint32_t calls, expected_sequence;
static void checked_kernel(uint32_t *out, const uint32_t *input, uint32_t blocks,
                            const uint32_t *previous, const iq16native8_tables_t *tables) {{
    const unsigned first = (expected_sequence * BLOCK_INPUT) & INPUT_MASK;
    assert(blocks == BLOCK_INPUT && input == in_buf+first);
    assert(previous == in_buf+((first-2u)&INPUT_MASK));
    assert(input+blocks <= in_buf+IN_WORDS && previous+2 <= in_buf+IN_WORDS);
    ++calls;
    iq16native8_kernel_c(out,input,blocks,previous,tables);
}}
void checked_shape(uint32_t *output, uint32_t sequence, const iq16native8_tables_t *tables) {{
    expected_sequence=sequence;
    {{
        kernel_fn run=checked_kernel;
        {body}
    }}
}}
unsigned checked_calls(void) {{ return calls; }}
'''
    with tempfile.TemporaryDirectory(prefix='iq16out-native-') as directory:
        path = Path(directory)
        (path/'check.c').write_text(fixture)
        library = path/'check.so'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        f'-I{source}', str(path/'check.c'), str(source/'iq16native8.c'), '-o', str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.checked_shape.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.POINTER(Tables)]
        lib.iq16native8_build.argtypes = [ctypes.POINTER(Tables), ctypes.c_uint]
        lib.iq16native8_build.restype = ctypes.c_bool
        input_words = np.ctypeslib.as_array((ctypes.c_uint32*trial.IN_WORDS).in_dll(lib, 'in_buf'))
        input_words[:] = trial.source_words()
        arrays = [np.empty(rows*4, np.uint32) for rows in ROWS]
        tables = Tables(*(ptr(a) for a in arrays))
        assert lib.iq16native8_build(ctypes.byref(tables), trial.GAMMA)
        canary = np.uint32(0x78ab3491)
        guarded = np.full(4*2048+2, canary, np.uint32)
        output_ring = guarded[1:-1]
        checked = 0
        for sequence in [*range(32*3+7), 0xfffffffd, 0xfffffffe, 0xffffffff]:
            slot = sequence % 4
            before = output_ring.copy()
            block = output_ring[slot*2048:(slot+1)*2048]
            lib.checked_shape(ptr(block), sequence, ctypes.byref(tables))
            # PIO RAW_PAIRS emits I0,Q0,I1,Q1. Signed padding is discarded,
            # and IQ_FLAG is supplied by side-set rather than by the sample words.
            lanes = block.reshape(-1, 2).view('<u2').reshape(-1, 4)
            bus = lanes[:, [0, 2, 1, 3]].ravel().copy()
            bus &= 0x3fff; bus[::2] |= 0x4000
            period = trial.reference_period()
            start = (sequence*trial.BLOCK_SYMBOLS*4) % len(period)
            want = period[(start+np.arange(len(bus))) % len(period)]
            np.testing.assert_array_equal(bus, want)
            assert np.array_equal(before[:slot*2048], output_ring[:slot*2048])
            assert np.array_equal(before[(slot+1)*2048:], output_ring[(slot+1)*2048:])
            assert guarded[0] == guarded[-1] == canary
            checked += len(bus)
        assert lib.checked_calls() == 106
        np.testing.assert_array_equal(input_words, trial.source_words())
    print(json.dumps(dict(native_output_helper='passed', actual_cyclic_blocks=106,
                          checked_gpio_halfwords=checked, source_wraps=3,
                          sequence_u32_wrap_cases=3, arena_guards=True,
                          zero_history_used=False, silicon_timing=False)))


if __name__ == '__main__': main()
