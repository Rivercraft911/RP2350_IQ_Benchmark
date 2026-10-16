"""Real GCS → native FEC/APSK/FIR against the finite scalar host reference.

Compiles portable firmware C only in a temporary directory. Three consecutive
frames exercise16APSK's partial native word across frame boundaries. Native
coordinate and32APSK offset kernels also run with different block partitions,
real preceding history, guarded buffers and progressive ring publication.
This checks functional integration, not Arm/DMA timing or an RF modem.
"""
from __future__ import annotations

import ctypes as ct
import json
from pathlib import Path
import subprocess
import tempfile
import zlib

import numpy as np

import apsk_fec_trial as oracle
from test_dvbs2_apsk import Config, Stream, SERVICE, Fast, FAST_WORDS
from test_dvbs2_8psk import frame_words
from test_iq16native8 import Tables
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
PTR = ct.POINTER(ct.c_uint32)
CANARY = 0x3ad6b891


def ptr(array):
    return array.ctypes.data_as(PTR)


def raw_bus(raw):
    lanes = raw.astype('<u4').view('<u2').reshape(-1, 4)
    bus = (lanes[:, [0, 2, 1, 3]].ravel() & 0x3fff).copy()
    bus[::2] |= 0x4000
    return bus


def configure(library):
    library.test_code.restype = ct.c_void_p
    library.test_fec.argtypes = [PTR, PTR]
    assert library.test_cfg_size() == ct.sizeof(Config)
    assert library.test_stream_size() == ct.sizeof(Stream)
    library.dvbs2_apsk_arena_words.argtypes = [ct.c_void_p, ct.c_uint, ct.c_bool]
    library.dvbs2_apsk_arena_words.restype = ct.c_uint32
    library.dvbs2_apsk_init.argtypes = [ct.POINTER(Config), ct.c_void_p, ct.c_uint,
                                       ct.c_bool, ct.c_uint32, PTR, ct.c_uint32]
    library.dvbs2_apsk_init.restype = ct.c_bool
    library.dvbs2_apsk_frame.argtypes = [ct.POINTER(Config), PTR, ct.POINTER(Stream)]
    library.dvbs2_apsk_frame.restype = ct.c_bool
    for modulation in (16, 32):
        build = getattr(library, f'iq{modulation}native8_build')
        build.argtypes, build.restype = [ct.POINTER(Tables), ct.c_uint], ct.c_bool
        getattr(library, f'iq{modulation}native8_kernel_c').argtypes = [PTR, PTR, ct.c_uint32, PTR, ct.POINTER(Tables)]
    library.iq32keys_history.argtypes = [PTR, PTR]
    library.iq32keys_encode.argtypes = [PTR, PTR, ct.c_uint32, PTR]
    library.iq32offsets_encode.argtypes = [PTR, PTR, ct.c_uint32]
    library.iq32offsets_from_native.argtypes = [PTR, PTR, ct.c_uint32, PTR]
    library.iq32offsets_from_native_mla.argtypes = [PTR, PTR, ct.c_uint32, PTR]
    library.iq32offsets_kernel_c.argtypes = [PTR, PTR, ct.c_uint32, PTR, ct.POINTER(Tables)]
    library.dvbs2_apsk_fast_init.argtypes = [ct.POINTER(Fast), ct.POINTER(Config), PTR, ct.c_uint32, ct.c_uint]
    library.dvbs2_apsk_fast_init.restype = ct.c_bool
    library.dvbs2_apsk_frame_fast.argtypes = [ct.POINTER(Config), ct.POINTER(Fast), PTR, ct.POINTER(Stream)]
    library.dvbs2_apsk_frame_fast.restype = ct.c_bool


def encode_fec(library, index):
    bb = frame_words(oracle.dual_spi.bbframe(index, gcs=True, rate=oracle.RATE))
    before = bb.copy()
    storage = np.full(2027+2, CANARY, np.uint32)
    fec = storage[1:-1]
    library.test_fec(ptr(bb), ptr(fec))
    assert storage[0] == storage[-1] == CANARY
    np.testing.assert_array_equal(bb, before)
    expected = frame_words(oracle.fec_bits(index))
    np.testing.assert_array_equal(fec[:2025], expected[:2025])
    return fec.copy()


def mapped_stream(library, modulation, fecs, *, ring=0, cancel=0, fast=False):
    code = library.test_code()
    words = library.dvbs2_apsk_arena_words(code, modulation, True)
    gold = np.full(words+2, CANARY, np.uint32)
    config = Config()
    assert library.dvbs2_apsk_init(ct.byref(config), code, modulation, True, 0, ptr(gold[1:-1]), words)
    assert config.frame_symbols == oracle.FRAME_SYMBOLS[modulation]
    state, fast_arena = Fast(), np.full(FAST_WORDS+2, CANARY, np.uint32)
    if fast:
        assert library.dvbs2_apsk_fast_init(ct.byref(state), ct.byref(config), ptr(fast_arena[1:-1]), FAST_WORDS, 0)
    original_config, original_gold, original_fast = bytes(config), gold.copy(), fast_arena.copy()
    symbols = len(fecs)*config.frame_symbols
    count = ring or (symbols//oracle.SYMBOLS_PER_WORD[modulation]+1)
    storage = np.full(count+2, CANARY, np.uint32)
    output = storage[1:-1]
    stream = Stream(out=ptr(output), mask=ring-1 if ring else 0xffffffff)
    published, calls, callback_errors = [], [], []

    def service(context, n):
        try:
            previous = calls[-1] if calls else 0
            assert 0 <= n-previous <= 64
            published.extend(int(output[j & stream.mask]) for j in range(previous, n))
            calls.append(n)
            return not cancel or n < cancel
        except Exception as exc:
            callback_errors.append(repr(exc))
            return False

    callback = SERVICE(service)
    stream.service = callback
    completed = 0
    for fec in fecs:
        before = fec.copy()
        if fast:
            ok = library.dvbs2_apsk_frame_fast(ct.byref(config), ct.byref(state), ptr(fec), ct.byref(stream))
        else:
            ok = library.dvbs2_apsk_frame(ct.byref(config), ptr(fec), ct.byref(stream))
        np.testing.assert_array_equal(fec, before)
        assert not callback_errors, callback_errors
        if not ok:
            assert cancel and stream.n >= cancel
            break
        completed += 1
    assert completed == len(fecs) if not cancel else completed < len(fecs)
    assert calls[0] == 0 and calls[-1] == stream.n
    assert storage[0] == storage[-1] == CANARY
    np.testing.assert_array_equal(gold, original_gold)
    np.testing.assert_array_equal(fast_arena, original_fast)
    assert bytes(config) == original_config
    assert len(published) == stream.n
    if not ring:
        np.testing.assert_array_equal(output[:stream.n], published)
        assert np.all(output[stream.n:] == CANARY)
    return np.asarray(published, np.uint32), stream.fill


def shaped_stream(library, modulation, words, tables, partition, *, offsets=False, fused=False, mla=False):
    history_count = oracle.RETAINED_WORDS[modulation]
    pilot = 0 if modulation == 16 else 6 | 6 << 5 | 6 << 16 | 6 << 21
    combined = np.concatenate([np.full(history_count, pilot, np.uint32), words])
    output_factor = 8 if modulation == 16 else 4
    storage = np.full(len(words)*output_factor+2, CANARY, np.uint32)
    for start in range(0, len(words), partition):
        count = min(partition, len(words)-start)
        history = combined[start:start+history_count]
        destination = ptr(storage[1+output_factor*start:])
        if offsets:
            key_storage = np.full(5+2*count+2, CANARY, np.uint32)
            keys = key_storage[1:-1]
            if fused:
                convert = library.iq32offsets_from_native_mla if mla else library.iq32offsets_from_native
                convert(ptr(keys), ptr(words[start:]), count, ptr(history))
            else:
                library.iq32keys_history(ptr(keys), ptr(history))
                library.iq32keys_encode(ptr(keys[5:]), ptr(words[start:]), count, ptr(history[-1:]))
                library.iq32offsets_encode(ptr(keys), ptr(keys), len(keys))
            library.iq32offsets_kernel_c(destination, ptr(keys[5:]), count, ptr(keys), ct.byref(tables))
            assert key_storage[0] == key_storage[-1] == CANARY
        else:
            kernel = getattr(library, f'iq{modulation}native8_kernel_c')
            kernel(destination, ptr(words[start:]), count, ptr(history), ct.byref(tables))
    assert storage[0] == storage[-1] == CANARY
    np.testing.assert_array_equal(combined[history_count:], words)
    return raw_bus(storage[1:-1])


def main():
    with tempfile.TemporaryDirectory(prefix='apsk-fec-native-') as directory:
        temp, source = Path(directory), ROOT/'firmware/src'
        bridge, library = temp/'bridge.c', temp/'pipeline.so'
        bridge.write_text('#include "dvbs2_apsk.h"\n#include "dvbs2_codes.h"\n'
                          'const dvbs2_code_t *test_code(void){return &DVBS2_CODES[S2_N5_6];}\n'
                          'unsigned test_cfg_size(void){return sizeof(dvbs2_apsk_cfg_t);}\n'
                          'unsigned test_stream_size(void){return sizeof(dvbs2_apsk_stream_t);}\n'
                          'void test_fec(const uint32_t *bb,uint32_t *fe){\n'
                          'dvbs2_init(test_code(),true);dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);\n'
                          'dvbs2_frame_begin_into(bb,fe);dvbs2_frame_ldpc_into(fe);}\n')
        files = ('dvbs2.c', 'iqgen.c', 'dvbs2_apsk.c', 'iq16native8.c',
                 'iq32native8.c', 'iq32keys.c', 'iq32offsets.c', 'iq32offsets_native.c', 'iq32offsets_mla.c')
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        *fwsrc.INCLUDES, *(str(fwsrc.path(name)) for name in files), str(bridge), '-o', str(library)], check=True)
        lib = ct.CDLL(str(library)); configure(lib)
        frames = [encode_fec(lib, index) for index in range(3)]
        # Random access ensures the BB/FEC oracle is not accidentally tied to a
        # sequential fixture cursor; it does not introduce a discontinuous PL stream.
        encode_fec(lib, 123)
        records = []
        for modulation in (16, 32):
            points = np.concatenate([oracle.frame_points(index, modulation) for index in range(3)])
            expected_words = oracle.native_words(points, modulation)
            expected_bus = oracle.shaped_bus(points, modulation)
            for fast in (False, True):  # original PL mapper and the A3 table-driven one
                for ring in (0, 256):
                    actual, fill = mapped_stream(lib, modulation, frames, ring=ring, fast=fast)
                    np.testing.assert_array_equal(actual, expected_words)
                    assert fill == len(points) % oracle.SYMBOLS_PER_WORD[modulation]
                cancelled, _ = mapped_stream(lib, modulation, frames, ring=256, cancel=128, fast=fast)
                assert len(cancelled) == 128
                np.testing.assert_array_equal(cancelled, expected_words[:128])
            sizes = (2048, 16384, 16384) if modulation == 16 else (9826,)*3
            guarded = [np.full(size+2, CANARY, np.uint32) for size in sizes]
            tables = Tables(*(ptr(array[1:-1]) for array in guarded))
            assert getattr(lib, f'iq{modulation}native8_build')(ct.byref(tables), 270 if modulation == 16 else 2)
            table_images = [array.copy() for array in guarded]
            for partition in (1, 7, 256, len(expected_words)):
                actual_bus = shaped_stream(lib, modulation, expected_words, tables, partition)
                np.testing.assert_array_equal(actual_bus, expected_bus)
                if modulation == 32:
                    offset_bus = shaped_stream(lib, modulation, expected_words, tables, partition, offsets=True)
                    np.testing.assert_array_equal(offset_bus, expected_bus)
                    fused_bus = shaped_stream(lib, modulation, expected_words, tables, partition, offsets=True, fused=True)
                    np.testing.assert_array_equal(fused_bus, expected_bus)
                    mla_bus = shaped_stream(lib, modulation, expected_words, tables, partition,
                                            offsets=True, fused=True, mla=True)
                    np.testing.assert_array_equal(mla_bus, expected_bus)
            for array, before in zip(guarded, table_images):
                assert array[0] == array[-1] == CANARY
                np.testing.assert_array_equal(array, before)
            records.append(dict(modulation=modulation, frames=3, symbols=len(points),
                                native_words=len(expected_words), stream_fill=fill,
                                native_crc=zlib.crc32(expected_words.tobytes()),
                                gpio_halfwords=len(expected_bus), gpio_crc=zlib.crc32(expected_bus.tobytes()),
                                partitions=[1,7,256,len(expected_words)], progressive_ring_words=256,
                                cancellation_words=len(cancelled), offset_path=modulation == 32,
                                fused_native_offset_path=modulation == 32, fast_pl_mapper=True,
                                multiply_form_offset_path=modulation == 32))
    print(json.dumps(dict(passed=True, real_gcs_fec_indices=[0,1,2,123],
                          fec_crc=oracle.first_fec_crcs(), cases=records)))


if __name__ == '__main__':
    main()
