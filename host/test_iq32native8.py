"""Independent native32APSK alphabet/FIR/table/component checks, no RF claims."""
from __future__ import annotations
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PTR = ctypes.POINTER(ctypes.c_uint32)
ROWS = 17**3
CANARY = 0xdeadbeef
PILOT = 6 | 6 << 5 | 6 << 16 | 6 << 21
TAPS = np.array([6658,-48202,6455,124501,-58274,-258197,258053,1108522,
                 1108522,258053,-258197,-58274,124501,6455,-48202,6658], np.int64)
GAMMA = ((284,527),(272,487),(264,464),(254,433),(253,430))


class Tables(ctypes.Structure):
    _fields_ = [(name, PTR) for name in ('old', 'middle', 'future')]


def ptr(a):
    return a.ctypes.data_as(PTR)


def crc(a):
    return zlib.crc32(a.astype(a.dtype.newbyteorder('<')).tobytes())


def levels(rate):
    g1, g2 = GAMMA[rate]
    floating = np.array([0, 100/(g2*np.sqrt(2)), g1/g2*np.sin(np.pi/12),
                         g1/g2/np.sqrt(2), g1/g2*np.cos(np.pi/12),
                         np.sin(np.pi/8), 1/np.sqrt(2), np.cos(np.pi/8), 1])
    # Independently reproduce the documented integer input-coordinate policy;
    # compare against trigonometry separately rather than sharing C constants.
    diagonal, sine, cosine = [int(round(x*(1<<30))) for x in
                              (1/np.sqrt(2), np.sin(np.pi/12), np.cos(np.pi/12))]
    quantized = np.array([0, (diagonal*100+g2//2)//g2,
                          (sine*g1+g2//2)//g2, (diagonal*g1+g2//2)//g2,
                          (cosine*g1+g2//2)//g2,
                          round(np.sin(np.pi/8)*(1<<30)), diagonal,
                          round(np.cos(np.pi/8)*(1<<30)), 1<<30], np.int64)
    floating = np.concatenate([floating, -floating[1:]])
    quantized = np.concatenate([quantized, -quantized[1:]])
    assert np.max(np.abs(quantized/(1<<30)-floating)) < 1e-9
    assert len(set(quantized)) == 17
    return floating, quantized


def rows(chunk, scaled):
    key = np.arange(ROWS, dtype=np.int64)
    digits = np.stack([key % 17, key//17 % 17, key//289], axis=1)
    output = np.zeros((ROWS, 4), np.int64)
    for r in range(4):
        for j in range(3):
            age = 7+r//2-(3*chunk+j)
            if 0 <= age < 8:
                output[:,r] += scaled[digits[:,j], 2*age+r%2]
    return ((output+128)>>8).astype('<i2')


def decode(words, axis):
    return ((words[:,None] >> (16*axis+5*np.arange(2))) & 31).ravel()


def expected(words, previous, scaled, floating):
    previous = np.full(4, PILOT, np.uint32) if previous is None else previous
    axes, ideal_axes = [], []
    for axis in (0,1):
        codes = decode(np.concatenate([previous, words]), axis)
        assert np.max(codes, initial=0) < 17
        windows = np.lib.stride_tricks.sliding_window_view(codes, 10)[::2,1:]
        output = np.zeros((len(words),4), np.int64)
        ideal = np.zeros((len(words),4), np.float64)
        for chunk in range(3):
            partial = np.zeros_like(output)
            for r in range(4):
                for j in range(3):
                    age=7+r//2-(3*chunk+j)
                    if 0 <= age < 8:
                        code = windows[:,3*chunk+j]
                        partial[:,r] += scaled[code,2*age+r%2]
                        ideal[:,r] += floating[code]*TAPS[2*age+r%2]/256
            output += (partial+128)>>8
        axes.append(output.ravel())
        ideal_axes.append(ideal.ravel())
    unsigned = [x.astype(np.uint16).astype(np.uint32) for x in axes]
    raw = np.stack([unsigned[0][::2] | unsigned[0][1::2]<<16,
                    unsigned[1][::2] | unsigned[1][1::2]<<16],axis=1).ravel()
    return raw, np.concatenate(axes), np.concatenate(ideal_axes)


def packed(raw):
    pair = raw.reshape(-1,2)
    return np.stack([(pair[:,0]&0x3fff) | (pair[:,1]&0x3fff)<<16,
                     (pair[:,0]>>16&0x3fff) | (pair[:,1]&0x3fff0000)],axis=1).ravel()


def execute(lib, words, previous, tables, partition):
    output = np.full(4*len(words)+2, CANARY, np.uint32)
    original = np.full(4, PILOT, np.uint32) if previous is None else previous
    all_words = np.concatenate([original, words])
    for start in range(0,len(words),partition):
        history = None if previous is None and start == 0 else all_words[start:start+4]
        count = min(partition,len(words)-start)
        lib.iq32native8_kernel_c(ptr(output[1+4*start:]), ptr(words[start:]),count,
                                None if history is None else ptr(history),ctypes.byref(tables))
    assert output[0] == output[-1] == CANARY
    return output[1:-1]


def main():
    source=ROOT/'firmware/src'
    coefficient_sha=hashlib.sha256(TAPS.astype('<i4').tobytes()).hexdigest()
    assert coefficient_sha in (source/'iq32native8.h').read_text()
    rng=np.random.default_rng(987123)
    checks=0
    # Exhaustive radix key identity and paired-halfword carry check. The proof
    # that each intermediate is <=4912 excludes a cross-lane carry entirely.
    values=np.arange(ROWS,dtype=np.uint32)
    for counterpart in (values[::-1],rng.permutation(values),values):
        lanes=values | counterpart<<16
        a=(values%17) | (counterpart%17)<<16
        b=(values//17%17) | (counterpart//17%17)<<16
        c=(values//289) | (counterpart//289)<<16
        assert np.array_equal(a+17*(b+17*c),lanes)
        checks+=len(values)
    records=[]
    with tempfile.TemporaryDirectory(prefix='iq32native8-') as temp:
        library=Path(temp)/'native.so'
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        f'-I{source}',str(source/'iq32native8.c'),'-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library))
        lib.iq32native8_build.argtypes=[ctypes.POINTER(Tables),ctypes.c_uint]
        lib.iq32native8_build.restype=ctypes.c_bool
        lib.iq32native8_fill.argtypes=[PTR,ctypes.c_uint32,PTR]
        lib.iq32native8_kernel_c.argtypes=[PTR,PTR,ctypes.c_uint32,PTR,ctypes.POINTER(Tables)]
        lib.iq32native8_kernel_c(None,None,0,None,None)
        words=np.empty(8192,np.uint32)
        seed=ctypes.c_uint32(0x1234abcd)
        lib.iq32native8_fill(ptr(words),len(words),ctypes.byref(seed))
        # Fixture point generator is checked against each4/12/16 ring and
        # angular geometry; this is deliberately not a DVB bit-label check.
        for rate in range(5):
            floating, quantized=levels(rate)
            points=floating[decode(words,0)]+1j*floating[decode(words,1)]
            g1,g2=GAMMA[rate]
            rings=np.array([100/g2,g1/g2,1])
            distance=np.min(abs(abs(points[:,None])-rings),axis=1)
            assert np.max(distance) < 1e-12
            assert len(set(np.round(points,12))) == 32
            assert all(np.any(abs(abs(points)-r)<1e-12) for r in rings)
            scaled=(quantized[:,None]*TAPS+(1<<29))>>30
            arrays=[np.full(2*ROWS+2,CANARY,np.uint32) for _ in range(3)]
            tables=Tables(*(ptr(a[1:-1]) for a in arrays))
            assert lib.iq32native8_build(ctypes.byref(tables),rate)
            expected_rows=[rows(c,scaled) for c in range(3)]
            for a,r in zip(arrays,expected_rows):
                assert a[0] == a[-1] == CANARY
                assert np.array_equal(a[1:-1].view('<i2').reshape(ROWS,4),r)
                checks+=r.size
            before=[a.copy() for a in arrays]
            assert not lib.iq32native8_build(ctypes.byref(tables),5)
            assert not lib.iq32native8_build(ctypes.byref(tables),0xffffffff)
            assert not lib.iq32native8_build(None,rate)
            assert not lib.iq32native8_build(ctypes.byref(Tables(None,tables.middle,tables.future)),rate)
            assert all(np.array_equal(a,b) for a,b in zip(arrays,before))
            # Symmetry is checked without assuming it in the actual native
            # three-table consumer. A future memory option could drop one.
            keys=np.arange(ROWS)
            reverse=(keys//289)+17*(keys//17%17)+289*(keys%17)
            assert np.array_equal(expected_rows[0],expected_rows[2][reverse,::-1])
            assert np.array_equal(expected_rows[1],expected_rows[1][reverse,::-1])
            low=np.array([r.min(axis=0) for r in expected_rows]).sum(axis=0)
            high=np.array([r.max(axis=0) for r in expected_rows]).sum(axis=0)
            bound=int(max(np.max(abs(low)),np.max(abs(high))))
            assert bound < 8192
            peak_error=0.0
            for history in (None, rng.integers(0,17,(4,2,2),dtype=np.uint32)):
                previous=None if history is None else sum(history[:,j,a]<<(16*a+5*j) for j in range(2) for a in range(2))
                raw,axes,ideal=expected(words,previous,scaled,floating)
                peak_error=max(peak_error,float(np.max(abs(axes-ideal))))
                assert peak_error <= 1.5+8/512+1e-4
                for partition in (1,2,3,4,5,7,16,31,127,256,len(words)):
                    got=execute(lib,words,previous,tables,partition)
                    assert np.array_equal(got,raw)
                    checks+=len(raw)
                noisy=words|rng.integers(0,1<<32,len(words),dtype=np.uint32)&0xfc00fc00
                assert np.array_equal(execute(lib,noisy,previous,tables,31),raw)
            # Every individual valid coordinate and unit-pilot transitions.
            for code in range(17):
                test=np.full(17,code | code<<5 | code<<16 | code<<21,np.uint32)
                test[7:10]=PILOT
                raw,_,_=expected(test,None,scaled,floating)
                assert np.array_equal(execute(lib,test,None,tables,3),raw)
                checks+=len(raw)
            raw,_,_=expected(words,None,scaled,floating)
            records.append({'rate_index':rate,'gamma100':list(GAMMA[rate]),
                            'table_crcs':[crc(a[1:-1]) for a in arrays],
                            'raw_crc':crc(raw),'packed_crc':crc(packed(raw)),
                            'headroom_bound_lsb':bound,'max_float_q8_error_lsb':peak_error})
    result={'status':'PASS','scope':'native component, no device/RF/fullmodem claim',
            'coefficient_sha256':coefficient_sha,'table_bytes':3*ROWS*8,
            'symmetry_table_option_bytes':2*ROWS*8,'input_crc':crc(words),
            'fixture_blocks':len(words),'fixture_symbols':2*len(words),
            'checks':checks,'rates':records}
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
