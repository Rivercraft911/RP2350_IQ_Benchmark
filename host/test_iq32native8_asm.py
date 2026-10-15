"""Execute actual32APSK assembly in a checked ISA/AAPCS model, not a timer."""
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import numpy as np
import test_iq16chunk_asm as vm
import test_iq32native8 as native


class Machine(vm.Machine):
    def c_leaf(self):
        r,mem=self.r,self.mem
        assert r[13]%8 == 0
        tables=mem.read(r[13])
        t=native.Tables(*(mem.ptr(mem.read(tables+4*i),native.ROWS*8) for i in range(3)))
        self.lib.iq32native8_kernel_c(mem.ptr(r[0],r[2]*16),mem.ptr(r[1],r[2]*4),r[2],
                                      mem.ptr(r[3],16) if r[3] else None,ctypes.byref(t))
        return_pc=r[14]&~1
        for i in (0,1,2,3,12): r[i]=0xdec00000+i
        r[14],r[15]=0xdec0000e,return_pc
        self.zero=self.borrow=False
        self.c_calls+=1

    def run(self):
        r,mem=self.r,self.mem
        while r[15] != vm.END:
            assert self.steps < 2_000_000
            assert r[15]%4 == 0 and 0 <= r[15]//4 < len(self.code)
            text=self.code[r[15]//4]
            op,*tail=text.split(maxsplit=1)
            a=vm.operands(tail[0]) if tail else []
            r[15]+=4
            self.steps+=1
            if op in ('cmp','subs'):
                x,y=(self.value(x) for x in (a[-2:] if len(a)==3 else a[:2]))
                self.zero,self.borrow=x==y,x<y
                if op=='subs': r[vm.reg(a[0])]=(x-y)&vm.MASK
            elif op in ('beq','bne'):
                if self.zero == (op=='beq'):
                    assert a[0][-1] in 'bf'
                    r[15]=self.labels[a[0][:-1]]
            elif op=='bl':
                assert a==['iq32native8_kernel_c']
                r[14]=r[15]|1
                self.c_leaf()
            elif op in ('mov','movs','movlo'):
                if op!='movlo' or self.borrow: r[vm.reg(a[0])]=self.value(a[1])
            elif op=='it':
                assert a==['lo'] and self.code[r[15]//4].startswith('movlo ')
            elif op in ('add','sub','and'):
                destination=vm.reg(a[0])
                x=self.value(a[1]) if len(a)>2 else r[destination]
                y=self.value(a[2]) if len(a)>2 else self.value(a[1])
                if len(a)==4:
                    assert a[3].startswith('lsl #')
                    y <<= int(a[3][5:])
                r[destination]=(x+y if op=='add' else x-y if op=='sub' else x&y)&vm.MASK
            elif op in ('lsr','uxth'):
                r[vm.reg(a[0])]=self.value(a[1]) >> self.value(a[2]) if op=='lsr' else self.value(a[1])&0xffff
            elif op in ('ldr','str'):
                address=self.address(a[1])
                if op=='str': mem.write(address,self.value(a[0]))
                else: r[vm.reg(a[0])]=mem.read(address)
            elif op in ('ldm','stm'):
                writeback=a[0].endswith('!')
                base_register=vm.reg(a[0].rstrip('!'))
                base=r[base_register]
                registers=vm.reglist(a[1])
                if op=='ldm':
                    values=[mem.read(base+4*i) for i in range(len(registers))]
                    for index,value in zip(registers,values): r[index]=value
                else:
                    for i,index in enumerate(registers): mem.write(base+4*i,r[index])
                if writeback: r[base_register]=base+4*len(registers)
            elif op=='sadd16':
                x,y=self.value(a[1]),self.value(a[2])
                r[vm.reg(a[0])]=((x+y)&0xffff)|((((x>>16)+(y>>16))&0xffff)<<16)
            elif op in ('push','pop'):
                registers=vm.reglist(a[0])
                if op=='push':
                    r[13]-=4*len(registers)
                    for i,index in enumerate(registers): mem.write(r[13]+4*i,r[index])
                else:
                    values=[mem.read(r[13]+4*i) for i in range(len(registers))]
                    r[13]+=4*len(registers)
                    for index,value in zip(registers,values): r[index]=value&~1 if index==15 else value
            elif op=='bx': r[15]=self.value(a[0])&~1
            else: raise AssertionError(('unsupported actual instruction',text))
        return self.steps


def execute(code,labels,lib,arrays,words,previous):
    mem=vm.Memory()
    for name,data in zip(('edge','middle','reverse'),arrays): mem.add(name,data.tobytes())
    mem.add('tables',np.array([vm.BASE[n] for n in ('edge','middle','reverse')],'<u4').tobytes())
    original=np.full(4,native.PILOT,np.uint32) if previous is None else previous
    input_bytes=mem.add('input',words.astype('<u4').tobytes())
    previous_bytes=mem.add('previous',original.astype('<u4').tobytes())
    output=mem.add('output',np.full(4*len(words)+2,native.CANARY,'<u4').tobytes(),True)
    stack=mem.add('stack',bytes(4096),True)
    machine=Machine(code,labels,mem,lib)
    machine.r=[0x12340000+i*0x111 for i in range(16)]
    machine.r[:4]=[vm.BASE['output']+4,vm.BASE['input'],len(words),vm.BASE['previous'] if previous is not None else 0]
    machine.r[13:16]=[vm.BASE['stack']+4088,vm.END|1,labels['iq32native8_kernel_asm']]
    mem.write(machine.r[13],vm.BASE['tables'])
    saved,sp=machine.r[4:12].copy(),machine.r[13]
    machine.run()
    assert machine.r[4:12]==saved and machine.r[13]==sp
    assert input_bytes==words.astype('<u4').tobytes() and previous_bytes==original.astype('<u4').tobytes()
    assert stack[:4040]==bytes(4040)
    result=np.frombuffer(output,'<u4')
    assert result[0]==result[-1]==native.CANARY
    assert machine.c_calls==int(len(words)>0)
    return result[1:-1],machine.steps


def main():
    source=native.ROOT/'firmware/src'
    vm.BASE['tables']=0x20040000
    code,labels=vm.program(source/'iq32native8_asm.S')
    cases=instructions=0
    rng=np.random.default_rng(812983)
    with tempfile.TemporaryDirectory(prefix='iq32native8-asm-') as temp:
        library=Path(temp)/'native.so'
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        f'-I{source}',str(source/'iq32native8.c'),'-o',str(library)],check=True)
        lib=ctypes.CDLL(str(library))
        lib.iq32native8_build.argtypes=[ctypes.POINTER(native.Tables),ctypes.c_uint]
        lib.iq32native8_build.restype=ctypes.c_bool
        lib.iq32native8_kernel_c.argtypes=[native.PTR,native.PTR,ctypes.c_uint32,native.PTR,ctypes.POINTER(native.Tables)]
        arrays=[np.empty(2*native.ROWS,np.uint32) for _ in range(3)]
        tables=native.Tables(*(native.ptr(a) for a in arrays))
        for rate in range(5):
            assert lib.iq32native8_build(ctypes.byref(tables),rate)
            floating,quantized=native.levels(rate)
            scaled=(quantized[:,None]*native.TAPS+(1<<29))>>30
            for n in (0,1,2,3,4,5,6,7,16,31,64,257):
                codes=rng.integers(0,17,(n,2,2),dtype=np.uint32)
                words=sum(codes[:,j,a]<<(16*a+5*j) for j in range(2) for a in range(2))
                for previous in (None,np.full(4,native.PILOT,np.uint32),
                                 np.array([0x02080208,0x00a000a0,0x014a014a,0x000e000e],np.uint32)):
                    got,count=execute(code,labels,lib,arrays,words,previous)
                    if n:
                        raw,_,_=native.expected(words,previous,scaled,floating)
                        assert np.array_equal(got,raw)
                    else: assert len(got)==0
                    cases+=1
                    instructions+=count
    print(json.dumps({'status':'PASS','scope':'actual source ISA/AAPCS model, not device timing',
                      'cases':cases,'instructions':instructions,'asm_frame_bytes':48,
                      'long_loop_instructions_per_two_symbols':len(code[labels['5']//4:labels['2']//4])},indent=2))


if __name__=='__main__': main()
