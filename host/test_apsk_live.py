"""Actual portable mapper/shaper C, reordered block history, and host checks."""
from __future__ import annotations
import ctypes as ct
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np

import apsk_live_trial as live
import iq32out_trial as reference
from test_iq32mapkeys import State, U8P, U32P, p8, p32


class Tables(ct.Structure):
    _fields_ = [('old',U32P),('middle',U32P),('future',U32P)]


def bus_words(output):
    values = output.view('<i2').reshape(-1,4).astype(np.int64)
    i,q = values[:,:2].ravel()&0x3fff, values[:,2:].ravel()&0x3fff
    return np.stack([i|0x4000,q],axis=1).ravel().astype('<u2')


def main():
    source = reference.iq.ROOT/'firmware/src'
    with tempfile.TemporaryDirectory(prefix='apsk-live-native-') as temp:
        path = Path(temp)/'native.so'
        sources = ('iq16maplabels.c','iq32mapkeys.c','iq32mapkeys_pair.c',
                   'iq32keys.c','iq32native8.c','iq16native8.c','iq32offsets.c')
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        f'-I{source}',*[str(source/name) for name in sources],'-o',str(path)],check=True)
        lib = ct.CDLL(str(path))
        lib.iq16maplabels_encode.argtypes = [U32P,U8P,ct.c_uint32]
        lib.iq32mapkeys_init.argtypes = [ct.POINTER(State),U8P]
        lib.iq32mapkeys_encode.argtypes = [U32P,U8P,ct.c_uint32,ct.POINTER(State)]
        lib.iq32mapkeys_pair_build.argtypes = [U32P]
        for name in ('iq32mapkeys_pair_encode','iq32mapkeys_pair_canonical'):
            getattr(lib,name).argtypes = [U32P,U8P,ct.c_uint32,ct.POINTER(State),U32P]
        for name in ('iq32native8_build','iq16native8_build'):
            getattr(lib,name).argtypes = [ct.POINTER(Tables),ct.c_uint]
            getattr(lib,name).restype = ct.c_bool
        for name in ('iq32keys_kernel_c','iq16native8_kernel_c','iq32offsets_kernel_c'):
            getattr(lib,name).argtypes = [U32P,U32P,ct.c_uint32,U32P,ct.POINTER(Tables)]
        lib.iq16maplabels_encode(None,None,0)
        # Every four-label packing combination, against independent polar
        # geometry + coordinate matching rather than the leaf's lookup values.
        labels = ((np.arange(65536,dtype=np.uint32)[:,None] >> (4*np.arange(4)))&15).astype('u1').ravel()
        points = live.ref16.constellation(live.ref16.GAMMA)[labels]
        _,expected = live.ref16.encode_points(points,live.ref16.levels(live.ref16.GAMMA)[0])
        packed = np.full(65536+2,0xa75c12e9,np.uint32)
        lib.iq16maplabels_encode(p32(packed[1:]),p8(labels),65536)
        assert packed[0] == packed[-1] == 0xa75c12e9
        np.testing.assert_array_equal(packed[1:-1],expected)
        pairs = np.empty(4096,np.uint32)
        lib.iq32mapkeys_pair_build(p32(pairs))
        assert reference.zlib.crc32(pairs.tobytes()) == 2680011457
        cases = 0; profiles = []
        nblocks = 48
        orders = [np.arange(nblocks),np.r_[np.arange(0,nblocks,2),np.arange(1,nblocks,2)],
                  np.random.default_rng(20261015).permutation(nblocks)]
        for modulation,mapper in ((16,0),(32,0),(32,1),(32,2),(32,3)):
            mapper_table = pairs << 3 if mapper == 3 else pairs
            mapper_crc = 3614349055 if mapper == 3 else 2680011457
            assert reference.zlib.crc32(mapper_table.tobytes()) == mapper_crc
            labels = (reference.iq.m.xorshift32(reference.SEED,live.SOURCE_SYMBOLS)&(modulation-1)).astype('u1')
            history = 8 if modulation == 16 else 7
            padded = np.concatenate([labels[-history:],labels])
            sizes = (2048,16384,16384) if modulation == 16 else (9826,9826,9826)
            arenas = [np.empty(size,np.uint32) for size in sizes]
            tables = Tables(*map(p32,arenas))
            builder = lib.iq16native8_build if modulation == 16 else lib.iq32native8_build
            assert builder(ct.byref(tables),270 if modulation == 16 else 2)
            crcs = live.ref16.TABLE_CRCS if modulation == 16 else reference.TABLE_CRCS
            assert [reference.zlib.crc32(a.tobytes()) for a in arenas] == crcs
            source_words = live.ref16.source_words()[:2048] if modulation == 16 else reference.source_keys()
            period = live.reference_period(modulation)
            for order in orders:
                for sequence in order:
                    start = int(sequence)*live.BLOCK_SYMBOLS % len(labels)
                    prefix,words = (2,258) if modulation == 16 else (5,1029)
                    output_keys = np.full(words+2,0xa75c12e9,np.uint32)
                    if modulation == 16:
                        lib.iq16maplabels_encode(p32(output_keys[1:]),p8(padded[start:]),words)
                        expected_keys = source_words[np.arange(start//4-2,start//4+256)%len(source_words)]
                    else:
                        state = State()
                        lib.iq32mapkeys_init(ct.byref(state),p8(padded[start:start+2]))
                        args = [p32(output_keys[1:]),p8(padded[start+2:]),words,ct.byref(state)]
                        if mapper == 3:
                            lib.iq32mapkeys_encode(p32(output_keys[1:]),p8(padded[start+2:]),5,ct.byref(state))
                            output_keys[1:6] <<= 3
                            state.last <<= 3; state.pair <<= 3
                            lib.iq32mapkeys_pair_canonical(p32(output_keys[6:]),p8(padded[start+7:]),1024,
                                                          ct.byref(state),p32(mapper_table))
                        elif mapper:
                            fn = lib.iq32mapkeys_pair_encode if mapper == 1 else lib.iq32mapkeys_pair_canonical
                            fn(*args,p32(mapper_table))
                        else: lib.iq32mapkeys_encode(*args)
                        expected_keys = source_words[np.arange(start-5,start+1024)%len(source_words)]
                        if mapper == 3: expected_keys = expected_keys << 3
                    assert output_keys[0] == output_keys[-1] == 0xa75c12e9
                    np.testing.assert_array_equal(output_keys[1:-1],expected_keys)
                    output = np.full(2048+2,0xa75c12e9,np.uint32)
                    shape = lib.iq16native8_kernel_c if modulation == 16 else lib.iq32offsets_kernel_c if mapper == 3 else lib.iq32keys_kernel_c
                    shape(p32(output[1:]),p32(output_keys[1+prefix:]),256 if modulation == 16 else 512,
                          p32(output_keys[1:]),ct.byref(tables))
                    assert output[0] == output[-1] == 0xa75c12e9
                    bus = bus_words(output[1:-1]); offset = start*4
                    np.testing.assert_array_equal(bus,period[(offset+np.arange(len(bus)))%len(period)])
                    cases += 1
            assert [reference.zlib.crc32(a.tobytes()) for a in arenas] == crcs
            assert reference.zlib.crc32(mapper_table.tobytes()) == mapper_crc
            assert reference.zlib.crc32(pairs.tobytes()) == 2680011457
            profiles.append(dict(modulation=modulation,mapper=mapper))
        print(json.dumps(dict(native_block_history='passed',independently_ordered_blocks=cases,
                              profiles=profiles,tested_schedules=['sequential','even_then_odd','random'],
                              exact_bus_halfwords_per_block=4096,cyclic_source_wraps=6,
                              exhaustive16_packing_combinations=65536,label_crcs=live.LABEL_CRCS)))
    live.self_test()


if __name__ == '__main__': main()
