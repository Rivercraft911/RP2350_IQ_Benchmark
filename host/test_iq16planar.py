"""Check actual bench row pointers/history plus actual planar assembly/FIR output.

Native extraction executes the production helper bodies. The existing checked ISA
model executes the real planar assembly. It checks output format and cyclic arena
reuse, not DMA arbitration or hardware deadline timing.
"""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import re
import subprocess
import tempfile
import numpy as np

import iq16out_trial as base
import test_iq16native8 as native
import test_iq16fast_planar as planar
from test_dual_faults import function_body


def main():
    source = base.iq.ROOT/'firmware/src'
    bench = (source/'bench16planar.c').read_text()
    enum = re.search(r'enum\s*\{[\s\S]*?\};', bench)[0]
    fixture = f'''
#include <assert.h>
#include <stdint.h>
#include "iq16native8.h"
#define IN_WORDS8192 8192
#define IN_WORDS IN_WORDS8192
{enum}
uint32_t in_buf[IN_WORDS];
unsigned input_offset, history_offset, output_count;
void iq16fast_planar_asm(uint32_t *out,const uint32_t *input,uint32_t blocks,
                         const uint32_t *previous,const iq16native8_tables_t *tables) {{
    (void)out; (void)tables;
    assert(input>=in_buf && input+blocks<=in_buf+IN_WORDS);
    assert(previous>=in_buf && previous+2<=in_buf+IN_WORDS);
    input_offset=input-in_buf;history_offset=previous-in_buf;output_count=blocks;
}}
void checked_shape(uint32_t *output,uint32_t sequence,const iq16native8_tables_t *tables) {{
{function_body(bench,'shape_block')}
}}
void checked_descriptors(uint32_t *di,uint32_t *dq,uint32_t *output) {{
{function_body(bench,'descriptor_rows')}
}}
'''
    code, labels = planar.program(source/'iq16fast_planar_asm.S')
    labels['iq16native8_kernel_asm'] = labels['iq16fast_planar_asm']
    with tempfile.TemporaryDirectory(prefix='iq16planar-glue-') as directory:
        path = Path(directory); (path/'check.c').write_text(fixture)
        library = path/'check.so'
        subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',f'-I{source}',
                        str(path/'check.c'),str(source/'iq16native8.c'),'-o',str(library)],check=True)
        lib = ctypes.CDLL(str(library))
        lib.checked_descriptors.argtypes = [native.PTR,native.PTR,native.PTR]
        lib.checked_shape.argtypes = [native.PTR,ctypes.c_uint32,ctypes.POINTER(native.Tables)]
        lib.iq16native8_build.argtypes = [ctypes.POINTER(native.Tables),ctypes.c_uint]
        lib.iq16native8_kernel_c.argtypes = [native.PTR,native.PTR,ctypes.c_uint32,native.PTR,ctypes.POINTER(native.Tables)]
        arrays = [np.zeros(rows*4,np.uint32) for rows in native.ROWS]
        tables = native.Tables(*(native.ptr(a) for a in arrays))
        assert lib.iq16native8_build(ctypes.byref(tables),270)
        words = base.source_words()
        inp = np.ctypeslib.as_array((ctypes.c_uint32*8192).in_dll(lib,'in_buf')); inp[:]=words
        output = np.zeros(8192,np.uint32)
        canary = 0xe4129acf
        di_guard = np.full(1026,canary,np.uint32);dq_guard = di_guard.copy()
        di,dq = di_guard[1:-1],dq_guard[1:-1]
        lib.checked_descriptors(native.ptr(di),native.ptr(dq),native.ptr(output))
        start = output.ctypes.data & 0xffffffff
        np.testing.assert_array_equal((di.astype(np.uint64)-start)&0xffffffff,np.arange(1024)*32)
        np.testing.assert_array_equal((dq.astype(np.uint64)-start)&0xffffffff,np.arange(1024)*32+16)
        assert di_guard[0]==di_guard[-1]==dq_guard[0]==dq_guard[-1]==canary
        descriptor_before = (di.copy(),dq.copy())
        checked = steps_total = cases = 0
        # Prefill, repeated sample-ring reuse, three source-period boundaries and
        # the u32 sequence multiplication boundary all use the production helper.
        sequences = [*range(8),30,31,32,33,62,63,64,65,94,95,96,97,0xfffffffd,0xffffffff]
        for sequence in sequences:
            lib.checked_shape(native.ptr(output),sequence,ctypes.byref(tables))
            first = ctypes.c_uint.in_dll(lib,'input_offset').value
            prev = ctypes.c_uint.in_dll(lib,'history_offset').value
            count = ctypes.c_uint.in_dll(lib,'output_count').value
            assert first==(sequence*256)&8191 and prev==(first-2)&8191 and count==256
            shaped,steps = native.assembly_output(code,labels,lib,arrays,words[first:first+count],words[prev:prev+2])
            slot = sequence & 3; output[slot*2048:(slot+1)*2048]=shaped
            # Follow numerical row pointers, as the data DMAs do, without any
            # transpose/copy in the production loop. Each axis shifts16 bits.
            bus = []
            for row in range(slot*256,(slot+1)*256):
                oi = ((int(di[row])-start)&0xffffffff)//4
                oq = ((int(dq[row])-start)&0xffffffff)//4
                i = output[oi:oi+4].astype('<u4').view('<u2') & 0x3fff
                q = output[oq:oq+4].astype('<u4').view('<u2') & 0x3fff
                bus.extend(np.stack([i|0x4000,q],axis=1).ravel())
            period = base.reference_period()
            origin = sequence*1024*4
            np.testing.assert_array_equal(bus,period[(origin+np.arange(4096,dtype=np.uint64))%len(period)])
            checked += len(bus); steps_total += steps; cases += 1
        np.testing.assert_array_equal(di,descriptor_before[0]);np.testing.assert_array_equal(dq,descriptor_before[1])
    print(json.dumps(dict(planar_glue='passed',actual_descriptor_rows=1024,axis_descriptors=2048,
                          cyclic_blocks=cases,checked_bus_halfwords=checked,executed_isa_steps=steps_total,
                          descriptor_guards=True,immutable_descriptors=True,source_boundary_cases=6,
                          silicon_timing=False)))


if __name__=='__main__':main()
