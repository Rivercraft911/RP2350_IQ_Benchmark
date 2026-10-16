"""Fused native-coordinate offset keys: exact prefix, partitions and FIR.

Native component proof only; no device timing.
"""
from pathlib import Path
import ctypes as c,hashlib,json,subprocess,tempfile
import numpy as np
import fwsrc

ROOT=Path(__file__).resolve().parents[1]
HERE=ROOT/'firmware/src'
P=c.POINTER(c.c_uint32)
ptr=lambda a:a.ctypes.data_as(P)
CANARY=np.uint32(0xa75c12e9)
class Tables(c.Structure):_fields_=[('old',P),('middle',P),('future',P)]

def independent(native,previous):
    pilot=np.uint32(6|6<<5|6<<16|6<<21)
    prior=np.full(4,pilot,np.uint32) if previous is None else previous
    words=np.r_[prior,native]
    axes=[]
    for shift in(0,16):
        codes=((words[:,None]>>(shift+5*np.arange(2)))&31).ravel()
        triples=np.lib.stride_tricks.sliding_window_view(codes,3)[1:]
        axes.append((triples[:,0]+17*triples[:,1]+289*triples[:,2])<<3)
    return (axes[0]|axes[1]<<16).astype(np.uint32)

def de_bruijn(k,n):
    a=[0]*(k*n);out=[]
    def visit(t,p):
        if t>n:
            if n%p==0:out.extend(a[1:p+1])
        else:
            a[t]=a[t-p];visit(t+1,p)
            for j in range(a[t-p]+1,k):a[t]=j;visit(t+1,t)
    visit(1,1)
    return np.asarray(out,np.uint32)

def main():
    with tempfile.TemporaryDirectory(prefix='native-offset-fusion-') as temp:
        library=Path(temp)/'native.so'
        subprocess.run(['cc','-O3','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
            *fwsrc.INCLUDES,str(HERE / 'apsk/iq32offsets_native.c'),str(HERE / 'apsk/iq32offsets_mla.c'),
            *[str(fwsrc.path(name)) for name in('iq32keys.c','iq32offsets.c','iq32native8.c')],
            '-o',str(library)],check=True)
        lib=c.CDLL(str(library))
        lib.iq32offsets_from_native.argtypes=[P,P,c.c_uint32,P]
        lib.iq32offsets_from_native_mla.argtypes=[P,P,c.c_uint32,P]
        def both(out,words,count,previous):
            """A3: the multiply form must write exactly the original words, no more."""
            size=2*count+5 if count else 0
            mla=np.full(size+2,CANARY,np.uint32)
            lib.iq32offsets_from_native_mla(ptr(mla[1:]) if count else None,ptr(words) if count else None,count,
                                            None if previous is None else ptr(previous))
            lib.iq32offsets_from_native(out,ptr(words) if count else None,count,None if previous is None else ptr(previous))
            assert mla[0]==mla[-1]==CANARY
            return mla[1:-1]
        mla_cases=0
        lib.iq32offsets_from_native_mla(None,None,0,None)
        # Any 32-bit input, including codes 17..31 whose packed products overflow a lane:
        # the two forms are the same integer polynomial modulo 2^32.
        wide=np.random.default_rng(20261016)
        for count in (1,2,3,5,64,511,512,513):
            for use_previous in (False,True):
                words=wide.integers(0,1<<32,count,dtype=np.uint64).astype(np.uint32)
                previous=wide.integers(0,1<<32,4,dtype=np.uint64).astype(np.uint32) if use_previous else None
                original=np.empty(2*count+5,np.uint32)
                np.testing.assert_array_equal(both(ptr(original),words,count,previous),original)
                mla_cases+=1
        lib.iq32keys_history.argtypes=[P,P]
        lib.iq32keys_encode.argtypes=[P,P,c.c_uint32,P]
        lib.iq32offsets_encode.argtypes=[P,P,c.c_uint32]
        lib.iq32native8_build.argtypes=[c.POINTER(Tables),c.c_uint]
        lib.iq32native8_build.restype=c.c_bool
        lib.iq32offsets_kernel_c.argtypes=[P,P,c.c_uint32,P,c.POINTER(Tables)]
        lib.iq32offsets_from_native(None,None,0,None)
        guarded=np.full(9,CANARY,np.uint32)
        lib.iq32offsets_from_native(ptr(guarded[2:]),None,0,None)
        assert np.all(guarded==CANARY)
        rng=np.random.default_rng(20261015)
        cases=0;symbols=0;output_words=0
        db=de_bruijn(17,3);db=np.r_[db,db[:3]]
        q=(2*db+3)%17
        exhaustive=(db[::2]|db[1::2]<<5|q[::2]<<16|q[1::2]<<21).astype(np.uint32)
        datasets=[exhaustive]
        for count in(1,2,3,4,7,63,64,256,512,1024):
            for _ in range(12):
                code=rng.integers(0,17,(count,4),dtype=np.uint32)
                words=code[:,0]|code[:,1]<<5|code[:,2]<<16|code[:,3]<<21
                # Reserved bits are ignored by both existing and candidate paths.
                words|=rng.integers(0,64,count,dtype=np.uint32)<<10
                words|=rng.integers(0,64,count,dtype=np.uint32)<<26
                datasets.append(words)
        for rate in range(5):
            arena=[np.empty(9826,np.uint32) for _ in range(3)];tables=Tables(*map(ptr,arena))
            assert lib.iq32native8_build(c.byref(tables),rate)
            for words in datasets:
                count=len(words)
                for use_previous in(False,True):
                    previous=None
                    if use_previous:
                        codes=rng.integers(0,17,(4,4),dtype=np.uint32)
                        previous=codes[:,0]|codes[:,1]<<5|codes[:,2]<<16|codes[:,3]<<21
                    source_before=words.copy();previous_before=None if previous is None else previous.copy()
                    result=np.full(2*count+7,CANARY,np.uint32)
                    mla=both(ptr(result[1:]),words,count,previous);mla_cases+=1
                    assert result[0]==result[-1]==CANARY
                    np.testing.assert_array_equal(mla,result[1:-1])
                    old=np.empty(2*count+5,np.uint32)
                    lib.iq32keys_history(ptr(old),None if previous is None else ptr(previous))
                    lib.iq32keys_encode(ptr(old[5:]),ptr(words),count,None if previous is None else ptr(previous[3:]))
                    lib.iq32offsets_encode(ptr(old),ptr(old),len(old))
                    np.testing.assert_array_equal(result[1:-1],old)
                    np.testing.assert_array_equal(result[1:-1],independent(words,previous))
                    got=np.empty(4*count,np.uint32);want=np.empty_like(got)
                    lib.iq32offsets_kernel_c(ptr(got),ptr(result[6:]),count,ptr(result[1:]),c.byref(tables))
                    lib.iq32offsets_kernel_c(ptr(want),ptr(old[5:]),count,ptr(old),c.byref(tables))
                    np.testing.assert_array_equal(got,want)
                    np.testing.assert_array_equal(words,source_before)
                    if previous is not None:np.testing.assert_array_equal(previous,previous_before)
                    cases+=1;symbols+=count*2;output_words+=len(got)
            # Every partition gets actual previous native words, including a
            # left-padded unit-pilot history for the first small chunks.
            words=exhaustive;pilot=np.uint32(6|6<<5|6<<16|6<<21)
            full=independent(words,None)[5:]
            start=0
            for count in(1,1,2,3,7,63,64,256,512,1024,99999):
                count=min(count,len(words)-start)
                if not count:continue
                previous=np.r_[np.full(4,pilot,np.uint32),words[:start]][-4:]
                out=np.empty(2*count+5,np.uint32)
                part=np.ascontiguousarray(words[start:start+count])
                np.testing.assert_array_equal(both(ptr(out),part,count,previous),out);mla_cases+=1
                np.testing.assert_array_equal(out[5:],full[2*start:2*(start+count)])
                np.testing.assert_array_equal(out,independent(words[start:start+count],previous))
                start+=count;cases+=1
            assert start==len(words)
        report=dict(status='passed',scope='portable C and independent integer model; no device timing',
                    cases=cases,current_symbols_compared=symbols,shaped_words_compared=output_words,
                    multiply_form_cases=mla_cases,multiply_form_any_32bit_input=True,
                    amplitude_rates=5,exhaustive_radix17_triples=4913,
                    ignored_reserved_bits=True,null_pilot_history=True,arbitrary_partition_history=True,
                    zero_blocks_no_access=True,immutable_inputs_and_canaries=True,
                    files_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in(HERE / 'apsk/iq32offsets_native.c',HERE / 'apsk/iq32offsets_native.h')})
        print(json.dumps(report,indent=2))
if __name__=='__main__':main()
