"""Exact rolling-triple32APSK format, partitions, and actual ASM ISA checks."""
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_iq16chunk_asm as vm
import test_iq32native8 as native
from test_iq32native8_asm import Machine as CoordinateMachine
import fwsrc


class Machine(CoordinateMachine):
    def c_leaf(self):
        r,mem=self.r,self.mem
        assert r[13]%8==0
        tables=mem.read(r[13])
        t=native.Tables(*(mem.ptr(mem.read(tables+4*i),native.ROWS*8) for i in range(3)))
        self.lib.iq32keys_kernel_c(mem.ptr(r[0],r[2]*16),mem.ptr(r[1],r[2]*8),r[2],
                                  mem.ptr(r[3],20) if r[3] else None,ctypes.byref(t))
        return_pc=r[14]&~1
        for i in (0,1,2,3,12): r[i]=0xdec00000+i
        r[14],r[15]=0xdec0000e,return_pc
        self.zero=self.borrow=False
        self.c_calls+=1


def encode(lib,words,previous,partition):
    result=np.full(2*len(words)+2,native.CANARY,np.uint32)
    for start in range(0,len(words),partition):
        history=(None if previous is None else previous[-1:]) if start==0 else words[start-1:start]
        lib.iq32keys_encode(native.ptr(result[1+2*start:]),native.ptr(words[start:]),
                             min(partition,len(words)-start),None if history is None else native.ptr(history))
    assert result[0]==result[-1]==native.CANARY
    return result[1:-1].copy()


def execute_c(lib,keys,previous,tables,partition):
    history=np.full(5,1842|1842<<16,np.uint32) if previous is None else previous
    data=np.concatenate([history,keys])
    output=np.full(2*len(keys)+2,native.CANARY,np.uint32)
    for start in range(0,len(keys)//2,partition):
        lib.iq32keys_kernel_c(native.ptr(output[1+4*start:]),native.ptr(keys[2*start:]),
                               min(partition,len(keys)//2-start),native.ptr(data[2*start:2*start+5]),
                               ctypes.byref(tables))
    assert output[0]==output[-1]==native.CANARY
    return output[1:-1].copy()


def execute_asm(code,labels,lib,arrays,keys,previous):
    mem=vm.Memory()
    for name,data in zip(('edge','middle','reverse'),arrays): mem.add(name,data.tobytes())
    mem.add('tables',np.array([vm.BASE[n] for n in ('edge','middle','reverse')],'<u4').tobytes())
    original=np.full(5,1842|1842<<16,np.uint32) if previous is None else previous
    source=mem.add('input',keys.astype('<u4').tobytes())
    history=mem.add('previous',original.astype('<u4').tobytes())
    output=mem.add('output',np.full(2*len(keys)+2,native.CANARY,'<u4').tobytes(),True)
    stack=mem.add('stack',bytes(4096),True)
    machine=Machine(code,labels,mem,lib)
    machine.r=[0x12340000+i*0x111 for i in range(16)]
    machine.r[:4]=[vm.BASE['output']+4,vm.BASE['input'],len(keys)//2,vm.BASE['previous'] if previous is not None else 0]
    machine.r[13:16]=[vm.BASE['stack']+4088,vm.END|1,labels['iq32keys_kernel_asm']]
    mem.write(machine.r[13],vm.BASE['tables'])
    saved,sp=machine.r[4:12].copy(),machine.r[13]
    machine.run()
    assert machine.r[4:12]==saved and machine.r[13]==sp
    assert source==keys.astype('<u4').tobytes() and history==original.astype('<u4').tobytes()
    assert stack[:4040]==bytes(4040)
    result=np.frombuffer(output,'<u4')
    assert result[0]==result[-1]==native.CANARY
    return result[1:-1],machine.steps


def main():
    source=native.ROOT/'firmware/src'
    vm.BASE['tables']=0x20040000
    code,labels=vm.program(source / 'apsk/iq32keys_asm.S')
    code=[line.replace('iq32keys_kernel_c','iq32native8_kernel_c') for line in code]
    cases=instructions=0
    rng=np.random.default_rng(9812)
    with tempfile.TemporaryDirectory(prefix='iq32keys-') as temp:
        library=Path(temp)/'native.so'
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        *fwsrc.INCLUDES,str(source / 'apsk/iq32native8.c'),str(source / 'apsk/iq32keys.c'),'-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library))
        lib.iq32native8_build.argtypes=[ctypes.POINTER(native.Tables),ctypes.c_uint]
        lib.iq32native8_build.restype=ctypes.c_bool
        lib.iq32native8_fill.argtypes=[native.PTR,ctypes.c_uint32,native.PTR]
        lib.iq32keys_encode.argtypes=[native.PTR,native.PTR,ctypes.c_uint32,native.PTR]
        lib.iq32keys_history.argtypes=[native.PTR,native.PTR]
        lib.iq32keys_kernel_c.argtypes=[native.PTR,native.PTR,ctypes.c_uint32,native.PTR,ctypes.POINTER(native.Tables)]
        lib.iq32keys_encode(None,None,0,None)
        lib.iq32keys_kernel_c(None,None,0,None,None)
        arrays=[np.empty(2*native.ROWS,np.uint32) for _ in range(3)]
        tables=native.Tables(*(native.ptr(a) for a in arrays))
        words=np.empty(8192,np.uint32)
        seed=ctypes.c_uint32(0x1234abcd)
        lib.iq32native8_fill(native.ptr(words),len(words),ctypes.byref(seed))
        fixture_keys=encode(lib,words,None,8192)
        for rate in range(5):
            assert lib.iq32native8_build(ctypes.byref(tables),rate)
            floating,quantized=native.levels(rate)
            scaled=(quantized[:,None]*native.TAPS+(1<<29))>>30
            coordinates=rng.integers(0,17,(4,2,2),dtype=np.uint32)
            previous=sum(coordinates[:,j,a]<<(16*a+5*j) for j in range(2) for a in range(2))
            for history in (None,previous):
                key_history=np.full(7,native.CANARY,np.uint32)
                lib.iq32keys_history(native.ptr(key_history[1:-1]),None if history is None else native.ptr(history))
                assert key_history[0]==key_history[-1]==native.CANARY
                expected_keys=encode(lib,words,history,8192)
                for partition in (1,2,3,5,31,256,8192):
                    assert np.array_equal(encode(lib,words,history,partition),expected_keys)
                    raw,_,_=native.expected(words,history,scaled,floating)
                    assert np.array_equal(execute_c(lib,expected_keys,key_history[1:-1],tables,partition),raw)
                    cases+=1
                all_codes=[native.decode(np.concatenate([np.full(4,native.PILOT,np.uint32) if history is None else history,words]),axis) for axis in (0,1)]
                reference_keys=[]
                for axis in (0,1):
                    windows=np.lib.stride_tricks.sliding_window_view(all_codes[axis],3)[6:]
                    reference_keys.append(windows[:,0]+17*windows[:,1]+289*windows[:,2])
                assert np.array_equal(expected_keys,reference_keys[0]|reference_keys[1]<<16)
                for count in (0,1,2,3,4,5,6,7,16,31,64,257):
                    got,steps=execute_asm(code,labels,lib,arrays,expected_keys[:2*count],None if history is None else key_history[1:-1])
                    assert np.array_equal(got,raw[:4*count])
                    cases+=1
                    instructions+=steps
    print(json.dumps({'status':'PASS','scope':'native/ISA component, no device timing',
                      'cases':cases,'instructions':instructions,'fixture_key_crc':native.crc(fixture_keys),
                      'input_bytes_per_symbol':4,'asm_frame_bytes':48,
                      'loop_instructions_per_two_symbols':len(code[labels['5']//4:labels['2']//4]),
                      'output_fixture_raw_crc':2048936984},indent=2))


if __name__=='__main__': main()
