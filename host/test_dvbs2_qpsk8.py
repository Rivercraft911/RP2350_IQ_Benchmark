"""QPSK/8PSK shared-plane proof: real FEC, exact tails, ring wraps and continuous FIR.

No Pico/shared build. The two constellations keep one global pi/8 rotation;
VCM BBHEADERs use0x62, with distinct QPSK/8PSK PL MODCODs. No live-rate claim.
"""
from __future__ import annotations
import ctypes as ct
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zlib

import numpy as np
import test_dvbs2_8psk as mapping
import test_iq8quad as pulse
import fwsrc

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'firmware/src'
PTR=ct.POINTER(ct.c_uint32)
Input,Stream=mapping.Input,mapping.Stream
CANARY=0x9ad4c781
U32=0xffffffff


def qpsk_points(words,tail,nsym):
    all_words=list(map(int,words))+([int(tail)] if nsym else [])
    counts=[16]*len(words)+([nsym] if nsym else [])
    i=[1-2*((w>>j)&1) for w,n in zip(all_words,counts) for j in range(n)]
    q=[1-2*((w>>(16+j))&1) for w,n in zip(all_words,counts) for j in range(n)]
    return (np.asarray(i)+1j*np.asarray(q))/np.sqrt(2)*np.exp(1j*np.pi/8)


def make_stream(size,fill=0,cursor=0,linear=False,prefix=None):
    raw=(Input*(size+2))()
    for value in raw:value.a=value.b=CANARY
    out=ct.cast(ct.byref(raw,ct.sizeof(Input)),ct.POINTER(Input))
    s=Stream(out=out,mask=U32 if linear else size-1,n=cursor,fill=fill)
    prefix=np.full(fill,np.exp(1j*np.pi/8)) if prefix is None else prefix
    bits=mapping.sign_planes(prefix)
    s.acc_ai,s.acc_bi,s.acc_aq,s.acc_bq=[sum(int(bit)<<j for j,bit in enumerate(b)) for b in bits]
    return raw,s,prefix


def memory(raw):
    return np.ctypeslib.as_array(ct.cast(raw,PTR),shape=(2*len(raw),)).reshape(-1,2)


def check_stream(raw,s,points,cursor=0):
    expected=mapping.packed_groups(points)
    size=len(raw)-2;view=memory(raw)
    want=np.full((size,2),CANARY,np.uint32)
    for j,group in enumerate(expected):want[(cursor+j)&s.mask]=group
    np.testing.assert_array_equal(view[1:-1],want)
    assert np.all(view[[0,-1]]==CANARY)
    assert s.n==(cursor+len(expected))&U32 and s.fill==len(points)%16
    tail=mapping.sign_planes(points[len(expected)*16:])
    for got,bits in zip((s.acc_ai,s.acc_bi,s.acc_aq,s.acc_bq),tail):
        assert got==sum(int(bit)<<j for j,bit in enumerate(bits))


class SharedModulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='qpsk8-native-');d=Path(cls.temp.name)
        (d/'bridge.c').write_text('#include "dvbs2_qpsk8.h"\n#include "dvbs2_codes.h"\n'
            'const dvbs2_code_t *test_code(unsigned i){return i<N_DVBS2_CODES?&DVBS2_CODES[i]:0;}\n'
            'void test_init(unsigned i,bool pilots){dvbs2_init(&DVBS2_CODES[i],pilots);}\n'
            'unsigned test_stream_size(void){return sizeof(dvbs2_8psk_stream_t);}\n'
            'void test_fec(const uint32_t *bb,uint32_t *fe){dvbs2_frame_begin_into(bb,fe);dvbs2_frame_ldpc_into(fe);}\n')
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',*fwsrc.INCLUDES,
                        *(str(fwsrc.path(n)) for n in ('dvbs2.c','dvbs2_8psk.c','dvbs2_qpsk8.c','iq8quad.c','iqgen.c')),
                        str(d/'bridge.c'),'-o',str(d/'test.so')],check=True)
        cls.lib=ct.CDLL(str(d/'test.so'));lib=cls.lib
        lib.test_code.argtypes=[ct.c_uint];lib.test_code.restype=ct.c_void_p
        lib.test_init.argtypes=[ct.c_uint,ct.c_bool];lib.test_fec.argtypes=[PTR,PTR]
        lib.test_stream_size.restype=ct.c_uint
        assert lib.test_stream_size()==ct.sizeof(Stream)
        lib.dvbs2_qpsk8_append.argtypes=[ct.POINTER(Stream),PTR,ct.c_uint32,ct.c_uint32,ct.c_uint]
        lib.dvbs2_qpsk8_append.restype=ct.c_bool
        lib.dvbs2_qpsk8_frame_from.argtypes=[PTR,ct.POINTER(Stream),PTR,ct.c_uint32]
        lib.dvbs2_qpsk8_frame_from.restype=ct.c_bool
        lib.dvbs2_map_set_kernel.argtypes=[ct.c_uint];lib.dvbs2_map_set_kernel.restype=ct.c_bool
        lib.dvbs2_8psk_arena_words.argtypes=[ct.c_void_p,ct.c_bool];lib.dvbs2_8psk_arena_words.restype=ct.c_uint32
        lib.dvbs2_8psk_init.argtypes=[ct.c_void_p,ct.c_void_p,ct.c_bool,ct.c_uint32,PTR,ct.c_uint32]
        lib.dvbs2_8psk_init.restype=ct.c_bool
        lib.dvbs2_8psk_frame.argtypes=[ct.c_void_p,PTR,ct.POINTER(Stream)]
        lib.iq8quad_build.argtypes=[ct.POINTER(pulse.Tables)]
        lib.iq8quad_kernel_c.argtypes=[PTR,ct.POINTER(pulse.Input),ct.c_uint32,
                                      ct.POINTER(pulse.Input),ct.POINTER(pulse.Tables)]
        cls.cache={}

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def fec(self,index,pilots,seed=0x1234abcd):
        key=index,pilots,seed
        if key in self.cache:return self.cache[key]
        ref=mapping.ref;short=index>=11;rate=ref.RATES[index-11 if short else index];code=ref.code(rate,short)
        payload=np.random.default_rng(seed).integers(0,2,code.kbch-80,dtype=np.uint8)
        bb=np.concatenate([ref.bbheader(0x62,len(payload),sync=0xb9),payload])
        want=ref.fecframe(bb,rate,short)
        self.lib.test_init(index,pilots)
        guarded=np.full(2029,CANARY,np.uint32);native=guarded[1:-1]
        bwords=mapping.frame_words(bb)
        self.lib.test_fec(bwords.ctypes.data_as(PTR),native.ctypes.data_as(PTR))
        packed=mapping.frame_words(want)
        np.testing.assert_array_equal(native[:len(packed)],packed)
        assert guarded[0]==guarded[-1]==CANARY
        i,q=ref.plframe(want,rate,short=short,pilots=pilots)
        qpoints=ref.symbols(i,q)*np.exp(1j*np.pi/8)
        result=native.copy(),want,qpoints
        self.cache[key]=result
        return result

    def test_append_all_fills_tails_rings_and_counter_wrap(self):
        rng=np.random.default_rng(0x8_2350);cases=0
        for fill in range(16):
            prefix=np.exp(1j*(np.pi/8+rng.integers(0,8,fill)*np.pi/4))
            for tail_count in range(17):
                for count in (0,1,7):
                    words=rng.integers(0,1<<32,count,dtype=np.uint32);tail=int(rng.integers(1<<32))
                    points=np.concatenate([prefix,qpsk_points(words,tail,tail_count)])
                    for size,linear in ((1,False),(8,False),(64,False),(32,True)):
                        cursor=3 if linear else U32-2
                        raw,s,_=make_stream(size,fill,cursor,linear,prefix)
                        self.assertTrue(self.lib.dvbs2_qpsk8_append(ct.byref(s),words.ctypes.data_as(PTR),count,tail,tail_count))
                        check_stream(raw,s,points,cursor);cases+=1
        self.assertEqual(cases,3264)

    def test_actual_weight_orientation_and_invalid_inputs(self):
        words=np.array([0,0xffff,0xffff0000,U32],np.uint32)
        raw,s,_=make_stream(8)
        self.assertTrue(self.lib.dvbs2_qpsk8_append(ct.byref(s),words.ctypes.data_as(PTR),4,0,0))
        packed=memory(raw)[1:5]
        for w,group in zip(words,packed):
            ai,aq=int(group[0])&1,int(group[0])>>16&1
            bi,bq=int(group[1])&1,int(group[1])>>16&1
            point=.6532814824381883*((1-2*ai)+1j*(1-2*aq))+.2705980500730985*((1-2*bi)+1j*(1-2*bq))
            self.assertAlmostEqual(abs(point-qpsk_points([w],0,0)[0]),0,places=14)
        before=bytes(raw),bytes(s)
        for count,source in ((17,words.ctypes.data_as(PTR)),(U32,words.ctypes.data_as(PTR)),(0,None)):
            n=1 if source is None else 0
            self.assertFalse(self.lib.dvbs2_qpsk8_append(ct.byref(s),source,n,0,count))
            self.assertEqual((bytes(raw),bytes(s)),before)
        for invalid_fill,invalid_out in ((16,False),(0,True)):
            bad=Stream(out=None if invalid_out else s.out,mask=7,fill=invalid_fill)
            old_state=bytes(bad)
            self.assertFalse(self.lib.dvbs2_qpsk8_append(ct.byref(bad),None,0,0,0))
            self.assertEqual(bytes(bad),old_state);self.assertEqual(bytes(raw),before[0])
        self.assertFalse(self.lib.dvbs2_qpsk8_append(None,None,0,0,0))
        scratch=np.full(4098,CANARY,np.uint32);frame=np.zeros(2027,np.uint32)
        for capacity in (0,4095):
            self.assertFalse(self.lib.dvbs2_qpsk8_frame_from(frame.ctypes.data_as(PTR),ct.byref(s),scratch[1:].ctypes.data_as(PTR),capacity))
            self.assertEqual((bytes(raw),bytes(s)),before);self.assertTrue(np.all(scratch==CANARY))

    def test_all_qpsk_codes_actual_fec_header_pilots_and_map_selectors(self):
        cases=0
        for index in range(21):
            for pilots in (False,True):
                frame,_,points=self.fec(index,pilots)
                for kernel in (0,1):
                    self.lib.test_init(index,pilots);self.assertTrue(self.lib.dvbs2_map_set_kernel(kernel))
                    fill=(index+7*pilots)%16;raw,s,prefix=make_stream(256,fill)
                    scratch=np.full(4098,CANARY,np.uint32);before=frame.copy()
                    self.assertTrue(self.lib.dvbs2_qpsk8_frame_from(frame.ctypes.data_as(PTR),ct.byref(s),scratch[1:].ctypes.data_as(PTR),4096))
                    check_stream(raw,s,np.concatenate([prefix,points]))
                    np.testing.assert_array_equal(frame,before)
                    self.assertEqual(int(scratch[0]),CANARY);self.assertEqual(int(scratch[-1]),CANARY)
                    cases+=1
        self.assertEqual(cases,84)

    def test_alternating_real_frames_all_fills_preserve_ring_and_continuous_fir(self):
        index=8;code=self.lib.test_code(index);pilots=True
        frames=[self.fec(index,pilots,0x1234abcd^k) for k in range(6)]
        symbol_frames=[r[2] if k%2==0 else mapping.scalar_frame(r[1],'5/6',False,True)
                       for k,r in enumerate(frames)]
        qh=mapping.ref.symbols(*mapping.ref.plheader(9,False,True))*np.exp(1j*np.pi/8)
        eh=mapping.ref.symbols(*mapping.ref.plheader(15,False,True))*np.exp(1j*np.pi/8)
        np.testing.assert_allclose(qh[:26],eh[:26]);self.assertFalse(np.array_equal(qh,eh))
        taps,coef=pulse.coefficients()
        ta=(ct.c_uint32*16384)();tb=(ct.c_uint32*16384)();tables=pulse.Tables(ta,tb)
        self.lib.iq8quad_build(ct.byref(tables))
        crcs=[]
        for fill in range(16):
            self.lib.test_init(index,True);self.assertTrue(self.lib.dvbs2_map_set_kernel(1))
            arena=np.full(8194,CANARY,np.uint32);gold=arena[1:-1];cfg=(ct.c_uint64*32)()
            need=self.lib.dvbs2_8psk_arena_words(code,True)
            self.assertLessEqual(need+4096,8192)
            self.assertTrue(self.lib.dvbs2_8psk_init(cfg,code,True,0,gold.ctypes.data_as(PTR),need))
            gold_before=gold[:need].copy();scratch=gold[need:need+4096]
            raw,s,prefix=make_stream(4096,fill);points=prefix.copy();consumed=0;trace=[]
            for k,(frame,_,symbols) in enumerate(frames):
                previous=bytes(raw[1+((consumed-1)&4095)]) if consumed else None
                reserved=(s.fill+len(symbol_frames[k])+15)//16
                self.assertLessEqual(s.n+reserved-max(0,consumed-1),4096)
                if k%2==0:
                    self.assertTrue(self.lib.dvbs2_qpsk8_frame_from(frame.ctypes.data_as(PTR),ct.byref(s),scratch.ctypes.data_as(PTR),4096))
                else:self.lib.dvbs2_8psk_frame(cfg,frame.ctypes.data_as(PTR),ct.byref(s))
                if previous is not None:self.assertEqual(bytes(raw[1+((consumed-1)&4095)]),previous)
                points=np.concatenate([points,symbol_frames[k]])
                # Check all retained groups and partial carries without assuming
                # a mode boundary is a word boundary or resetting FIR history.
                expected=mapping.packed_groups(points);logical=np.arange(max(0,s.n-4096),s.n)
                np.testing.assert_array_equal(memory(raw)[1:-1][logical&4095],expected[logical])
                tail=mapping.sign_planes(points[len(expected)*16:])
                self.assertEqual(s.fill,len(points)%16)
                for value,bits in zip((s.acc_ai,s.acc_bi,s.acc_aq,s.acc_bq),tail):
                    self.assertEqual(value,sum(int(bit)<<j for j,bit in enumerate(bits)))
                while consumed<s.n:
                    off=consumed&4095;take=min(64,s.n-consumed,4096-off)
                    output=np.empty(take*32,np.uint32)
                    ip=ct.cast(ct.byref(raw,(1+off)*ct.sizeof(Input)),ct.POINTER(pulse.Input))
                    prev=ct.cast(ct.byref(raw,(1+((consumed-1)&4095))*ct.sizeof(Input)),ct.POINTER(pulse.Input)) if consumed else None
                    self.lib.iq8quad_kernel_c(output.ctypes.data_as(PTR),ip,take,prev,ct.byref(tables))
                    trace.append(output);consumed+=take
                np.testing.assert_array_equal(gold[:need],gold_before)
                self.assertTrue(np.all(memory(raw)[[0,-1]]==CANARY))
            actual=np.concatenate(trace);bits=mapping.sign_planes(points[:s.n*16])
            want,_,error=pulse.direct_reference(bits,taps,coef)
            np.testing.assert_array_equal(actual,want);self.assertLess(error,1.06)
            self.assertGreater(s.n,2*4096);self.assertEqual(int(arena[0]),CANARY);self.assertEqual(int(arena[-1]),CANARY)
            crcs.append(zlib.crc32(actual.tobytes()))
        print(json.dumps(dict(mixed_frames=6,initial_fills=16,shared_ring_groups=4096,source_ring_wraps_at_least=2,
                              raw_crc_by_fill=crcs,bb_matype=0x62,qpsk_modcod=9,psk8_modcod=15)),flush=True)

    def test_undefined_shift_sanitizer(self):
        d=Path(self.temp.name);source=d/'ubsan.c'
        source.write_text('#include "dvbs2_qpsk8.h"\nint main(void){iq8_input_t out[8];uint32_t w[2]={0x1234abcd,~0u};'
            'for(unsigned f=0;f<16;++f)for(unsigned t=0;t<=16;++t){'
            'dvbs2_8psk_stream_t s={.out=out,.mask=7,.fill=f,.acc_ai=(1u<<f)-1,.acc_bq=(1u<<f)-1};'
            'if(!dvbs2_qpsk8_append(&s,w,2,~0u,t))return 1;}return 0;}\n')
        subprocess.run(['cc','-std=c11','-O1','-fsanitize=undefined','-fno-sanitize-recover=undefined',*fwsrc.INCLUDES,
                        str(source),str(SRC / 'dvbs2/dvbs2_qpsk8.c'),str(SRC / 'dvbs2/dvbs2.c'),'-o',str(d/'ubsan')],check=True)
        subprocess.run([str(d/'ubsan')],check=True)


if __name__=='__main__':unittest.main()
