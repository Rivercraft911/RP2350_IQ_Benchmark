"""Native table/credit tests and exact PIO model for the raw-input quad producer.

No USB or shared firmware build. DMA address arithmetic is modeled; its real
arbitration, FIFO deadlines and the output engine still require device evidence.
"""
from __future__ import annotations

import ctypes
import json
import random
import re
import subprocess
import tempfile
from types import SimpleNamespace
import zlib
from pathlib import Path

import numpy as np
import test_iqquad as quad

ROOT = Path(__file__).resolve().parents[1]
MASK = 0xffffffff
BASE = 0x20040000
Q, N = 512, 16
PTR = ctypes.POINTER(ctypes.c_uint32)


class PointerProfile(ctypes.Structure):
    _fields_ = [("program", ctypes.POINTER(ctypes.c_uint16)),
                ("program_length", ctypes.c_uint), ("cycles_per_input_word", ctypes.c_uint),
                ("table_alignment", ctypes.c_uint), ("table_crc", ctypes.c_uint32),
                ("control_bswap", ctypes.c_bool)]


def bswap(value):
    return int.from_bytes(value.to_bytes(4, "little"), "big")


def execute(code, words, base, previous=0, limit=None, *, control_bswap=False, raw_fifo=False):
    # X contains reversed nibble groups; bit order within each nibble is unchanged.
    seed = ((previous >> 12) & 15) | ((previous >> 4) & 0xf0)
    upper = base >> 16
    y = ((upper & 255) << 8) | (upper >> 8) if control_bswap else base >> 17
    regs = dict(x=seed, y=y, isr=0, osr=0)
    sources = {1: "x", 2: "y", 3: None, 6: "isr", 7: "osr"}
    pc, shifted, consumed, cycles, result = 0, 32, 0, 0, []
    target = len(words) * 4 if limit is None else limit
    while len(result) < target:
        insn = code[pc]
        nxt = (pc + 1) % len(code)
        major, arg, count = insn >> 13, (insn >> 5) & 7, insn & 31
        cycles += 1
        if major == 0:
            assert arg == 7
            if shifted < 16:
                nxt = count
        elif major == 4:
            if insn & 0x80:
                word = words[consumed]
                consumed += 1
                regs["osr"], shifted = word | word << 16, 0
            else:
                value = regs["isr"]
                result.append(bswap(value) if control_bswap and not raw_fifo else value)
                regs["isr"] = 0
        elif major == 5:
            operation = (insn >> 3) & 3
            assert operation in (0, 2)
            value = regs[sources[insn & 7]]
            regs[sources[arg]] = int(f"{value:032b}"[::-1], 2) if operation == 2 else value
        elif major == 2:
            value = regs[sources[arg]] if sources[arg] else 0
            regs["isr"] = ((regs["isr"] << count) | (value & ((1 << count) - 1))) & MASK
        elif major == 3:
            assert arg == 3
            regs["osr"] >>= count
            shifted = min(32, shifted + count)
        else:
            raise AssertionError(f"unknown opcode {insn:04x}")
        pc = nxt
    return result, cycles, consumed


def reverse_nibbles(key):
    return ((key & 15) << 8) | (key & 0xf0) | ((key >> 8) & 15)


def l8_reverse_groups(key):
    return (key & 7) | (((key >> 3) & 15) << 7) | (((key >> 7) & 15) << 3)


def reference(words, previous, base, span=9):
    bits = [(previous >> k) & 1 for k in range(16)]
    bits += [(word >> k) & 1 for word in words for k in range(16)]
    permutation = reverse_nibbles if span == 9 else l8_reverse_groups
    return [base + (permutation(sum(bits[16 + n - (span - 1) + k] << k for k in range(span + 3))) << 5)
            for n in range(0, 16 * len(words), 4)]


def check_l8_profile(lib, frame_words):
    info = lib.iqquadptr_profile_info(1).contents
    assert (info.program_length, info.cycles_per_input_word, info.table_alignment,
            info.table_crc, info.control_bswap) == (13, 49, 65536, 0xccad7f5a, True)
    old = lib.iqquadptr_profile_info(0).contents
    assert (old.program_length, old.cycles_per_input_word, old.table_alignment,
            old.table_crc, old.control_bswap) == (10, 37, 131072, 0x9da111d1, False)
    code = list(info.program[:info.program_length])
    for bad in (-1, 2, 0x7fffffff):
        assert not lib.iqquadptr_profile_info(bad)
        assert lib.iqquadptr_key_profile(0, bad) == MASK
    guarded = (ctypes.c_uint32 * (4096 * 8 + 2))(*([quad.CANARY] * (4096 * 8 + 2)))
    out = ctypes.cast(ctypes.byref(guarded, 4), PTR)
    for bad in (-1, 2):
        assert not lib.iqquadptr_build_table_profile(out, 4096 * 8, bad)
    assert not lib.iqquadptr_build_table_profile(out, 2048 * 8 - 1, 1)
    assert not lib.iqquadptr_build_table_profile(None, 2048 * 8, 1)
    assert all(value == quad.CANARY for value in guarded)
    assert lib.iqquadptr_build_table_profile(out, 2048 * 8, 1)
    assert guarded[0] == quad.CANARY and all(v == quad.CANARY for v in guarded[2048 * 8 + 1:])
    rows = np.ctypeslib.as_array(guarded)[1:2048 * 8 + 1].copy().reshape(2048, 8)
    pulse = SimpleNamespace(L=8, sps=4, coef=quad.L8_COEFFICIENTS)
    permutation = np.array([l8_reverse_groups(key) for key in range(2048)])
    assert np.array_equal(permutation[permutation], np.arange(2048))
    np.testing.assert_array_equal(rows, quad.fir_rows(pulse)[permutation])
    assert zlib.crc32(rows.astype("<u4").tobytes()) == info.table_crc
    for key in range(2048):
        assert lib.iqquadptr_key_profile(key, 1) == l8_reverse_groups(key)
        previous, word = (key & 127) << 9, key >> 7
        got, _, _ = execute(code, [word], BASE, previous, 1, control_bswap=True)
        assert got == [BASE + 32 * l8_reverse_groups(key)]
    for previous in range(65536):
        word = (previous * 0x9e37) & 0xffff
        got, _, _ = execute(code, [word], BASE, previous, 1, control_bswap=True)
        key = ((previous >> 9) & 127) | ((word & 15) << 7)
        assert got == [BASE + 32 * l8_reverse_groups(key)]
    rng = random.Random(0x8_2350)
    cases = 0
    for n in (1, 2, 3, 7, 31, 257):
        packed = [rng.getrandbits(32) for _ in range(n)]
        for previous in (0, 0xffff, 0xa53c):
            for base in (0x20000000, 0x20010000, BASE, 0x20070000):
                for axis in range(2):
                    words = [(word >> (16 * axis)) & 0xffff for word in packed]
                    expected = reference(words, previous, base, 8)
                    got, cycles, consumed = execute(code, words, base, previous, control_bswap=True)
                    assert got == expected and cycles == n * 49 and consumed == n
                    raw, _, _ = execute(code, words, base, previous, control_bswap=True, raw_fifo=True)
                    assert [bswap(value) for value in raw] == expected
                    cases += 1
    axis_rows = []
    for axis in range(2):
        addresses, _, _ = execute(code, [(int(w) >> (16 * axis)) & 0xffff for w in frame_words],
                                  BASE, control_bswap=True)
        axis_rows.append(rows[(np.asarray(addresses, dtype=np.uint32) - BASE) // 32])
    wave = np.stack([axis_rows[0] | np.uint32(0x40004000), axis_rows[1]], axis=-1).ravel()
    np.testing.assert_array_equal(wave, quad.scalar_waveform(frame_words, 0, quad.L8_COEFFICIENTS))
    # Reproducible first hardware fixture: one credited batch, nonzero history,
    # and expected numerical RX values before and after mandatory control BSWAP.
    raw_words = quad.m.xorshift32(0x1234abcd, Q)
    raw_words[:4] = [0, MASK, 0x5555aaaa, 0x716bc429]
    fixture = dict(profile="qpsk_l8_n4_both_m010_far2", table_base=BASE,
                   previous_packed_word=0x716bc429, input_words=raw_words.tolist(),
                   pio_program=code, control_bswap=True, axes=[])
    for axis in range(2):
        raw, _, _ = execute(code, [(int(w) >> (16 * axis)) & 0xffff for w in raw_words], BASE,
                             (0x716bc429 >> (16 * axis)) & 0xffff, control_bswap=True, raw_fifo=True)
        addresses = [bswap(v) for v in raw]
        assert addresses == reference([(int(w) >> (16 * axis)) & 0xffff for w in raw_words],
                                      (0x716bc429 >> (16 * axis)) & 0xffff, BASE, 8)
        fixture['axes'].append(dict(raw_rx_fifo_prefix=raw[:64], address_prefix=addresses[:64],
                                    raw_rx_fifo_crc32=zlib.crc32(np.array(raw, '<u4').tobytes()),
                                    address_crc32=zlib.crc32(np.array(addresses, '<u4').tobytes()),
                                    total_addresses=len(raw)))
    path = Path(tempfile.gettempdir()) / "yapogee-l8-pointer-address-fixture.json"
    path.write_text(json.dumps(fixture, indent=2) + "\n")
    return dict(profile=fixture['profile'], pio_words=len(code), isa_cases=cases,
                all_keys=2048, prehistory_cases=65536, cycles_per_symbol=49 / 16,
                table_crc=info.table_crc, full_frame_waveform_crc=zlib.crc32(wave.tobytes()),
                hardware_fixture=str(path), fixture_address_crcs=[a['address_crc32'] for a in fixture['axes']])


def check_credit_stalls(lib):
    rng = random.Random(0x2350)
    longest = lead = 0
    for _ in range(200):
        grants = (ctypes.c_uint32 * N)()
        done, stopped = [0, 0], [False, False]
        granted, published = 0, 8 * Q
        stall_batch = rng.randrange(40, 200)
        for tick in range(5000):
            if min(done) <= stall_batch:
                published = max(published, (min(done) + N - 2) * Q)
                reusable = max(0, min(done) * Q - 8)
                granted = int(lib.iqquadptr_grant_until(grants, granted, published, reusable))
                assert granted == min(published // Q, reusable // Q + N - 1) * Q
                for batch in range(min(done), granted // Q):
                    assert grants[batch % N] == Q
                assert grants[(granted // Q) % N] == 0
            stalling = min(done) >= stall_batch
            for axis in range(2):
                # Hold Q indefinitely after many wraps while I runs without a
                # lead bound; only release Q after I has stopped on the sentinel.
                if stalling and axis == 1 and not stopped[0]:
                    continue
                allowed = stalling or done[axis] <= min(done) + 2
                if not stopped[axis] and allowed and rng.randrange(3) == 0:
                    count = grants[done[axis] % N]
                    if count == 0:
                        stopped[axis] = True
                    else:
                        assert count == Q
                        assert done[axis] * Q < granted <= published
                        done[axis] += 1
                        lead = max(lead, abs(done[0] - done[1]))
            assert max(done) * Q <= published
            if all(stopped):
                break
        assert all(stopped) and min(done) >= stall_batch
        longest = max(longest, max(done))
    assert lead == N - 2
    return longest, lead


def check_credit_microsteps(directory, source):
    # Invoke a consumer observation after each release fence in the actual C leaf.
    # Instrument only the hook, retaining its source stores, bounds and arithmetic.
    original = (source / "iqquadptr.c").read_text()
    macro = "#define RELEASE() atomic_thread_fence(memory_order_release)"
    assert original.count(macro) == 1
    instrumented = original.replace(macro,
        "extern void test_credit_step(void);\n"
        "#define RELEASE() do { atomic_thread_fence(memory_order_release); test_credit_step(); } while (0)")
    (directory / "credit_steps.c").write_text(instrumented)
    (directory / "credit_hook.c").write_text(
        "typedef void (*hook_fn)(void);\nstatic hook_fn hook;\n"
        "void set_credit_hook(hook_fn next) { hook = next; }\n"
        "void test_credit_step(void) { if (hook) hook(); }\n")
    library = directory / "credit_steps.so"
    subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    f"-I{source}", str(source / "iqquad.c"), str(directory / "credit_steps.c"),
                    str(directory / "credit_hook.c"), "-o", str(library)], check=True)
    lib = ctypes.CDLL(str(library))
    hook_type = ctypes.CFUNCTYPE(None)
    lib.set_credit_hook.argtypes = [hook_type]
    lib.iqquadptr_grant_until.argtypes = [PTR] + [ctypes.c_uint64] * 3
    lib.iqquadptr_grant_until.restype = ctypes.c_uint64
    cases = 0
    for batch in (1, 16, 31, 160, 1024):
        for release_stage in (0, 1):
            grants = (ctypes.c_uint32 * N)(*([Q] * N))
            grants[batch % N] = 0
            seen = []
            def observe():
                seen.append((grants[batch % N], grants[(batch + 1) % N]))
            callback = hook_type(observe)
            lib.set_credit_hook(callback)
            reusable = max(0, batch - (N - 2)) * Q
            granted = int(lib.iqquadptr_grant_until(grants, batch * Q, (batch + 1) * Q, reusable))
            assert seen == [(0, 0), (Q, 0)]
            assert granted == (batch + 1) * Q
            # DMA captures its count at the selected fence. Replacing SRAM later
            # cannot change an already-fetched zero or automatically retrigger it.
            fetched = seen[release_stage][0]
            stopped = fetched == 0
            assert stopped == (release_stage == 0)
            if not stopped:
                assert batch * Q + fetched <= (batch + 1) * Q
            cases += 1
    return cases


def check_stride():
    for base in (0x20000000, 0x20040000):
        for axis in range(2):
            initial = base + 2 * axis
            address, previous, total = initial, 0, 0
            for n in range(5 * 8192):
                assert address == base + 4 * (n % 8192) + 2 * axis
                address = (address & ~32767) | ((address + 4) & 32767)
                cursor = ((address - initial) & 32767) >> 2
                total += (cursor - previous) & 8191
                previous = cursor
                assert total == n + 1


def check_stop_lifecycle(directory, source):
    original = (source / "iqquadptr.c").read_text()
    header = (source / "iqquadptr.h").read_text()
    def function(signature):
        start = original.index(signature)
        end, depth = original.index('{', start) + 1, 1
        while depth:
            depth += (original[end] == '{') - (original[end] == '}')
            end += 1
        return original[start:end]
    progress = header[header.index('typedef struct {', header.index('IQQUADPTR_ERR_CONFIG')):]
    progress = progress[:progress.index('} iqquadptr_progress_t;') + len('} iqquadptr_progress_t;')]
    state = original[original.index('typedef struct {'):]
    state = state[:state.index('} pointer_state_t;') + len('} pointer_state_t;')]
    # Compile the actual stop and cursor functions. time_us_32 models a pending
    # source read retiring while ABORT is active, before its completion fence.
    shim = r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "iqquadptr.h"
typedef int PIO;
typedef struct { const uint16_t *instructions; unsigned length; int origin; } pio_program_t;
enum { IQQUADPTR_ERR_POLL_GAP=32, IQQUADPTR_ERR_OWNERSHIP=64, IQQUADPTR_ERR_STOP=128 };
''' + progress + state + r'''
static pointer_state_t state;
static const char *last_error;
static struct { struct { uint32_t read_addr; } ch[4]; uint32_t abort; } registers;
#define dma_hw (&registers)
static unsigned ticks, delta, timeout, cleanups, faults, posthalt_faults;
static uint64_t cleanup_latched;
static uint32_t time_us_32(void) {
 if (registers.abort && !timeout) {
  for (unsigned a=0;a<2;++a) {
   uint32_t cursor=((registers.ch[a].read_addr-(uintptr_t)state.input-2*a)>>2)&state.mask;
   registers.ch[a].read_addr=(uintptr_t)state.input+2*a+4*((cursor+delta)&state.mask);
  }
  registers.abort=0;
 }
 return ticks+=500;
}
static void sample_faults(void) { ++faults; posthalt_faults+=!state.result.running; }
static void halt(void) { state.result.running=false; }
static void dma_channel_cleanup(int ch) {
 if (!cleanups) cleanup_latched=state.result.input_i_words;
 ++cleanups; registers.ch[ch].read_addr=0; // a released channel is no longer a valid cursor
}
static void dma_channel_unclaim(int ch) { (void)ch; }
static void pio_sm_clear_fifos(PIO pio,int sm) { (void)pio;(void)sm; }
static void pio_sm_unclaim(PIO pio,int sm) { (void)pio;(void)sm; }
static void pio_remove_program(PIO pio,const pio_program_t *p,unsigned o) { (void)pio;(void)p;(void)o; }
''' + function('static void sample_cursors(') + function('void iqquadptr_stop(') + r'''
void check_stop(uint64_t before,unsigned advance,uint64_t granted,uint64_t published,unsigned flags,uint64_t *out) {
 static const iqquadptr_profile_info_t profile={.program=IQQUADPTR_L8_PROGRAM,.program_length=13};
 ticks=cleanups=faults=posthalt_faults=0;cleanup_latched=0;delta=advance;timeout=flags&4;registers.abort=0;
 state=(pointer_state_t){.pio=2,.input=(const uint32_t *)(uintptr_t)0x20008000,
  .sm={0,1},.input_dma={0,1},.credit_dma={2,3},.mask=8191,.words=8192,.sm_mask=3,.dma_mask=15,
  .last={(uint32_t)before&8191,(uint32_t)before&8191},.owned=true,.added=!(flags&2),.profile=&profile,
  .result={.input_i_words=before,.input_q_words=before,.published_words=published,.granted_words=granted,
   .running=!(flags&1)}};
 for(unsigned a=0;a<2;++a)registers.ch[a].read_addr=0x20008000+2*a+4*((uint32_t)before&8191);
 iqquadptr_stop();
 out[0]=state.result.input_i_words;out[1]=state.result.input_q_words;out[2]=state.result.reusable_words;
 out[3]=state.result.errors;out[4]=state.owned;out[5]=cleanups;out[6]=faults;out[7]=posthalt_faults;
 out[8]=cleanup_latched;
}
'''
    (directory / 'stop.c').write_text(shim)
    library = directory / 'stop.so'
    subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                    f'-I{source}',str(directory/'stop.c'),'-o',str(library)],check=True)
    lib = ctypes.CDLL(str(library))
    lib.check_stop.argtypes = [ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint64,ctypes.c_uint64,
                              ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)]
    cases = 0
    for before in (0,511,8191,8192,2**32-1,2**40+511):
        for advance in (0,1,4):
            for already_halted in (False,True):
                for beyond in ('neither','grant','publication'):
                    final = before+advance
                    granted = before if beyond=='grant' else final
                    published = before if beyond=='publication' else final
                    result = (ctypes.c_uint64*9)()
                    lib.check_stop(before,advance,granted,published,int(already_halted),result)
                    assert list(result[:3]) == [final,final,max(final-8,0)]
                    assert result[3] == (64 if advance and beyond!='neither' else 0)
                    assert list(result[4:8]) == [0,4,int(not already_halted),0]
                    assert result[8] == final
                    cases += 1
    result = (ctypes.c_uint64*9)()
    lib.check_stop(511,1,512,512,3,result) # incomplete prepare: no source cursor latch
    assert list(result[:2]) == [511,511] and list(result[3:8]) == [0,0,4,0,0]
    lib.check_stop(511,1,512,512,4,result) # timeout retains all claims and memory
    assert list(result[:2]) == [511,511] and list(result[3:8]) == [128,1,0,1,0]
    return cases+2


def main():
    source = ROOT / "firmware/src"
    with tempfile.TemporaryDirectory(prefix="iqquadptr-native-") as directory:
        directory = Path(directory)
        shim = directory / "program.c"
        shim.write_text('#include "iqquadptr.h"\n'
                        'const uint16_t *test_program(void) { return IQQUADPTR_PROGRAM; }\n')
        library = directory / "quadptr.so"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        f"-I{source}", str(source / "iqquad.c"), str(source / "iqquadptr.c"),
                        str(shim), "-o", str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.test_program.restype = ctypes.POINTER(ctypes.c_uint16)
        lib.iqquadptr_build_table.argtypes = [PTR]
        lib.iqquadptr_build_table_profile.argtypes = [PTR, ctypes.c_uint, ctypes.c_int]
        lib.iqquadptr_build_table_profile.restype = ctypes.c_bool
        lib.iqquadptr_profile_info.argtypes = [ctypes.c_int]
        lib.iqquadptr_profile_info.restype = ctypes.POINTER(PointerProfile)
        lib.iqquadptr_key.argtypes = [ctypes.c_uint32]
        lib.iqquadptr_key_profile.argtypes = [ctypes.c_uint32, ctypes.c_int]
        lib.iqquadptr_key_profile.restype = ctypes.c_uint32
        lib.iqquadptr_seed.argtypes = [ctypes.c_uint16]
        lib.iqquadptr_grant_until.argtypes = [PTR] + [ctypes.c_uint64] * 3
        lib.iqquadptr_grant_until.restype = ctypes.c_uint64
        code = [lib.test_program()[i] for i in range(10)]
        table = (ctypes.c_uint32 * (4096 * 8))()
        lib.iqquadptr_build_table(table)
        rows = np.ctypeslib.as_array(table).copy().reshape(4096, 8)
        lut = quad.m.Lut(.2, 4, 9, .8, 1.0)
        normal = quad.fir_rows(lut)
        permutation = np.array([reverse_nibbles(key) for key in range(4096)])
        np.testing.assert_array_equal(rows, normal[permutation])
        table_crc = zlib.crc32(rows.astype("<u4").tobytes())
        expected_crc = int(re.search(r"IQQUADPTR_TABLE_CRC\s+0x([0-9a-fA-F]+)",
                                    (source / "iqquadptr.h").read_text())[1], 16)
        assert table_crc == expected_crc
        for key in range(4096):
            assert lib.iqquadptr_key(key) == reverse_nibbles(key)
            previous = (reverse_nibbles(key) & 255) << 8
            got, _, _ = execute(code, [key & 15], BASE, previous, 1)
            assert got == [BASE + 32 * key]
        for prev in range(65536):
            assert lib.iqquadptr_seed(prev) == ((prev >> 12) & 15) | ((prev >> 4) & 0xf0)
        rng = random.Random(0x2350)
        cases = 0
        for n in (1, 2, 3, 7, 31, 257):
            for previous in (0, 0xffff, 0xa53c):
                packed = [rng.getrandbits(32) for _ in range(n)]
                for base in (0x20000000, 0x20020000, BASE, 0x20060000):
                    for axis in range(2):
                        half = [(word >> (16 * axis)) & 0xffff for word in packed]
                        got, cycles, consumed = execute(code, half, base, previous)
                        assert got == reference(half, previous, base)
                        assert cycles == n * 37 and consumed == n
                        cases += 1
        waves = []
        for seed in (0, 1, 0x1234abcd, MASK):
            words = quad.m.xorshift32(seed, 1027)
            axis_rows = []
            for axis in range(2):
                addresses, _, _ = execute(code, [int(w >> (16 * axis)) & 0xffff for w in words], BASE)
                axis_rows.append(rows[(np.asarray(addresses, dtype=np.uint32) - BASE) // 32])
            got = np.stack([axis_rows[0] | np.uint32(0x40004000), axis_rows[1]], axis=-1).ravel()
            np.testing.assert_array_equal(got, quad.m.to_layout(quad.m.generate(words, lut), quad.m.LAYOUT_PAIRS))
            waves.append(zlib.crc32(got.tobytes()))
        c = quad.s2.code("2/3")
        bb = quad.m.xorshift32(0x9e3779b9 ^ 5, c.kbch // 32)
        frames = []
        for index in range(3):
            raw = bb.copy()
            if index & 1:
                raw[::64] ^= np.uint32(0x80000000)
            bits = ((raw[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()
            frames.append(quad.s2.plframe(quad.s2.fecframe(bits, "2/3"), "2/3", pilots=True))
        words = quad.symbol_words([np.concatenate([frame[axis] for frame in frames]) for axis in range(2)])
        axis_rows = []
        for axis in range(2):
            addresses, _, _ = execute(code, [int(w >> (16 * axis)) & 0xffff for w in words], BASE)
            axis_rows.append(rows[(np.asarray(addresses, dtype=np.uint32) - BASE) // 32])
        frame_wave = np.stack([axis_rows[0] | np.uint32(0x40004000), axis_rows[1]], axis=-1).ravel()
        np.testing.assert_array_equal(frame_wave, quad.m.to_layout(quad.m.generate(words, lut), quad.m.LAYOUT_PAIRS))
        frame_crc = zlib.crc32(frame_wave.tobytes())
        l8 = check_l8_profile(lib, words)
        longest, max_lead = check_credit_stalls(lib)
        microsteps = check_credit_microsteps(directory, source)
        stop_cases = check_stop_lifecycle(directory, source)
    check_stride()
    print(json.dumps(dict(pio_words=len(code), isa_cases=cases, all_keys=4096, seeds=65536,
                          cycles_per_symbol=37 / 16, table_crc=table_crc, waveform_crc=waves,
                          full_frame_waveform_crc=frame_crc,
                          credit_stall_runs=200, longest_safe_stall_batch=longest,
                          max_axis_lead_batches=max_lead, publication_microstep_cases=microsteps,
                          post_abort_stop_lifecycle_cases=stop_cases,
                          dma_ring_wraps_per_case=5, credit_bytes=N * 4, l8_profile=l8)))


if __name__ == "__main__":
    main()
