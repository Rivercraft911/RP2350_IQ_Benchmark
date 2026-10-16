"""Independent pointer vectors, actual firmware address-check helper and host fault scope."""
import ctypes as ct
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import zlib

import numpy as np
import quadptr_trial as trial
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
PTR = ct.POINTER(ct.c_uint32)


def fixture(mode):
    words, values = trial.reference_addresses(mode, 0x20040000)
    axis_crc = [zlib.crc32(values[a*2048:(a+1)*2048].tobytes()) for a in range(2)]
    r = dict(cmd='benchquadptr',clk_hz=128000000,mode=mode,profile=trial.PROFILE,
             table_base=0x20040000,table_bytes=65536,table_crc=3433922394,
             ring_base=0x20038000,ring_bytes=131072,input_base=0x20008000,
             seed=trial.SEED,previous=trial.PREVIOUS,input_words=512,input_ring_words=8192,
             input_crc=zlib.crc32(words.tobytes()),input_dma_mask=0x30,pointer_sm_mask=3,
             sm_enabled_at_stop=3,null_trigger_mask=0x30,terminal_txstall_mask=3,rxstall_mask=0,
             pointer_errors=24,expected_pointer_errors=24,unexpected_pointer_errors=0,
             other_pio_flags=0,capture_dma_errors=0,input_issue_words=[512,512],
             input_read_addr=[0x20008800,0x20008802],input_remaining=[0,0],input_busy_mask=0,
             published_words=512,granted_words=512,read_addr_stable=True,
             capture_axis_words=[2048,2048],address_mismatches=[0,0],cap_layout='I_then_Q',
             capture_bswap=bool(mode),axis_crc=axis_crc,expected_axis_crc=axis_crc,
             ok=True,errors=0,cleanup_ok=True,restored=True,gpio_configured=False,
             cap_words=4096,cap_crc=zlib.crc32(values.tobytes()))
    return words, values, r


def lines(values):
    return ['@cap '+str(k)+' '+ ' '.join(f'{int(v):08x}' for v in values[k:k+16])
            for k in range(0,len(values),16)]


class PointerTrial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='quadptr-address-native-')
        d = Path(cls.temp.name)
        source = (ROOT/'firmware/src/bench/bench_quadptr.c').read_text()
        match = re.search(r'static unsigned address_mismatches\([^)]*\)\s*\{',source)
        start,end,depth = match.start(),match.end(),1
        while depth:
            depth += (source[end]=='{')-(source[end]=='}');end += 1
        check = source[start:end]
        wrapper = '''#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "iqgen.h"
enum { INPUT_WORDS=512, AXIS_POINTERS=2048, PREVIOUS=0x716bc429 };
static uint32_t in_buf[8192], cap_buf[4096];
''' + check + '''
void check_vectors(const uint32_t *input,const uint32_t *cap,uint32_t base,int mode,uint32_t *bad,uint32_t *crc) {
 memcpy(in_buf,input,2048);memcpy(cap_buf,cap,16384);
 for(unsigned axis=0;axis<2;++axis)bad[axis]=address_mismatches(axis,base,mode==1,&crc[axis]);
}
'''
        (d/'check.c').write_text(wrapper)
        subprocess.run(['cc','-std=c11','-O2','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        *fwsrc.INCLUDES,str(d/'check.c'),str(ROOT/'firmware/src/cpu_shaping/iqgen.c'),
                        '-o',str(d/'check.so')],check=True)
        cls.lib=ct.CDLL(str(d/'check.so'))
        cls.lib.check_vectors.argtypes=[PTR,PTR,ct.c_uint32,ct.c_int,PTR,PTR]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_firmware_helper_all_addresses_and_negative_control(self):
        for mode in (0,1):
            for base in (0x20000000,0x20010000,0x20040000,0x20070000):
                words,values=trial.reference_addresses(mode,base)
                for corrupt in (False,True):
                    got=values.copy()
                    if corrupt:got[37]^=32
                    bad=(ct.c_uint32*2)();crc=(ct.c_uint32*2)()
                    self.lib.check_vectors(words.ctypes.data_as(PTR),got.ctypes.data_as(PTR),base,mode,bad,crc)
                    self.assertEqual(list(bad),[int(corrupt),0])
                    self.assertEqual(list(crc),[zlib.crc32(values[a*2048:(a+1)*2048].tobytes()) for a in range(2)])

    def test_both_modes_exact_capture_and_optional_terminal_stall(self):
        for mode in (0,1):
            _,values,r=fixture(mode)
            for stalled in (False,True):
                r.update(terminal_txstall_mask=3 if stalled else 0,pointer_errors=24 if stalled else 8,
                         expected_pointer_errors=24 if stalled else 8)
                got=trial.validate_capture(r,lines(values),mode,128000000)
                self.assertEqual(got['faults'],[])
                self.assertEqual(got['host_checked_pointers'],4096)

    def test_complete_serially_valid_wrong_pointer_is_rejected(self):
        _,values,r=fixture(1);values[2048+73]^=64
        r['cap_crc']=zlib.crc32(values.tobytes())
        got=trial.validate_capture(r,lines(values),1)
        self.assertEqual(got['host_address_mismatches'],[0,1])
        self.assertEqual(got['host_first_bad'],2121)
        self.assertIn('numerical address mismatch',got['faults'])

    def test_source_boundary_and_fault_allowlist_are_strict(self):
        _,values,original=fixture(0)
        changes=[dict(input_read_addr=[0x20008804,0x20008802]),dict(input_issue_words=[513,512]),
                 dict(null_trigger_mask=0x10),dict(pointer_errors=16),dict(other_pio_flags=1),
                 dict(pointer_errors=24|4,expected_pointer_errors=24|4),dict(cleanup_ok=False),
                 dict(table_base=0x20040020),dict(profile='qpsk_l9_n4_kaiser_b080')]
        for change in changes:
            r=dict(original);r.update(change)
            self.assertTrue(trial.validate_capture(r,lines(values),0)['faults'],change)

    def test_truncated_or_reordered_capture_is_rejected(self):
        _,values,r=fixture(1);cap=lines(values)
        for bad in (cap[:-1],[cap[1],cap[0]]+cap[2:]):
            self.assertTrue(trial.validate_capture(r,bad,1)['faults'])


if __name__=='__main__':unittest.main()
