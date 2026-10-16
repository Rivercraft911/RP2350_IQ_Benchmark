"""Native DVB-S2 APSK mapping, streaming and progressive-credit proofs.

ETSI EN302307-1 V1.4.1 §§5.3.3–5.5.4, figures11/12; outer-radius1.
Independent polar geometry, scalar interleaver/PL/Gold reference, all valid
normal/short rates, native partial groups, ring wrap and cancellation.
No Pico/shared SDK build or RF hardware is accessed.
"""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'reference/dvbs2'))
import dvbs2 as ref
import test_iq16chunk as native16
import test_iq32native8 as native32
from test_dvbs2_8psk import frame_words

PTR=ctypes.POINTER(ctypes.c_uint32)
SERVICE=ctypes.CFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_uint32)
CANARY=0x9ad4c781
RATES16=('2/3','3/4','4/5','5/6','8/9','9/10')
RATES32=('3/4','4/5','5/6','8/9','9/10')
GAMMA16=dict(zip(RATES16,(315,285,275,270,260,257)))
GAMMA32=dict(zip(RATES32,((2.84,5.27),(2.72,4.87),(2.64,4.64),(2.54,4.33),(2.53,4.30))))


class Gold(ctypes.Structure):
    _fields_=[('r0',ctypes.c_uint32),('r1',ctypes.c_uint32)]


class Config(ctypes.Structure):
    _fields_=[('code',ctypes.c_void_p),('gold',ctypes.POINTER(Gold)),
              ('header',ctypes.c_uint32*6),('data_symbols',ctypes.c_uint32),
              ('body_symbols',ctypes.c_uint32),('frame_symbols',ctypes.c_uint32),
              ('gold_code',ctypes.c_uint32),('pilot_blocks',ctypes.c_uint32),
              ('modulation',ctypes.c_uint8),('modcod',ctypes.c_uint8),
              ('pilots',ctypes.c_uint8),('is_short',ctypes.c_uint8)]


class Stream(ctypes.Structure):
    _fields_=[('out',PTR),('mask',ctypes.c_uint32),('n',ctypes.c_uint32),
              ('acc_i',ctypes.c_uint32),('acc_q',ctypes.c_uint32),('fill',ctypes.c_uint32),
              ('service',SERVICE),('context',ctypes.c_void_p)]


class Fast(ctypes.Structure):
    _fields_=[('lut',PTR),('header32',PTR),('run',ctypes.c_void_p*3),('modulation',ctypes.c_uint32),
              ('placement',ctypes.c_uint32),('code_bytes',ctypes.c_uint32)]


FAST_WORDS=1024  # DVBS2_APSK_FAST_WORDS, checked against the header below


def polar_constellation(modulation,rate):
    if modulation==16:
        angle=np.array([3,-3,9,-9,1,-1,11,-11,5,-5,7,-7,3,-3,9,-9])
        points=np.exp(1j*np.pi*angle/12)
        points[12:]/=GAMMA16[rate]/100
        return points
    g1,g2=GAMMA32[rate]
    result=np.empty(32,np.complex128)
    # Three independent polar rings, indexed by the Figure12 binary labels.
    for labels,angles,radius,step in (
            ((17,19,21,23),(3,-3,9,-9),1/g2,np.pi/12),
            ((0,1,2,3,4,5,6,7,16,18,20,22),(3,5,-3,-5,9,7,-9,-7,1,-1,11,-11),g1/g2,np.pi/12),
            ((8,9,10,11,12,13,14,15,24,25,26,27,28,29,30,31),
             (1,3,-2,-4,6,4,-7,-5,0,2,-1,-3,7,5,8,-6),1,np.pi/8)):
        result[list(labels)]=radius*np.exp(1j*step*np.array(angles))
    return result


def scalar_frame(fec,modulation,rate,short,pilots,gold):
    width=4 if modulation==16 else 5
    columns=np.asarray(fec,np.uint8).reshape(width,-1)
    labels=columns.T@(1<<np.arange(width-1,-1,-1))
    data=polar_constellation(modulation,rate)[labels]
    chunks=[]
    for start in range(0,len(data),1440):
        chunks.append(data[start:start+1440])
        if pilots and start+1440<len(data):chunks.append(np.full(36,(1+1j)/np.sqrt(2)))
    body=np.concatenate(chunks)*1j**ref.pl_scrambling_sequence(sum(map(len,chunks)),gold)
    modcod=(18+RATES16.index(rate)) if modulation==16 else (24+RATES32.index(rate))
    hi,hq=ref.plheader(modcod,short,pilots)
    return np.concatenate([ref.symbols(hi,hq),body])


def alphabet(modulation,rate):
    return native16.levels(GAMMA16[rate])[0] if modulation==16 else native32.levels(RATES32.index(rate))[0]


def pack(points,modulation,rate):
    levels=alphabet(modulation,rate)
    group,width=(4,3) if modulation==16 else (2,5)
    axes=[np.argmin(abs(coordinate[:,None]-levels),axis=1).astype(np.uint32)
          for coordinate in (points.real,points.imag)]
    for values,codes in zip((points.real,points.imag),axes):
        assert np.max(abs(values-levels[codes]),initial=0)<2e-14
    take=len(points)//group*group
    shifts=width*np.arange(group,dtype=np.uint32)
    words=[(axis[:take].reshape(-1,group)<<shifts).sum(axis=1,dtype=np.uint32) for axis in axes]
    tail=[sum(int(code)<<(width*j) for j,code in enumerate(axis[take:])) for axis in axes]
    return words[0]|words[1]<<16,tail,len(points)%group


def run_case(lib,index,modulation,rate,short,pilots,fecs,*,gold=0,prefix=0,ring=0,callback=False,cancel=0,fast=False):
    code=lib.test_code(index)
    need=lib.dvbs2_apsk_arena_words(code,modulation,pilots)
    assert 0<need<=1040
    arena=np.full(need+2,CANARY,np.uint32)
    ap=arena[1:-1].ctypes.data_as(PTR)
    cfg=Config()
    assert lib.dvbs2_apsk_init(ctypes.byref(cfg),code,modulation,pilots,gold,ap,need)
    state=Fast();fast_arena=np.full(FAST_WORDS+2,CANARY,np.uint32)
    if fast:
        assert lib.dvbs2_apsk_fast_init(ctypes.byref(state),ctypes.byref(cfg),
                                        fast_arena[1:-1].ctypes.data_as(PTR),FAST_WORDS,0)
    fast_before=fast_arena.copy();state_before=bytes(state)
    assert cfg.modcod==(18+RATES16.index(rate) if modulation==16 else 24+RATES32.index(rate))
    rotations=ref.pl_scrambling_sequence(cfg.body_symbols,gold)
    data=np.arange(len(rotations))%1476<1440 if pilots else np.ones(len(rotations),bool)
    stored=np.concatenate([rotations[data],rotations[~data]])
    planes=((arena[1:-1].reshape(-1,2)[:,:,None]>>np.arange(32,dtype=np.uint32))&1).transpose(0,2,1).reshape(-1,2)
    np.testing.assert_array_equal(planes[:len(stored)],np.stack([stored&1,stored>>1],axis=1))
    assert not np.any(planes[len(stored):])
    arena_before=arena.copy();cfg_before=bytes(cfg)
    all_points=np.concatenate([polar_constellation(modulation,rate)[:prefix]]+
                              [scalar_frame(f,modulation,rate,short,pilots,gold) for f in fecs])
    all_words,_,_=pack(all_points,modulation,rate)
    size=ring if ring else len(all_words)+1
    raw=np.full(size+2,CANARY,np.uint32);out=raw[1:-1]
    _,tail,fill=pack(all_points[:prefix],modulation,rate)
    stream=Stream(out=out.ctypes.data_as(PTR),mask=size-1 if ring else 0xffffffff,
                  acc_i=tail[0],acc_q=tail[1],fill=fill)
    published=[];calls=[];errors=[]
    def service(context,n):
        try:
            previous=calls[-1] if calls else 0
            assert 0<=n-previous<=64
            published.extend(int(out[j&stream.mask]) for j in range(previous,n))
            calls.append(n)
            return not cancel or len(calls)<cancel
        except Exception as exc:
            errors.append(repr(exc));return False
    fn=SERVICE(service)
    if callback:stream.service=fn
    points=all_points[:prefix]
    accepted=0
    for fec in fecs:
        source=frame_words(fec);saved=source.copy()
        if fast:
            ok=lib.dvbs2_apsk_frame_fast(ctypes.byref(cfg),ctypes.byref(state),source.ctypes.data_as(PTR),
                                         ctypes.byref(stream))
        else:
            ok=lib.dvbs2_apsk_frame(ctypes.byref(cfg),source.ctypes.data_as(PTR),ctypes.byref(stream))
        assert not errors,errors
        np.testing.assert_array_equal(source,saved)
        points=np.concatenate([points,scalar_frame(fec,modulation,rate,short,pilots,gold)])
        if not ok:
            assert cancel and len(calls)==cancel
            group=4 if modulation==16 else 2
            points=points[:stream.n*group+stream.fill]
        else:accepted+=1
        expected,tail,fill=pack(points,modulation,rate)
        assert stream.n==len(expected) and stream.fill==fill and [stream.acc_i,stream.acc_q]==tail
        first=max(0,len(expected)-size)
        np.testing.assert_array_equal(out[np.arange(first,len(expected))&stream.mask],expected[first:])
        if not ok:break
    assert raw[0]==raw[-1]==CANARY
    np.testing.assert_array_equal(arena,arena_before)
    assert bytes(cfg)==cfg_before
    np.testing.assert_array_equal(fast_arena,fast_before)
    assert bytes(state)==state_before and fast_arena[0]==fast_arena[-1]==CANARY
    if callback:
        expected,_,_=pack(points,modulation,rate)
        np.testing.assert_array_equal(np.asarray(published,np.uint32),expected)
        assert calls[0]==0 and calls[-1]==stream.n
    return dict(modulation=modulation,rate=rate,short=short,pilots=pilots,gold=gold,fast=fast,
                prefix=prefix,ring=ring,callback=callback,cancel=cancel,frames=accepted,
                symbols=len(points),words=stream.n,fill=stream.fill,crc=zlib.crc32(expected.astype('<u4').tobytes()))


def run_both(lib,*args,**kwargs):
    """Original and A3 fast mapper must each match the independent reference."""
    old=run_case(lib,*args,**kwargs);new=run_case(lib,*args,fast=True,**kwargs)
    strip=lambda case:{key:value for key,value in case.items() if key!='fast'}
    assert strip(old)==strip(new),(old,new)
    return [old,new]


def mapper(lib,index,modulation,pilots,gold=0):
    code=lib.test_code(index);need=lib.dvbs2_apsk_arena_words(code,modulation,pilots)
    arena=np.zeros(need,np.uint32);cfg=Config()
    assert lib.dvbs2_apsk_init(ctypes.byref(cfg),code,modulation,pilots,gold,arena.ctypes.data_as(PTR),need)
    state=Fast();fast_arena=np.zeros(FAST_WORDS,np.uint32)
    assert lib.dvbs2_apsk_fast_init(ctypes.byref(state),ctypes.byref(cfg),fast_arena.ctypes.data_as(PTR),FAST_WORDS,0)
    return cfg,state,(arena,fast_arena)


def stream_run(lib,cfg,state,fecs,ring,fill,acc,cancel,fast):
    out=np.zeros(ring,np.uint32);calls=[];views=[];oks=[]
    stream=Stream(out=out.ctypes.data_as(PTR),mask=ring-1,acc_i=acc[0],acc_q=acc[1],fill=fill)
    def service(context,n):
        calls.append(n);views.append(out.copy())
        return len(calls)!=cancel
    callback=SERVICE(service);stream.service=callback
    for fec in fecs:
        words=frame_words(fec)
        if fast:ok=lib.dvbs2_apsk_frame_fast(ctypes.byref(cfg),ctypes.byref(state),words.ctypes.data_as(PTR),ctypes.byref(stream))
        else:ok=lib.dvbs2_apsk_frame(ctypes.byref(cfg),words.ctypes.data_as(PTR),ctypes.byref(stream))
        oks.append(bool(ok))
        if not ok:break
    return oks,calls,(stream.n,stream.fill,stream.acc_i,stream.acc_q),out,views


def exact_ab(lib,index,modulation,pilots,fecs,ring,fill,acc,cancel):
    """Fast vs original: return values, every service argument, the ring contents
    seen at every service call, the final ring and the stop state are identical."""
    cfg,state,keep=mapper(lib,index,modulation,pilots)
    a=stream_run(lib,cfg,state,fecs,ring,fill,acc,cancel,False)
    b=stream_run(lib,cfg,state,fecs,ring,fill,acc,cancel,True)
    assert a[0]==b[0] and a[1]==b[1] and a[2]==b[2],(modulation,index,pilots,ring,fill,cancel,a[2],b[2])
    np.testing.assert_array_equal(a[3],b[3])
    assert len(a[4])==len(b[4]) and all(np.array_equal(x,y) for x,y in zip(a[4],b[4]))
    return len(a[1])


def main():
    rng=np.random.default_rng(20261016)
    cases=[]
    with tempfile.TemporaryDirectory(prefix='dvbs2-apsk-native-') as temp:
        temp=Path(temp);src=ROOT/'firmware/src';bridge=temp/'bridge.c';library=temp/'mapper.so'
        bridge.write_text('#include "dvbs2_apsk.h"\n#include "dvbs2_codes.h"\n'
                          'const dvbs2_code_t *test_code(unsigned i){return i<N_DVBS2_CODES?&DVBS2_CODES[i]:0;}\n'
                          'unsigned test_cfg_size(void){return sizeof(dvbs2_apsk_cfg_t);}\n'
                          'unsigned test_stream_size(void){return sizeof(dvbs2_apsk_stream_t);}\n'
                          'unsigned test_fast_size(void){return sizeof(dvbs2_apsk_fast_t);}\n'
                          'unsigned test_fast_words(void){return DVBS2_APSK_FAST_WORDS;}\n')
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',f'-I{src}',
                        str(src/'dvbs2_apsk.c'),str(bridge),'-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library));lib.test_code.argtypes=[ctypes.c_uint];lib.test_code.restype=ctypes.c_void_p
        lib.dvbs2_apsk_arena_words.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_bool]
        lib.dvbs2_apsk_arena_words.restype=ctypes.c_uint32
        lib.dvbs2_apsk_init.argtypes=[ctypes.POINTER(Config),ctypes.c_void_p,ctypes.c_uint,ctypes.c_bool,
                                     ctypes.c_uint32,PTR,ctypes.c_uint32]
        lib.dvbs2_apsk_init.restype=ctypes.c_bool
        lib.dvbs2_apsk_frame.argtypes=[ctypes.POINTER(Config),PTR,ctypes.POINTER(Stream)]
        lib.dvbs2_apsk_frame.restype=ctypes.c_bool
        lib.dvbs2_apsk_fast_init.argtypes=[ctypes.POINTER(Fast),ctypes.POINTER(Config),PTR,ctypes.c_uint32,ctypes.c_uint]
        lib.dvbs2_apsk_fast_init.restype=ctypes.c_bool
        lib.dvbs2_apsk_frame_fast.argtypes=[ctypes.POINTER(Config),ctypes.POINTER(Fast),PTR,ctypes.POINTER(Stream)]
        lib.dvbs2_apsk_frame_fast.restype=ctypes.c_bool
        assert lib.test_cfg_size()==ctypes.sizeof(Config) and lib.test_stream_size()==ctypes.sizeof(Stream)
        assert lib.test_fast_size()==ctypes.sizeof(Fast) and lib.test_fast_words()==FAST_WORDS
        assert not lib.dvbs2_apsk_frame(None,None,None)
        assert not lib.dvbs2_apsk_frame_fast(None,None,None,None)
        configurations=0
        for modulation,rates in ((16,RATES16),(32,RATES32)):
            for short in (False,True):
                for rate in rates:
                    if short and rate=='9/10':continue
                    index=ref.RATES.index(rate)+(11 if short else 0)
                    fecs=[rng.integers(0,2,16200 if short else 64800,dtype=np.uint8) for _ in range(3)]
                    for pilots in (False,True):
                        configurations+=1
                        for prefix in range(4 if modulation==16 else 2):
                            cases+=run_both(lib,index,modulation,rate,short,pilots,fecs,prefix=prefix)
                        cases+=run_both(lib,index,modulation,rate,short,pilots,fecs,ring=2,prefix=1)
                        cases+=run_both(lib,index,modulation,rate,short,pilots,fecs,ring=2,prefix=0)
        for modulation in (16,32):
            fecs=[rng.integers(0,2,64800,dtype=np.uint8) for _ in range(3)]
            cases+=run_both(lib,8,modulation,'5/6',False,True,fecs,ring=128,callback=True,prefix=1)
            cases+=run_both(lib,8,modulation,'5/6',False,True,fecs,ring=128,callback=True,prefix=0)
            for aligned_cancel in (1,2,5):
                cases+=run_both(lib,8,modulation,'5/6',False,True,fecs,ring=128,callback=True,cancel=aligned_cancel,prefix=0)
            for cancel in (1,2,5):
                cases+=run_both(lib,8,modulation,'5/6',False,True,fecs,ring=128,callback=True,cancel=cancel,prefix=1)
            for gold in (1,99999,262142):
                cases+=run_both(lib,8,modulation,'5/6',False,True,fecs[:1],gold=gold)
        # A3 exactness beyond the reference: identical service calls, ring views and stop
        # states for rings that force runs across credit points and wraps, every fill
        # (odd fills and short frames take the original code) and refusals anywhere.
        exact=calls=0
        for modulation,indices in ((16,(5,8,10,16)),(32,(6,8,10,17))):
            for index in indices:
                fecs=[rng.integers(0,2,16200 if index>10 else 64800,dtype=np.uint8) for _ in range(3)]
                for pilots in (True,False):
                    for ring in (2,64,128,8192):
                        for fill in range(4 if modulation==16 else 2):
                            if modulation==16:acc=tuple(int(rng.integers(0,1<<(3*fill))) for _ in range(2))
                            else:acc=tuple(int(rng.integers(0,17)) if fill else 0 for _ in range(2))
                            for cancel in (0,1,2,5,40,65,150):
                                calls+=exact_ab(lib,index,modulation,pilots,fecs,ring,fill,acc,cancel);exact+=1
        # fill0 with a stale accumulator: put16 ORs it into the next word, and so must the leaf.
        for modulation,acc in ((16,(0x0123,0x0456)),(32,(5,9))):
            fec=[rng.integers(0,2,64800,dtype=np.uint8)]
            calls+=exact_ab(lib,8,modulation,True,fec,8192,0,acc,0);exact+=1
        # Engagement control: corrupting the fast tables must change normal-frame data;
        # short frames (original code) must not change at all.
        engaged={}
        for modulation,index in ((16,8),(32,8),(16,16),(32,17)):
            fec=[rng.integers(0,2,16200 if index>10 else 64800,dtype=np.uint8)]
            cfg,state,keep=mapper(lib,index,modulation,True)
            good=stream_run(lib,cfg,state,fec,1<<15,0,(0,0),0,True)[3]
            keep[1][:128]^=0x10001
            bad=stream_run(lib,cfg,state,fec,1<<15,0,(0,0),0,True)[3]
            changed=int(np.count_nonzero(good!=bad))
            if index>10:assert changed==0,(modulation,changed)
            else:assert changed>=(2000 if modulation==16 else 6480),(modulation,changed)
            engaged[f'{modulation}{"short" if index>10 else "normal"}']=changed
        # A mismatched fast state falls back to the original mapper; bad init changes nothing.
        cfg32,_,keep32=mapper(lib,8,32,True);_,state16,keep16=mapper(lib,8,16,True)
        fec=[rng.integers(0,2,64800,dtype=np.uint8)]
        np.testing.assert_array_equal(stream_run(lib,cfg32,state16,fec,1<<14,0,(0,0),0,True)[3],
                                      stream_run(lib,cfg32,state16,fec,1<<14,0,(0,0),0,False)[3])
        fast_rejected=0;probe=Fast();before=bytes(probe);space=np.zeros(FAST_WORDS,np.uint32)
        for args in ((None,ctypes.byref(cfg32),space.ctypes.data_as(PTR),FAST_WORDS,0),
                     (ctypes.byref(probe),None,space.ctypes.data_as(PTR),FAST_WORDS,0),
                     (ctypes.byref(probe),ctypes.byref(cfg32),None,FAST_WORDS,0),
                     (ctypes.byref(probe),ctypes.byref(cfg32),space.ctypes.data_as(PTR),FAST_WORDS-1,0),
                     (ctypes.byref(probe),ctypes.byref(cfg32),space.ctypes.data_as(PTR),FAST_WORDS,2)):
            assert not lib.dvbs2_apsk_fast_init(*args);fast_rejected+=1
        assert bytes(probe)==before and not space.any()
        rejected=0
        cfg=Config();arena=np.full(1042,CANARY,np.uint32)
        cfg_before=bytes(cfg);arena_before=arena.copy()
        for modulation in (8,16,32,64):
            for index in range(22):
                code=lib.test_code(index)
                need=lib.dvbs2_apsk_arena_words(code,modulation,True)
                if not need:
                    assert not lib.dvbs2_apsk_init(ctypes.byref(cfg),code,modulation,True,0,arena.ctypes.data_as(PTR),1040)
                    rejected+=1
                else:
                    for size,gold in ((need-1,0),(need,262143)):
                        assert not lib.dvbs2_apsk_init(ctypes.byref(cfg),code,modulation,True,gold,arena.ctypes.data_as(PTR),size)
                        rejected+=1
                assert bytes(cfg)==cfg_before
                np.testing.assert_array_equal(arena,arena_before)
        # Finite actual GCS BBHEADER→scalar BCH/LDPC frames, independently mapped.
        import apsk_fec_trial as finite
        real=[]
        for modulation in (16,32):
            fecs=[finite.dual_spi.ref.fecframe(finite.dual_spi.bbframe(i,gcs=True,rate='5/6'),'5/6',False)
                  for i in range(3)]
            for first in run_both(lib,8,modulation,'5/6',False,True,fecs[:1]):
                assert first['crc']==(1813402982 if modulation==16 else 4248357742)
                real.append(first)
            real+=run_both(lib,8,modulation,'5/6',False,True,fecs,ring=128,callback=True)
    print(json.dumps(dict(status='PASS',configurations=configurations,native_cases=len(cases),
                          native_frames=sum(c['frames'] for c in cases),
                          symbols_checked=sum(c['symbols'] for c in cases),
                          fast_mapper_cases=sum(c['fast'] for c in cases),
                          fast_exact_ab_cases=exact,fast_exact_ab_service_calls=calls,
                          fast_engagement_changed_words=engaged,fast_invalid_init_controls=fast_rejected,
                          gold_codes=[0,1,99999,262142],all_partial_fills={16:[0,1,2,3],32:[0,1]},
                          progressive_credit_words=64,invalid_init_controls=rejected,
                          real_gcs_fec_cases=real,amplitude_policy='outer_radius_one;unit_header_and_pilots'),indent=2))


if __name__=='__main__':main()
