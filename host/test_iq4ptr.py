"""Execute the exact four-level pointer PIO instructions, without USB or hardware.

This checks address values, history, FIFO stalls and minimum instruction cadence.
It does not model RP2350 DMA arbitration or establish a sustained device rate.
"""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import random
import subprocess
import tempfile

from test_dacclk import Machine, firmware_program
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
MASK = 0xffffffff


def seed(previous):
    return sum(((previous >> (30 - 2 * age)) & 3) << (2 * age) for age in range(6))


def reference(words, previous, base):
    codes = [(previous >> shift) & 3 for shift in range(0, 32, 2)]
    codes += [(word >> shift) & 3 for word in words for shift in range(0, 32, 2)]
    return [base + 8 * sum(codes[16 + n - age] << (2 * age) for age in range(7))
            for n in range(16 * len(words))]


def execute(code, words, base, previous=0, limit=None, stall=None):
    regs = dict(x=seed(previous), y=base >> 17, isr=0, osr=0)
    sources = {1: "x", 2: "y", 3: None, 6: "isr", 7: "osr"}
    pc, shifted, consumed, cycles, outputs = 0, 32, 0, 0, []
    target = 16 * len(words) if limit is None else limit
    stalled = set()
    while len(outputs) < target:
        insn = code[pc]
        nxt = (pc + 1) % len(code)
        major, arg, count = insn >> 13, (insn >> 5) & 7, insn & 31
        cycles += 1
        # A blocked PULL/PUSH is retried without changing the shift registers.
        if major == 4 and stall and (len(outputs), pc) not in stalled:
            delayed = stall(len(outputs), pc)
            if delayed:
                cycles += delayed
                stalled.add((len(outputs), pc))
        if major == 0:
            assert arg == 7
            if shifted < 32:
                nxt = count
        elif major == 4:
            if insn & 0x80:
                regs["osr"], shifted = words[consumed], 0
                consumed += 1
            else:
                outputs.append(regs["isr"])
                regs["isr"] = 0
        elif major == 5:
            assert (insn >> 3) & 3 == 0
            regs[sources[arg]] = regs[sources[insn & 7]]
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
    return outputs, cycles, consumed


def check_dma_rings():
    cases = 0
    for log2 in range(8, 14):
        words, size = 1 << log2, 4 << log2
        for base in (0x20000000, 0x20018000, 0x20040000):
            base = (base + size - 1) & ~(size - 1)
            address, observed, last = base, 0, 0
            for n in range(5 * words):
                assert address == base + 4 * (n % words)
                address = (address & ~(size - 1)) | ((address + 4) & (size - 1))
                cursor = ((address - base) >> 2) & (words - 1)
                observed += (cursor - last) & (words - 1)
                last = cursor
                assert observed == n + 1
            cases += 1
    return cases


def check_axis_writers():
    # The shared row contains no axis flag; actual paired-writer opcodes put
    # GPIO14 high for I and low for Q, while consuming complete16-bit slots.
    source = (ROOT / "firmware/src/output/dual_dma.c").read_text()
    assert "state.row_words >= 4 || state.shared_axis_row" in source
    assert "shared_samples() ? &paired_i_program : &out_program" in source
    for inverted in (False, True):
        axes = [Machine(firmware_program("paired_i_code"), 0, side_bits=2, optional=True),
                Machine(firmware_program("paired_q_code"), 1, side_bits=2, optional=True)]
        clock = Machine(firmware_program("ddr_clock_code"), int(inverted), side_bits=1)
        for slot in range(8192):
            writes = [(axis, flag) for axis, machine in enumerate(axes)
                      for out, flag in [machine.step()] if out]
            _, level = clock.step()
            assert writes == [(slot & 1, 1 - (slot & 1))]
            assert level == ((1 - (slot & 1)) ^ int(inverted))
        assert axes[0].outs == axes[1].outs == 4096
    return 2 * 8192


def check_stop(directory, source):
    # Compile the actual lifecycle/cursor leaves against a tiny register model.
    # A pending source read retires during ABORT; a timeout must retain claims.
    original = (source / "output/iq4ptr.c").read_text()
    header = (source / "output/iq4ptr.h").read_text()
    def function(signature):
        start = original.index(signature)
        end, depth = original.index('{', start) + 1, 1
        while depth:
            depth += (original[end] == '{') - (original[end] == '}')
            end += 1
        return original[start:end]
    progress = header[header.index('typedef struct {'):]
    progress = progress[:progress.index('} iq4ptr_progress_t;') + len('} iq4ptr_progress_t;')]
    state = original[original.index('typedef struct {'):]
    state = state[:state.index('} pointer_state_t;') + len('} pointer_state_t;')]
    shim = r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef int PIO;
enum { IQ4PTR_ERR_POLL_GAP=16, IQ4PTR_ERR_STOP=32, IQ4PTR_ERR_OWNERSHIP=128,
       IQQUADPTR_REUSE_GUARD_WORDS=8 };
''' + progress + state + r'''
static pointer_state_t state;
static const char *last_error;
static int program;
static struct { struct { uint32_t read_addr; } ch[4]; uint32_t abort; } registers;
#define dma_hw (&registers)
static unsigned ticks, delta, timeout, cleanups, faults, posthalt_faults;
static uint64_t cleanup_latched;
static uint32_t time_us_32(void) {
 if (registers.abort && !timeout) {
  for (unsigned a=0;a<2;++a) {
   unsigned cursor=((registers.ch[a].read_addr-(uintptr_t)state.input[a])>>2)&state.mask;
   registers.ch[a].read_addr=(uintptr_t)state.input[a]+4*((cursor+delta)&state.mask);
  }
  registers.abort=0;
 }
 return ticks+=500;
}
static void sample_faults(void) { ++faults; posthalt_faults+=!state.result.running; }
static void halt(void) { state.result.running=false; }
static void dma_channel_cleanup(int ch) {
 if (!cleanups) cleanup_latched=state.result.input_words[0];
 ++cleanups; registers.ch[ch].read_addr=0;
}
static void dma_channel_unclaim(int ch) { (void)ch; }
static void pio_sm_clear_fifos(PIO pio,int sm) { (void)pio;(void)sm; }
static void pio_sm_unclaim(PIO pio,int sm) { (void)pio;(void)sm; }
static void pio_remove_program(PIO pio,const int *p,unsigned o) { (void)pio;(void)p;(void)o; }
''' + function('static void sample_cursors(') + function('void iq4ptr_stop(') + r'''
void check_stop(uint64_t before,unsigned advance,unsigned flags,uint64_t *out) {
 ticks=cleanups=faults=posthalt_faults=0;cleanup_latched=0;delta=advance;timeout=flags&4;registers.abort=0;
 // flags&8: live mode with credit channels2/3, granted/published exactly up to before+1.
 const bool live=flags&8;
 state=(pointer_state_t){.pio=2,.input={(const uint32_t *)(uintptr_t)0x20020000,
  (const uint32_t *)(uintptr_t)0x20024000},.sm={0,1},.dma={0,1},
  .credit_dma={live?2:-1,live?3:-1},.live=live,.mask=4095,.sm_mask=3,.dma_mask=live?15:3,
  .last={(uint32_t)before&4095,(uint32_t)before&4095},.owned=true,.added=!(flags&2),
  .result={.input_words={before,before},.granted_words=before+1,.published_words=before+1,
   .running=!(flags&1)}};
 for(unsigned a=0;a<2;++a)registers.ch[a].read_addr=0x20020000+0x4000*a+4*((uint32_t)before&4095);
 iq4ptr_stop();
 out[0]=state.result.input_words[0];out[1]=state.result.input_words[1];out[2]=state.result.errors;
 out[3]=state.owned;out[4]=cleanups;out[5]=faults;out[6]=posthalt_faults;out[7]=cleanup_latched;
}
'''
    path = directory / "stop.c"
    path.write_text(shim)
    library = directory / "stop.so"
    subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    str(path), "-o", str(library)], check=True)
    lib = ctypes.CDLL(str(library))
    lib.check_stop.argtypes = [ctypes.c_uint64, ctypes.c_uint, ctypes.c_uint,
                              ctypes.POINTER(ctypes.c_uint64)]
    cases = 0
    for before in (0, 511, 4095, 4096, 2**32 - 1, 2**40 + 511):
        for advance in (0, 1, 4):
            for halted in (False, True):
                result = (ctypes.c_uint64 * 8)()
                lib.check_stop(before, advance, int(halted), result)
                final = before + advance
                assert list(result) == [final, final, 0, 0, 2, int(not halted), 0, final]
                cases += 1
    result = (ctypes.c_uint64 * 8)()
    lib.check_stop(511, 1, 3, result) # incomplete prepare has no configured cursors
    assert list(result) == [511, 511, 0, 0, 2, 0, 0, 511]
    lib.check_stop(511, 1, 4, result) # ABORT timeout retains claims
    assert list(result) == [511, 511, 32, 1, 0, 1, 0, 0]
    # Live mode releases the two credit channels too. A read retiring during ABORT may
    # reach the granted bound but never pass it; passing it is an ownership fault.
    for advance, errors in ((0, 0), (1, 0), (2, 128), (4, 128)):
        lib.check_stop(511, advance, 8, result)
        assert list(result) == [511 + advance, 511 + advance, errors, 0, 4, 1, 0, 511 + advance], list(result)
        cases += 1
    return cases + 2


def main():
    source = ROOT / "firmware/src"
    with tempfile.TemporaryDirectory(prefix="iq4ptr-native-") as directory:
        directory = Path(directory)
        shim = directory / "program.c"
        shim.write_text('#include "iq4ptr.h"\n'
                        'const uint16_t *test_program(void) { return IQ4PTR_PROGRAM; }\n'
                        'unsigned test_length(void) { return IQ4PTR_PROGRAM_LENGTH; }\n'
                        'unsigned test_cycles(void) { return IQ4PTR_CYCLES_PER_WORD; }\n')
        library = directory / "iq4ptr.so"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        *fwsrc.INCLUDES, str(source / "output/iq4ptr.c"), str(shim), "-o", str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.test_program.restype = ctypes.POINTER(ctypes.c_uint16)
        lib.iq4ptr_seed.argtypes = [ctypes.c_uint32]
        lib.iq4ptr_seed.restype = ctypes.c_uint32
        code = list(lib.test_program()[:lib.test_length()])
        for history in range(4096):
            previous = sum(((history >> (2 * age)) & 3) << (30 - 2 * age) for age in range(6))
            for irrelevant in (0, 0xfffff, 0x93526):
                assert lib.iq4ptr_seed(previous | irrelevant) == history
        for key in range(16384):
            previous = sum(((key >> (2 + 2 * age)) & 3) << (30 - 2 * age) for age in range(6))
            for base in range(0x20000000, 0x20080000, 0x20000):
                got, cycles, used = execute(code, [key & 3], base, previous, 1)
                assert got == [base + 8 * key] and cycles == 10 and used == 1
        rng = random.Random(0x4_7_2350)
        cases, stall_cases = 0, 0
        for length in (1, 2, 3, 7, 31, 257):
            words = [rng.getrandbits(32) for _ in range(length)]
            for previous in (0, MASK, 0xa531c976):
                for base in range(0x20000000, 0x20080000, 0x20000):
                    expected = reference(words, previous, base)
                    got, cycles, used = execute(code, words, base, previous)
                    assert got == expected and cycles == length * 145 and used == length
                    assert cycles == length * lib.test_cycles()
                    cases += 1
            delay = lambda n, pc: (n * 17 + pc * 5) % 13
            got, cycles, used = execute(code, words, 0x20040000, MASK, stall=delay)
            assert got == reference(words, MASK, 0x20040000) and cycles > length * 145 and used == length
            stall_cases += 1
        # The last word seeds an immutable ring, so its first six outputs retain
        # the preceding lap exactly; check multiple laps with a nonzero seam.
        words = [rng.getrandbits(32) for _ in range(17)]
        got, _, _ = execute(code, words * 5, 0x20000000, words[-1])
        lap = reference(words, words[-1], 0x20000000)
        assert got == lap * 5
        stop_cases = check_stop(directory, source)
    print(json.dumps(dict(pio_words=len(code), all_keys=16384, table_bases=4,
                          prehistory_cases=4096 * 3, stream_cases=cases, fifo_stall_cases=stall_cases,
                          cycles_per_input_word=145, cycles_per_symbol=145 / 16,
                          cyclic_laps=5, dma_ring_cases=check_dma_rings(),
                          writer_clock_slots=check_axis_writers(), post_abort_stop_cases=stop_cases,
                          device_rate_measured=False)))


if __name__ == "__main__":
    main()
