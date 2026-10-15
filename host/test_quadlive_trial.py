"""Actual cursor extension and strict full-waveform/fault1 host validation; no hardware."""
import ctypes as ct
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import zlib

import numpy as np

import quadlive_trial as trial
from dual_trial import reference_bus

ROOT = Path(__file__).resolve().parents[1]


def capture_lines(values):
    return ['@cap '+str(k)+' '+ ' '.join(f'{int(v):08x}' for v in values[k:k+16])
            for k in range(0,len(values),16)]


def fixture(cpw=1, fault=0, clock=128000000):
    words = trial.iq.m.xorshift32(trial.SEED,trial.INPUT_WORDS).astype('<u4')
    bus = reference_bus(1,trial.SEED,4,profile=trial.L8_PROFILE)
    # One fixed offset crosses the physical source-ring boundary and both I/Q axes.
    values = bus[(len(bus)-257 + 17*np.arange(8192)) % len(bus)].astype('<u2').view('<u4')
    end = int(clock/(8*cpw*16))+8 if not fault else 257536
    elapsed = 1000000 if not fault else 260000
    reusable = end-8
    published = reusable+8192
    granted = published//512*512 if not fault else end
    polls = elapsed//trial.POLL_US
    issue_budget,cursor_deadline = trial.poll_limits(clock)
    r = dict(cmd='quadlive',git='frozen-test-image',clk_hz=clock,source=1,sps=4,L=8,row_profile=trial.L8_PROFILE,
             filter_beta=None,row_symbols=4,writers=2,schedule=0,bus_priority_mask=0,
             cpw=cpw,divider256=cpw*256,ms=1000,fault=fault,elapsed_us=elapsed,
             sym_rate=clock/(8*cpw),seed=trial.SEED,in_words=8192,previous=int(words[-1]),
             input_base=0x20008000,input_crc=zlib.crc32(words.tobytes()),input_after_crc=zlib.crc32(words.tobytes()),
             ring_base=0x20038000,ring_bytes=131072,table_base=0x20040000,table_bytes=65536,
             table_crc=3433922394,errors=(trial.ERR_POINTER|3) if fault else 0,
             pointer_errors=24 if fault else 0,live_pointer_errors=24 if fault else 0,
             output_errors=3 if fault else 0,reason='pointer producer fault' if fault else 'complete',
             restored=True,selectors_restored=True,core1_idle=True,descriptor_gen_busy=0.0,
             sample_math_busy=0.0,supervision_cycles=polls*300,
             supervision_busy=polls*300/(elapsed*clock/1e6),core0_work_busy=polls*300/(elapsed*clock/1e6),idle_wait='spin',
             polls=polls,poll_interval_us=trial.POLL_US,max_poll_interval_us=trial.MAX_POLL_US,
             max_poll_gap_us=129,max_supervision_cycles=350,poll_issue_budget_words=issue_budget,
             max_source_snapshot_span_us=130,
             required_credit_runway_words=2*issue_budget,min_credit_runway_words=7168,
             source_cursor_deadline_us=cursor_deadline,
             txstalls_i=int(bool(fault)),txstalls_q=int(bool(fault)),pointer_sm_mask=3,pointer_other_pio_flags=0,
             pointer_txstall_mask=3 if fault else 0,
             pointer_rxstall_mask=3,null_trigger_mask=0x30 if fault else 0,
             start_input_issue_words=[8,8],input_issue_words=[end-1,end-1] if not fault else [end,end],
             final_input_issue_words=[end,end],pre_abort_input_issue_words=[end,end],
             pre_abort_input_read_addr=[0x20008000+4*(end%8192),0x20008002+4*(end%8192)],
             pre_abort_input_read_addr_stable=True,reusable_words=reusable,published_words=published,granted_words=granted,
             grants_frozen=bool(fault),frozen_granted_words=granted if fault else 0,read_beyond_grants=False,
             cap_input_issue_low=end//2,cap_input_issue_high=end//2,cap_symbol_cursor=8*end,
             capture_started=not fault,capture_count=0 if fault else 4096,cap_words=0 if fault else 4096,
             cap_crc=0 if fault else zlib.crc32(values.tobytes()),cap_stride=17,
             cap_divider256=cpw*256,cap_fifo_only=False)
    return r,values,[] if fault else capture_lines(values)


class QuadliveTrial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='quadlive-cursor-native-')
        d = Path(cls.temp.name)
        text = (ROOT/'firmware/src/bench_quadlive.c').read_text()
        match = re.search(r'static uint64_t observed_issues\([^)]*\)\s*\{',text)
        start,end,depth = match.start(),match.end(),1
        while depth:
            depth += (text[end]=='{')-(text[end]=='}');end += 1
        def function(signature):
            start=text.index(signature)
            end,depth=text.index('{',start)+1,1
            while depth:
                depth+=(text[end]=='{')-(text[end]=='}');end+=1
            return text[start:end]
        constants=re.search(r'enum \{.*?\};',text,re.S).group()
        source = '''#include <stdint.h>
#include <stdbool.h>
#include "iqquadptr.h"
enum { IN_WORDS=8192 };
typedef struct { uint64_t granted_words,input_high_words; } iqquadptr_progress_t;
static uint32_t in_buf[IN_WORDS];
struct channel { uint32_t read_addr; };
static struct { struct channel ch[2]; } registers;
#define dma_hw (&registers)
''' + constants + function('static uint32_t poll_issue_budget(') + function('static bool credit_runway_ok(') + text[start:end] + '''
uint64_t check_cursor(unsigned axis,uint64_t previous,uint32_t cursor,uint32_t *captured) {
 registers.ch[axis].read_addr=(uint32_t)(uintptr_t)in_buf+2*axis+4*cursor;
 return observed_issues(axis,axis,previous,captured);
}
uint32_t check_budget(uint32_t clock,unsigned cycles) { return poll_issue_budget(clock,cycles); }
bool check_runway(uint64_t granted,uint64_t issued,uint32_t budget,uint64_t *minimum) {
 iqquadptr_progress_t p={.granted_words=granted,.input_high_words=issued};
 return credit_runway_ok(&p,budget,minimum);
}
'''
        (d/'check.c').write_text(source)
        subprocess.run(['cc','-std=c11','-O2','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        '-I'+str(ROOT/'firmware/src'),str(d/'check.c'),
                        str(ROOT/'firmware/src/iqquadptr.c'),str(ROOT/'firmware/src/iqquad.c'),
                        '-o',str(d/'check.so')],check=True)
        cls.lib = ct.CDLL(str(d/'check.so'))
        cls.lib.check_cursor.argtypes=[ct.c_uint,ct.c_uint64,ct.c_uint32,ct.POINTER(ct.c_uint32)]
        cls.lib.check_cursor.restype=ct.c_uint64
        cls.lib.check_budget.argtypes=[ct.c_uint32,ct.c_uint]
        cls.lib.check_budget.restype=ct.c_uint32
        cls.lib.check_runway.argtypes=[ct.c_uint64,ct.c_uint64,ct.c_uint32,ct.POINTER(ct.c_uint64)]
        cls.lib.check_runway.restype=ct.c_bool
        cls.lib.iqquadptr_grant_until.argtypes=[ct.POINTER(ct.c_uint32)]+[ct.c_uint64]*3
        cls.lib.iqquadptr_grant_until.restype=ct.c_uint64

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_cursor_extension_every_position_wrap_and_axis(self):
        address=ct.c_uint32()
        for axis in (0,1):
            for previous in (0,8191,8192,2**32-7,2**32+8191,2**40+3):
                for delta in range(8192):
                    current=(previous+delta)&8191
                    got=self.lib.check_cursor(axis,previous,current,ct.byref(address))
                    self.assertEqual(got,previous+delta)
                    self.assertEqual(address.value&3,2*axis)

    def test_derived_maximum_gap_credit_runway_actual_grant_leaf_all_phases(self):
        # For paired fault-free outputs, either source axis leads the physical
        # output by <=128 symbols. Round up one extra word for phase differences.
        # Arbitrarily skewed/faulty consumers are checked by the guard below.
        minima={}
        for clock in (128000000,150000000):
            budget,deadline=trial.poll_limits(clock)
            self.assertEqual(self.lib.check_budget(clock,49),budget)
            self.assertLess(trial.MAX_POLL_US,deadline//8)
            minimum=8192
            for phase in range(16):
                for offset in range(512):
                    reusable=8192+512*phase+offset
                    for advance in (0,1,budget):
                        published=reusable-advance+8192 # prior guarded reuse, conservative
                        for skew in (0,9):
                            grants=(ct.c_uint32*16)()
                            granted=int(self.lib.iqquadptr_grant_until(grants,reusable//512*512,published,reusable))
                            high=reusable+8+skew
                            remaining=granted-high
                            minimum=min(minimum,remaining)
                            tracked=ct.c_uint64(2**64-1)
                            self.assertTrue(self.lib.check_runway(granted,high,budget,ct.byref(tracked)))
                            self.assertEqual(tracked.value,remaining)
                            self.assertEqual(grants[(granted//512)%16],0)
            minima[clock]=minimum
        self.assertEqual(minima,{128000000:6987,150000000:6872})

    def test_arbitrary_axis_skew_low_credit_runway_rejects_before_starvation(self):
        for clock in (128000000,150000000):
            budget,_=trial.poll_limits(clock)
            for base in (0,8192,2**32,2**40):
                granted=base+7680
                for remaining in (0,1,budget,2*budget-1,2*budget,2*budget+1):
                    tracked=ct.c_uint64(2**64-1)
                    ok=self.lib.check_runway(granted,granted-remaining,budget,ct.byref(tracked))
                    self.assertEqual(ok,remaining>=2*budget)
                    self.assertEqual(tracked.value,remaining)
                tracked=ct.c_uint64(9999)
                self.assertFalse(self.lib.check_runway(granted,granted+1,budget,ct.byref(tracked)))
                self.assertEqual(tracked.value,0)

    def test_both_rates_all8192_sparse_observations_and_ring_wrap(self):
        for clock in (128000000,150000000):
            for cpw in (1,2):
                r,_,lines=fixture(cpw,clock=clock)
                result=trial.validate_run(r,lines,cpw,1000,0,clock)
                self.assertEqual(result['faults'],[])
                self.assertEqual(result['classification'],'digital_live_clean')
                self.assertEqual(result['cap_checked_bus_words'],8192)

    def test_late_sample_corruption_with_valid_serial_crc_is_rejected(self):
        r,values,_=fixture();values=values.copy();values[-1]^=1<<16
        r['cap_crc']=zlib.crc32(values.tobytes())
        result=trial.validate_run(r,capture_lines(values),1,1000,0)
        self.assertEqual(result['cap_checked_bus_words'],8192)
        self.assertEqual(result['cap_mismatches'],1)
        self.assertEqual(result['cap_first_bad'],8191)
        self.assertTrue(result['faults'])

    def test_expected_starvation_needs_no_capture_and_terminal_stalls_are_optional(self):
        for stalled in (False,True):
            r,_,lines=fixture(fault=1)
            r.update(pointer_txstall_mask=3 if stalled else 0,pointer_errors=24 if stalled else 8,
                     live_pointer_errors=24 if stalled else 8,output_errors=3 if stalled else 0,
                     errors=trial.ERR_POINTER|(3 if stalled else 0))
            result=trial.validate_run(r,lines,1,1000,1)
            self.assertEqual(result['faults'],[])
            self.assertEqual(result['classification'],'credit_starvation_detected')

    def test_late_abort_retirement_uses_final_owned_snapshot_not_pre_abort_address(self):
        r,_,lines=fixture()
        r['final_input_issue_words']=[value+1 for value in r['final_input_issue_words']]
        r['reusable_words']+=1
        self.assertEqual(trial.validate_run(r,lines,1,1000,0)['faults'],[])
        r,_,lines=fixture(fault=1)
        r['final_input_issue_words']=[value+1 for value in r['final_input_issue_words']]
        self.assertTrue(trial.validate_run(r,lines,1,1000,1)['faults'])

    def test_fault_scope_boundary_read_address_and_cleanup_fail_closed(self):
        for negative in (False,True):
            r,_,lines=fixture(fault=int(negative))
            bad=[dict(table_base=0x20040020),dict(input_after_crc=0),dict(pre_abort_input_read_addr_stable=False),
                 dict(pointer_other_pio_flags=1),dict(pointer_rxstall_mask=4),
                 dict(pre_abort_input_read_addr=[0x20008800,0x20008802]),dict(restored=False),dict(selectors_restored=False),
                 dict(pointer_errors=4),dict(output_errors=16),dict(errors=1<<20),dict(max_poll_gap_us=257),
                 dict(poll_issue_budget_words=0),dict(required_credit_runway_words=0),dict(min_credit_runway_words=1),
                 dict(source_cursor_deadline_us=0),dict(max_poll_interval_us=32),dict(poll_interval_us=24),
                 dict(max_source_snapshot_span_us=257),
                 dict(core0_work_busy=0),dict(row_profile='l9_quad'),dict(cap_words=1)]
            beyond = dict(final_input_issue_words=[r['granted_words']+1,r['granted_words']],read_beyond_grants=False)
            bad.append(beyond)
            if negative:
                bad += [dict(frozen_granted_words=r['granted_words']-512),dict(null_trigger_mask=0),
                        dict(pointer_errors=24|32),dict(live_pointer_errors=16),dict(output_errors=4)]
            else:
                bad += [dict(capture_started=False),dict(capture_count=0),dict(txstalls_i=1),dict(null_trigger_mask=16),
                        dict(start_input_issue_words=[8192,8192])]
            for change in bad:
                modified=dict(r);modified.update(change)
                self.assertTrue(trial.validate_run(modified,lines,1,1000,int(negative))['faults'],change)

    def test_truncated_reordered_or_serially_corrupt_capture_fails(self):
        r,_,lines=fixture()
        for bad in (lines[:-1],[lines[1],lines[0]]+lines[2:]):
            self.assertTrue(trial.validate_run(r,bad,1,1000,0)['faults'])
        r['cap_crc']^=1
        self.assertTrue(trial.validate_run(r,lines,1,1000,0)['faults'])

    def test_frozen_firmware_identity_and_clock_response(self):
        r,_,lines=fixture()
        self.assertEqual(trial.validate_run(r,lines,1,1000,0,128000000,'frozen-test-image')['faults'],[])
        self.assertIn('git',trial.validate_run(r,lines,1,1000,0,128000000,'different-image')['faults'])
        info=dict(cmd='info',clk_hz=128000000,git='frozen-test-image')
        trial.validate_clock(info,128000,'frozen-test-image')
        for change in (dict(error='clock failed'),dict(cmd='clock'),dict(clk_hz=150000000),dict(git='different-image')):
            modified=dict(info);modified.update(change)
            with self.assertRaises(ValueError):
                trial.validate_clock(modified,128000,'frozen-test-image')


if __name__=='__main__':
    unittest.main()
