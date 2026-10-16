"""Common native QPSK/8PSK/32APSK pulse: exact component transition proof.

This constructs geometric symbols, not complete DVB-S2 frames or bit labels.
Fixed32APSK gamma, outer radii1, orientation, pulse and sample rate are shared.
The faster globallypi/8-rotated8PSK engine is a different profile.
"""
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_iq32native8 as native
import test_iq32keys as keys
import fwsrc


def points(rate):
    g1,g2=native.GAMMA[rate]
    return (np.exp(1j*(np.pi/4+np.arange(4)*np.pi/2)),
            np.exp(1j*np.arange(8)*np.pi/4),
            np.concatenate([100/g2*np.exp(1j*(np.pi/4+np.arange(4)*np.pi/2)),
                            g1/g2*np.exp(1j*(np.pi/12+np.arange(12)*np.pi/6)),
                            np.exp(1j*np.arange(16)*np.pi/8)]))


def pack(points,alphabet):
    result=[]
    for axis in (points.real,points.imag):
        code=np.argmin(abs(axis[:,None]-alphabet),axis=1).astype(np.uint32)
        assert np.max(abs(axis-alphabet[code]))<1e-12
        result.append(code)
    i,q=result
    assert len(i)%2==0
    return i[::2]|i[1::2]<<5|q[::2]<<16|q[1::2]<<21


def main():
    source=native.ROOT/'firmware/src'
    rng=np.random.default_rng(325916)
    cases=boundaries=observations=0
    with tempfile.TemporaryDirectory(prefix='iq32mix-') as temp:
        library=Path(temp)/'native.so'
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        *fwsrc.INCLUDES,str(source / 'apsk/iq32native8.c'),str(source / 'apsk/iq32keys.c'),'-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library))
        lib.iq32native8_build.argtypes=[ctypes.POINTER(native.Tables),ctypes.c_uint]
        lib.iq32native8_build.restype=ctypes.c_bool
        lib.iq32native8_kernel_c.argtypes=[native.PTR,native.PTR,ctypes.c_uint32,native.PTR,ctypes.POINTER(native.Tables)]
        lib.iq32keys_encode.argtypes=[native.PTR,native.PTR,ctypes.c_uint32,native.PTR]
        lib.iq32keys_history.argtypes=[native.PTR,native.PTR]
        lib.iq32keys_kernel_c.argtypes=[native.PTR,native.PTR,ctypes.c_uint32,native.PTR,ctypes.POINTER(native.Tables)]
        arrays=[np.empty(2*native.ROWS,np.uint32) for _ in range(3)]
        tables=native.Tables(*(native.ptr(a) for a in arrays))
        for rate in range(5):
            floating,quantized=native.levels(rate)
            scaled=(quantized[:,None]*native.TAPS+(1<<29))>>30
            assert lib.iq32native8_build(ctypes.byref(tables),rate)
            constellations=points(rate)
            # Every point under every Gold quarter-turn stays in the same17
            # levels. No equality between bit labels across modes is assumed.
            for constellation in constellations:
                for turn in range(4):
                    p=constellation*(1j**turn)
                    for axis in (p.real,p.imag):
                        assert np.max(np.min(abs(axis[:,None]-floating),axis=1))<1e-12
            for starting_fill in (0,1):
                segments=[]
                ends=[]
                if starting_fill: segments.append(constellations[0][:1])
                for repeat in range(6):
                    # Odd-sized data regions intentionally straddle two-symbol
                    # groups; unit pilot/header geometry preserves continuity.
                    for mode,length in ((0,91),(1,177),(2,333),(1,95),(2,259),(0,37)):
                        constellation=constellations[mode]
                        turn=(repeat+mode)%4
                        segments.append(constellation[rng.integers(0,len(constellation),length)]*(1j**turn))
                        ends.append(sum(map(len,segments)))
                        segments.append(constellations[0][rng.integers(0,4,36)])
                stream=np.concatenate(segments)
                if len(stream)%2: stream=np.append(stream,constellations[0][0])
                words=pack(stream,floating)
                history=pack(constellations[2][rng.integers(0,32,8)],floating)
                want,_,_=native.expected(words,history,scaled,floating)
                got=native.execute(lib,words,history,tables,31)
                assert np.array_equal(got,want)
                encoded=keys.encode(lib,words,history,17)
                previous=np.empty(5,np.uint32)
                lib.iq32keys_history(native.ptr(previous),native.ptr(history))
                assert np.array_equal(keys.execute_c(lib,encoded,previous,tables,29),want)
                # A per-block history reset is observably wrong at the first
                # transition neighborhood; the real stream never resets it.
                cut=ends[1]//2
                reset=native.execute(lib,words[cut:cut+8],None,tables,8)
                assert np.any(reset[:16] != want[4*cut:4*cut+16])
                observations+=len(want)*2
                boundaries+=len(ends)
                cases+=1
    print(json.dumps({'status':'PASS','scope':'native mixed-geometry component only',
                      'modes':['native QPSK','native8PSK','native32APSK R3=1'],
                      'fixed_gamma_profiles':5,'starting_partial_fills':2,'cases':cases,
                      'mode_boundaries':boundaries,'checked_output_halfwords':observations,
                      'same_sample_clock':True,'history_flushed':False,
                      'complete_dvb_frames':False},indent=2))


if __name__=='__main__': main()
