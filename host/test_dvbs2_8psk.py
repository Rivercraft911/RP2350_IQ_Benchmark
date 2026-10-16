"""Native full-PLFRAME tests for the experimental two-plane 8PSK mapper.

All DVB-S2 8PSK normal/short rates, pilots, independent scalar constellation/Gold
reference, frame-partial groups, odd initial fill, ring wrap and explicit flush.
Optional --fixtures DIR also checks pinned GNU Radio FEC/complex-output fixtures.
No Pico, shared firmware build or RF hardware is accessed.
"""
from __future__ import annotations
import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference/dvbs2"))
import dvbs2 as ref
import fwsrc

PTR = ctypes.POINTER(ctypes.c_uint32)
CANARY = 0x9ad4c781
RATES = ("3/5", "2/3", "3/4", "5/6", "8/9", "9/10")
MODCOD = dict(zip(RATES, range(12, 18)))
ANGLES = np.pi / 4 * np.array([1, 0, 4, 5, 2, 7, 3, 6])

class Input(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint32)]

class Stream(ctypes.Structure):
    _fields_ = [("out", ctypes.POINTER(Input)), ("mask", ctypes.c_uint32), ("n", ctypes.c_uint32),
                ("acc_ai", ctypes.c_uint32), ("acc_aq", ctypes.c_uint32),
                ("acc_bi", ctypes.c_uint32), ("acc_bq", ctypes.c_uint32), ("fill", ctypes.c_uint32)]

def scalar_frame(fec, rate, short, pilots, gold=0):
    columns = np.asarray(fec, np.uint8).reshape(3, -1)
    if rate == "3/5": columns = columns[::-1]
    labels = columns[0] * 4 + columns[1] * 2 + columns[2]
    data = np.exp(1j * ANGLES[labels])
    chunks = []
    for j in range(0, len(data), 1440):
        chunks.append(data[j:j+1440])
        if pilots and j+1440 < len(data): chunks.append(np.full(36, np.exp(1j*np.pi/4)))
    body = np.concatenate(chunks)
    body *= np.exp(1j*np.pi/2*ref.pl_scrambling_sequence(len(body), gold))
    hi, hq = ref.plheader(MODCOD[rate], short, pilots)
    return np.concatenate([ref.symbols(hi, hq), body]) * np.exp(1j*np.pi/8)

def sign_planes(points):
    bits = []
    for coordinate in (points.real, points.imag):
        sign = (coordinate < 0).astype(np.uint32)
        bits.extend([sign, sign ^ (abs(coordinate) < .7).astype(np.uint32)])
    return bits

def packed_groups(points, flush=False):
    if flush and len(points) % 16:
        points = np.concatenate([points, np.full(16-len(points)%16, np.exp(1j*np.pi/8))])
    count = len(points)//16*16
    bits = sign_planes(points[:count])
    shifts = np.arange(16, dtype=np.uint32)
    group = np.zeros((count//16, 2), np.uint32)
    for p, values in enumerate(bits):
        group[:, p%2] |= (values.reshape(-1,16) << shifts).sum(axis=1,dtype=np.uint32) << (16*(p//2))
    return group

def frame_words(bits):
    packed = np.packbits(bits)
    packed = np.pad(packed, (0, (-len(packed)) % 4))
    words = packed.view('>u4').astype(np.uint32)
    return np.concatenate([words, np.zeros(2, np.uint32)])

def check_stream(stream, memory, points, ring_mask):
    expected = packed_groups(points)
    assert stream.n == len(points)//16 and stream.fill == len(points)%16
    start = max(0, len(expected)-ring_mask-1) if ring_mask != 0xffffffff else 0
    got = np.ctypeslib.as_array(ctypes.cast(memory, PTR), shape=(2*(ring_mask+1),)) if ring_mask != 0xffffffff else \
          np.ctypeslib.as_array(ctypes.cast(memory, PTR), shape=(2*max(1,len(expected)),))
    got = got.reshape(-1,2)
    for k in range(start,len(expected)):
        np.testing.assert_array_equal(got[k & ring_mask],expected[k])
    tail = sign_planes(points[len(expected)*16:])
    accum = [stream.acc_ai, stream.acc_bi, stream.acc_aq, stream.acc_bq]
    for plane, values in zip(accum,tail):
        assert plane == sum(int(v)<<j for j,v in enumerate(values))

def run_case(lib, index, rate, short, pilots, fecs, gold=0, prefix=0, ring=False, ring_groups=256):
    code = lib.test_code(index)
    need = lib.dvbs2_8psk_arena_words(code,pilots)
    assert 0 < need <= 1384
    arena = (ctypes.c_uint32*(need+2))();arena[0]=arena[-1]=CANARY
    ap = ctypes.cast(ctypes.byref(arena,4),PTR)
    cfg = (ctypes.c_uint64*32)() # suitably aligned opaque storage, exact C size checked below
    assert lib.dvbs2_8psk_init(cfg,code,pilots,gold,ap,need)
    frame_len = (5598 if pilots else 5490) if short else (22194 if pilots else 21690)
    rotations = ref.pl_scrambling_sequence(frame_len-90,gold)
    data_positions = np.arange(len(rotations))%1476 < 1440 if pilots else np.ones(len(rotations),bool)
    stored = np.concatenate([rotations[data_positions],rotations[~data_positions]])
    gold_words = np.ctypeslib.as_array(ap,shape=(need,)).reshape(-1,2)
    gold_bits = ((gold_words[:,:,None] >> np.arange(32,dtype=np.uint32)) & 1).transpose(0,2,1).reshape(-1,2)
    np.testing.assert_array_equal(gold_bits[:len(stored)],np.stack([stored&1,stored>>1],axis=1))
    assert not np.any(gold_bits[len(stored):])
    groups = (prefix+len(fecs)*frame_len+15)//16
    size = ring_groups if ring else groups+1
    raw = (Input*(size+2))()
    raw[0] = raw[-1] = Input(CANARY,CANARY)
    memory = ctypes.cast(ctypes.byref(raw,ctypes.sizeof(Input)),ctypes.POINTER(Input))
    stream = Stream(out=memory,mask=size-1 if ring else 0xffffffff)
    points = np.full(prefix,np.exp(1j*np.pi/8))
    if prefix: stream.fill=prefix;stream.acc_bq=(1<<prefix)-1
    for fec in fecs:
        data = frame_words(fec)
        original = data.copy()
        lib.dvbs2_8psk_frame(cfg,data.ctypes.data_as(PTR),ctypes.byref(stream))
        np.testing.assert_array_equal(data,original)
        points = np.concatenate([points,scalar_frame(fec,rate,short,pilots,gold)])
        check_stream(stream,memory,points,stream.mask)
    lib.dvbs2_8psk_flush(ctypes.byref(stream))
    if len(points)%16: points=np.concatenate([points,np.full(16-len(points)%16,np.exp(1j*np.pi/8))])
    check_stream(stream,memory,points,stream.mask)
    assert (raw[0].a,raw[0].b,raw[-1].a,raw[-1].b)==(CANARY,)*4
    assert arena[0] == arena[-1] == CANARY
    return dict(index=index,rate=rate,short=short,pilots=pilots,gold=gold,prefix=prefix,ring=ring,
                frames=len(fecs),symbols=len(points),packed_crc=zlib.crc32(packed_groups(points).tobytes()))

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--fixtures',type=Path);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='dvbs2-8psk-native-') as temp:
        temp=Path(temp);bridge=temp/'bridge.c';library=temp/'mapper.so';src=ROOT/'firmware/src'
        bridge.write_text('#include "dvbs2_8psk.h"\n#include "dvbs2_codes.h"\n'
                          'const dvbs2_code_t *test_code(unsigned i){return i<N_DVBS2_CODES?&DVBS2_CODES[i]:0;}\n'
                          'unsigned test_cfg_size(void){return sizeof(dvbs2_8psk_cfg_t);}\n'
                          'unsigned test_stream_size(void){return sizeof(dvbs2_8psk_stream_t);}\n')
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',*fwsrc.INCLUDES,
                        str(fwsrc.path('dvbs2_8psk.c')),str(bridge),'-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library));lib.test_code.argtypes=[ctypes.c_uint];lib.test_code.restype=ctypes.c_void_p
        lib.dvbs2_8psk_arena_words.argtypes=[ctypes.c_void_p,ctypes.c_bool];lib.dvbs2_8psk_arena_words.restype=ctypes.c_uint32
        lib.dvbs2_8psk_init.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_bool,ctypes.c_uint32,PTR,ctypes.c_uint32]
        lib.dvbs2_8psk_init.restype=ctypes.c_bool
        lib.dvbs2_8psk_frame.argtypes=[ctypes.c_void_p,PTR,ctypes.POINTER(Stream)]
        lib.dvbs2_8psk_flush.argtypes=[ctypes.POINTER(Stream)]
        assert lib.test_cfg_size()<=256 and lib.test_stream_size()==ctypes.sizeof(Stream)
        rng=np.random.default_rng(20261015);cases=[]
        configs=[]
        for short in (False,True):
            for rate in RATES:
                if short and rate=='9/10':continue
                index=ref.RATES.index(rate)+(11 if short else 0)
                # FEC validity was independently checked against gr-dtv; mapping also
                # accepts arbitrary FE data, so random complete bit vectors stress labels.
                fec=rng.integers(0,2,16200 if short else 64800,dtype=np.uint8)
                for pilots in (False,True):
                    configs.append((index,rate,short,pilots))
                    for prefix in (0,1):
                        cases.append(run_case(lib,index,rate,short,pilots,[fec]*8,prefix=prefix))
                    cases.append(run_case(lib,index,rate,short,pilots,[fec]*3,ring=True))
        extra=[]
        for gold in (1,99999,262142):
            fec=rng.integers(0,2,64800,dtype=np.uint8)
            extra.append(run_case(lib,8,'5/6',False,True,[fec]*3,gold=gold))
        # Exercise each packed carry alignment directly, including the pair that
        # straddles the end of the smallest useful two-group ring.
        for rate,index in (('3/5',4),('5/6',8)):
            fec=rng.integers(0,2,64800,dtype=np.uint8)
            for prefix in range(16):
                extra.append(run_case(lib,index,rate,False,bool(prefix&1),[fec],
                                      prefix=prefix,ring=True,ring_groups=2))
        rejected=[]
        for index in (0,1,2,3,7,11,12,13,14,18):
            assert lib.dvbs2_8psk_arena_words(lib.test_code(index),True)==0;rejected.append(index)
        cfg=(ctypes.c_uint64*32)();a=(ctypes.c_uint32*1384)();code=lib.test_code(8)
        assert not lib.dvbs2_8psk_init(cfg,code,True,0,a,1383)
        assert not lib.dvbs2_8psk_init(cfg,code,True,262143,a,1384)
        fixture_cases=[]
        if args.fixtures:
            manifest=json.loads((args.fixtures/'results.json').read_text())
            for result in manifest['results']:
                name=result['name']
                if not name.startswith('3bit_'):continue
                _,num,den,size,pil,gold=name.split('_');rate=num+'/'+den;short=size=='short';pilots=bool(int(pil[3:]));gold=int(gold[4:])
                index=ref.RATES.index(rate)+(11 if short else 0)
                fec=np.fromfile(args.fixtures/name/'fec.u8',np.uint8).reshape(result['frames'],-1)
                gr=np.fromfile(args.fixtures/name/'pl.f32',np.float32).reshape(-1,2)
                expected=np.concatenate([scalar_frame(x,rate,short,pilots,gold) for x in fec])
                actual=(gr[:,0]+1j*gr[:,1])*np.exp(1j*np.pi/8)
                assert float(abs(expected-actual).max())<3e-7
                fixture_cases.append(run_case(lib,index,rate,short,pilots,fec,gold=gold))
    summary=dict(configurations=len(configs),native_cases=len(cases)+len(extra),
                 native_frames=sum(c['frames'] for c in cases+extra),all_partial_fills=list(range(16)),
                 rejected_codes=rejected,gr_fixture_cases=len(fixture_cases),
                 gr_fixture_frames=sum(c['frames'] for c in fixture_cases),fixtures=fixture_cases)
    print(json.dumps(summary))

if __name__=='__main__':main()
