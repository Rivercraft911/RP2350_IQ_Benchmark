"""Native exact16APSK chunk-shaper checks. No Pico or shared firmware build.

Allsix ETSI radius ratios, every table key and symmetry sample, signed partial
bounds, unit header/pilot transitions, arbitrary partitions and exact/float FIR.
The production component uses128KiB tables plus an optional caller8KiB reverse map.
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
sys.path.insert(0,str(ROOT/'reference'))
import iqlut as m

L,N,K,ROWS,BLOCKS=9,2,4,4096,8192
PTR=ctypes.POINTER(ctypes.c_uint32)
P16=ctypes.POINTER(ctypes.c_uint16)
CANARY=0x9ad4c781

class Tables(ctypes.Structure):
    _fields_=[('edge',PTR),('middle',PTR),('reverse',P16)]

def filter_coefficients():
    taps=m.design_taps(.20,N,L,1.16)
    taps=np.convolve(taps,[-.0455,1.091,-.0455])[1:-1]
    taps*=8191*10**(-1/20)/abs(taps.reshape(L,N)).sum(axis=0).max()
    return taps,np.round(taps*256).astype(np.int64)

def levels(gamma100):
    gamma=gamma100/100
    positive=np.array([1/np.sqrt(2),np.cos(np.pi/12),np.sin(np.pi/12),1/(gamma*np.sqrt(2))])
    # Independently defined mathematical alphabet; the last Q30 integer must use
    # the builder's documented rational scaling of rounded unit diagonal.
    q=np.floor(positive*(1<<30)+.5).astype(np.int64)
    q[3]=(q[0]*100+gamma100//2)//gamma100
    return np.r_[positive,-positive],np.r_[q,-q]

def constellation(gamma100):
    # Figure11 labels in ascending binary order, outer radius1.
    angle=np.pi/12*np.array([3,-3,9,-9,1,-1,11,-11,5,-5,7,-7,3,-3,9,-9])
    points=np.exp(1j*angle);points[12:]/=gamma100/100
    return points

def encode_points(points,alphabet):
    codes=[np.argmin(abs(coordinate[:,None]-alphabet[None,:]),axis=1).astype(np.uint32)
           for coordinate in (points.real,points.imag)]
    assert all(float(np.max(abs(coordinate-alphabet[code])))<1e-14
               for coordinate,code in zip((points.real,points.imag),codes))
    shifts=3*np.arange(4,dtype=np.uint32)
    words=(codes[0].reshape(-1,4)<<shifts).sum(axis=1,dtype=np.uint32)
    words|=(codes[1].reshape(-1,4)<<shifts).sum(axis=1,dtype=np.uint32)<<16
    return codes,words

def decode_words(words,alphabet):
    shifts=3*np.arange(4,dtype=np.uint32)
    return [((words[:,None]>>(16*axis+shifts))&7).ravel().astype(np.int64) for axis in (0,1)]

def direct_rows(chunk,coordinate_q30,coef):
    keys=np.arange(ROWS,dtype=np.int64)
    digits=(keys[:,None]>>(3*np.arange(4)))&7
    scaled=(coordinate_q30[:,None]*coef[None,:]+(1<<29))>>30
    rows=np.zeros((ROWS,8),np.int64)
    for o in range(K):
        for p in range(N):
            for j in range(4):
                age=8+o-(4*chunk+j)
                if 0<=age<L: rows[:,2*o+p]+=scaled[digits[:,j],2*age+p]
    return ((rows+128)>>8).astype(np.int16)

def direct_output(codes,previous,coordinate_q30,coef):
    scaled=(coordinate_q30[:,None]*coef[None,:]+(1<<29))>>30
    axes=[]
    for axis in (0,1):
        data=np.concatenate([previous[axis],codes[axis]])
        windows=np.lib.stride_tricks.sliding_window_view(data,12)[::4]
        output=np.zeros((len(windows),8),np.int64)
        for ch in range(3):
            partial=np.zeros_like(output)
            for o in range(K):
                for p in range(N):
                    for j in range(4):
                        age=8+o-(4*ch+j)
                        if 0<=age<L:partial[:,2*o+p]+=scaled[windows[:,4*ch+j],2*age+p]
            output+=(partial+128)>>8
        axes.append(output.ravel())
    i,q=[x.astype(np.uint16).astype(np.uint32) for x in axes]
    words=np.stack([i[::2]|i[1::2]<<16,q[::2]|q[1::2]<<16],axis=1).ravel()
    return axes,words

def native_output(lib,words,previous,tables,partition):
    result=(ctypes.c_uint32*(len(words)*8+2))();result[0]=result[-1]=CANARY
    for start in range(0,len(words),partition):
        take=min(partition,len(words)-start)
        if start==0: history=previous
        elif start==1:history=np.array([0 if previous is None else previous[-1],words[0]],np.uint32)
        else:history=words[start-2:start]
        prev=None if history is None else history.ctypes.data_as(PTR)
        lib.iq16chunk_kernel_c(ctypes.cast(ctypes.byref(result,4+32*start),PTR),
                              words[start:].ctypes.data_as(PTR),take,prev,ctypes.byref(tables))
    assert result[0]==result[-1]==CANARY
    return np.ctypeslib.as_array(result)[1:-1].copy()

def main():
    taps,coef=filter_coefficients();records=[];cases=0
    with tempfile.TemporaryDirectory(prefix='iq16chunk-native-') as temp:
        source=ROOT/'firmware/src';library=Path(temp)/'chunk.so'
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',f'-I{source}',
                        str(source/'iq16chunk.c'),'-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library));lib.iq16chunk_build.argtypes=[ctypes.POINTER(Tables),ctypes.c_uint]
        lib.iq16chunk_build.restype=ctypes.c_bool;lib.iq16chunk_build_reverse.argtypes=[P16]
        lib.iq16chunk_coefficients.restype=ctypes.POINTER(ctypes.c_int32)
        lib.iq16chunk_fill.argtypes=[PTR,ctypes.c_uint32,PTR]
        lib.iq16chunk_kernel_c.argtypes=[PTR,PTR,ctypes.c_uint32,PTR,ctypes.POINTER(Tables)]
        np.testing.assert_array_equal(np.ctypeslib.as_array(lib.iq16chunk_coefficients(),shape=(18,)),coef)
        a=[(ctypes.c_uint32*(ROWS*4+2))() for _ in range(2)]
        for array in a:array[0]=array[-1]=CANARY
        reverse=(ctypes.c_uint16*(ROWS+2))();reverse[0]=reverse[-1]=0x9753
        revptr=ctypes.cast(ctypes.byref(reverse,2),P16);lib.iq16chunk_build_reverse(revptr)
        assert reverse[0]==reverse[-1]==0x9753
        r=np.arange(ROWS);want_reverse=((r&7)<<9)|((r&56)<<3)|((r>>3)&56)|((r>>9)&7)
        np.testing.assert_array_equal(np.ctypeslib.as_array(reverse)[1:-1],want_reverse)
        tables=Tables(*(ctypes.cast(ctypes.byref(x,4),PTR) for x in a),None)
        for gamma100 in (315,285,275,270,260,257):
            alphabet,q30=levels(gamma100)
            assert lib.iq16chunk_build(ctypes.byref(tables),gamma100)
            rows=[np.ctypeslib.as_array(x)[1:-1].copy().view(np.int16).reshape(ROWS,8) for x in a]
            for ch in (0,1):np.testing.assert_array_equal(rows[ch],direct_rows(ch,q30,coef))
            last=direct_rows(2,q30,coef)
            np.testing.assert_array_equal(last,rows[0][want_reverse,::-1])
            np.testing.assert_array_equal(rows[1],rows[1][want_reverse,::-1])
            prefix=np.zeros(8,np.int64);largest=0
            for row in [rows[0],rows[1],last]:
                prefix+=abs(row.astype(np.int64)).max(axis=0);largest=max(largest,int(prefix.max()))
            assert largest<8192
            crcs=[zlib.crc32(row.astype('<i2').tobytes()) for row in rows]
            if gamma100==270:assert crcs==[0x1cb4542d,0x938da81f]
            for seed in (0x1234abcd,1,0xffffffff):
                labels=m.xorshift32(seed,BLOCKS*4)&15
                points=constellation(gamma100)[labels]
                codes,data=encode_points(points,alphabet)
                native=np.zeros(BLOCKS,np.uint32);state=ctypes.c_uint32(seed)
                lib.iq16chunk_fill(native.ctypes.data_as(PTR),BLOCKS,ctypes.byref(state))
                np.testing.assert_array_equal(native,data)
                previous_codes=[np.zeros(8,np.int64)]*2
                axes,expected=direct_output(codes,previous_codes,q30,coef)
                for use_map in (False,True):
                    tables.reverse=revptr if use_map else None
                    for partition in (1,7,64,BLOCKS):
                        np.testing.assert_array_equal(native_output(lib,data,None,tables,partition),expected);cases+=1
                before=np.full(8,(1+1j)/np.sqrt(2));imp=np.zeros((len(points)+8)*N,complex)
                imp[::N]=np.concatenate([before,points]);reference=np.convolve(imp,taps)[16:][:len(points)*N]
                error=axes[0]+1j*axes[1]-reference
                max_error=float(max(abs(error.real).max(),abs(error.imag).max()))
                assert max_error<1.535
                if seed==0x1234abcd:
                    packed=(axes[0]&0x3fff)|(axes[1]&0x3fff)<<16
                    records.append(dict(gamma100=gamma100,blocks=BLOCKS,symbols=BLOCKS*4,
                                        table_crcs=crcs,all_history_partial_bound_lsb=largest,
                                        max_float_axis_error_lsb=max_error,raw_pairs_crc=zlib.crc32(expected.astype('<u4').tobytes()),
                                        packed_no_flag_crc=zlib.crc32(packed.astype('<u4').tobytes())))
            # Header/pilot transients begin inside a packed key and cross table/history boundaries.
            labels=m.xorshift32(0x98765432,548)&15;points=constellation(gamma100)[labels]
            points[137:227]=np.tile(np.array([1+1j,-1+1j,1-1j,-1-1j])/np.sqrt(2),23)[:90]
            points[401:437]=(1+1j)/np.sqrt(2)
            codes,data=encode_points(points,alphabet)
            previous=data[17:19].copy();prev_codes=decode_words(previous,alphabet)
            _,expected=direct_output(codes,prev_codes,q30,coef)
            noisy=data|np.uint32(0xf000f000);previous|=np.uint32(0xa0005000)
            for partition in (1,7,64,len(data)):
                np.testing.assert_array_equal(native_output(lib,noisy,previous,tables,partition),expected);cases+=1
        # Zero-size calls must not dereference any pointer, including tables/history.
        lib.iq16chunk_kernel_c(None,None,0,None,None)
        snapshots=[bytes(x) for x in a]
        assert not lib.iq16chunk_build(ctypes.byref(tables),271)
        assert snapshots==[bytes(x) for x in a]
        assert all(x[0]==x[-1]==CANARY for x in a)
    print(json.dumps(dict(profile='exact16apsk_chunks',L=L,sps=N,bytes=2*ROWS*4*4,
                          optional_reverse_bytes=ROWS*2,native_partition_cases=cases,
                          symmetry_samples_per_gamma=ROWS*8,fixtures=records)))

if __name__=='__main__':main()
