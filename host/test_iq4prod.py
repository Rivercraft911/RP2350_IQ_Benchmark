"""Native B6 producer opcodes against scalar history/FIR and four-writer slots.

The C instruction model implements RP2350 section11.5.4 refill/push retries,
including empty-OUT's extra cycle and an IN which must only shift once while
RX is full. These are logical proofs, not DMA/M33 throughput measurements.
"""
from __future__ import annotations

import json
from pathlib import Path
import random
import subprocess
import tempfile
import zlib

import numpy as np

from pulse_profiles import iq4hist8_rows, iq4hist_prbs
from test_iq4quad import cyclic_scalar
from test_quad_rows import Writer
from test_dual_pio import firmware_program
from test_dual_faults import function_body

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"


def reference(words, previous, base):
    """Direct chronological windows, independently of X/ISR shift updates."""
    codes = [(previous >> (2 * n)) & 3 for n in range(16)]
    codes += [(word >> (2 * n)) & 3 for word in words for n in range(16)]
    return np.asarray([base + 4 * sum(codes[16 + n - age] * 4 ** age for age in range(7))
                       for n in range(16 * len(words))], dtype="<u4")


def stream(executable, words, previous, base, parity, delays):
    packed = np.asarray(words, dtype="<u4")
    raw = np.frombuffer(subprocess.check_output(
        [str(executable), "stream", hex(base), str(parity), str(previous), str(delays)],
        input=packed.tobytes()), dtype="<u4")
    magic, count, low, high, pulled, out_stalls, in_stalls, retired = map(int, raw[:8])
    assert magic == 0x62367038 and count == len(words) * 8 and len(raw) == 8 + count
    assert pulled == len(words) and retired == 88 * len(words)
    cycles = low + (high << 32)
    assert cycles >= retired
    if delays == 0:
        assert cycles == retired and not out_stalls and not in_stalls
    if delays == 1 and len(words) > 1:
        assert out_stalls and not in_stalls
    if delays & 2:
        assert in_stalls
    return raw[8:]


class PointerWriter(Writer):
    def __init__(self, code, stream_index, pc, x, pointers, table, base):
        super().__init__(code, stream_index, 8, pc, x)
        self.addresses, self.table, self.base = pointers, table, base

    def pull(self):
        address = int(self.addresses[self.words % len(self.addresses)])
        self.osr = int(self.table[(address - self.base) // 4])
        self.words += 1
        self.shifts = 0


def writers(executable):
    """Actual producer rows feed actual B5 writer instructions, with two wraps."""
    symbols, base = 256 * 16, 0x20040000
    codes = iq4hist_prbs(0x1234abcd, symbols)
    packed = np.asarray([[sum(int(codes[a, 16 * w + n]) << (2 * n) for n in range(16))
                          for w in range(symbols // 16)] for a in range(2)], dtype="<u4")
    rings = [stream(executable, packed[s & 1], int(packed[s & 1, -1]), base, s // 2, 3)
             for s in range(4)]
    table, scalar = iq4hist8_rows(), cyclic_scalar(codes, 8)
    starts, initial_x = firmware_program("quad_row_start"), firmware_program("quad_row_initial_x")
    programs = [firmware_program(f"quad_row8_{axis}_code") for axis in "iq"]
    clock = firmware_program("ddr_clock_code")
    slots = 0
    for cpw in (1, 2):
        for invert in (0, 1):
            machines = [PointerWriter(programs[s & 1], s, starts[s], initial_x[s], rings[s], table, base)
                        for s in range(4)]
            for machine in machines:
                machine.pull()
            for tick in range(2 * symbols * 8):
                got, flags = [], []
                for machine in machines:
                    data, flag = machine.step()
                    if data is not None:
                        got.append((machine.stream, data))
                    if flag is not None:
                        flags.append((machine.stream, flag))
                axis, symbol, phase = tick & 1, (tick // 8) % symbols, (tick // 2) % 4
                owner = 2 * (symbol & 1) + axis
                sample = (int(scalar[axis, symbol, phase]) & 255) << 6
                assert got == [(owner, sample)] and flags == [(owner, 1 - axis)]
                ddr = (clock[(tick + invert) & 1] >> 12) & 1
                assert ddr == (1 - axis) ^ invert
                bus = sample | (flags[0][1] << 14)
                assert bus & 0x803f == 0 and ((bus >> 14) ^ ddr) == invert
                slots += cpw
    return dict(period_symbols=symbols, pointer_crc=zlib.crc32(np.asarray(rings, dtype="<u4").tobytes()),
                writer_slots=slots, cyclic_laps=2)


def finite_dma_policy(directory):
    """Execute the actual worker/publication code with finite DMA completions.

    DMA progress is deliberately asynchronous across the four streams. The
    token in each physical ring slot identifies its logical source word, so
    unpublished reads or overwrites of an outstanding read fail immediately.
    The schedule is a functional ownership test, not an M33 timing model.
    """
    source = (SRC / "iq4prod_dma.c").read_text()
    header = (SRC / "iq4prod_dma.h").read_text()
    enums = header[header.index("enum {"):header.index("};") + 2]
    progress_type = header[header.index("typedef struct {"):header.index("} iq4prod_progress_t;") +
                           len("} iq4prod_progress_t;")]
    snapshot_type = source[source.index("typedef struct {"):source.index("} worker_snapshot_t;") +
                           len("} worker_snapshot_t;")]
    progress = function_body(source, "iq4prod_dma_progress")
    start = progress.index("if (state.live) {")
    stop, depth = progress.index("{", start) + 1, 1
    while depth:
        depth += (progress[stop] == "{") - (progress[stop] == "}")
        stop += 1
    # The cyclic path has separate cursor tests; preserve this live branch and
    # its exact minimum-completed calculation/fault tail without hardware APIs.
    progress = progress[:stop] + "\n" + progress[progress.rindex("if (state.result.errors)"):]
    fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
''' + enums + progress_type + snapshot_type + r'''
enum { WORKER_POLL_CYCLES=64, FAULT_POLL_ROUNDS=32,
       DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS=1u<<31, DMA_CH0_CTRL_TRIG_BUSY_BITS=1u<<24,
       PIO_FDEBUG_TXSTALL_LSB=24, PIO_FDEBUG_RXSTALL_LSB=16,
       PIO_FDEBUG_TXOVER_LSB=8, PIO_FDEBUG_RXUNDER_LSB=0 };
static struct { struct { uint32_t ctrl_trig, al1_transfer_count_trig; } ch[4]; } registers;
#define dma_hw (&registers)
static struct { uint32_t fdebug; } pio_ports[2];
#define pio0 (&pio_ports[0])
#define pio1 (&pio_ports[1])
static struct {
    int dma[4]; uint32_t sm_mask[2], published;
    bool owned, live, worker_stop, worker_ready;
    worker_snapshot_t snapshot;
    iq4prod_progress_t result;
} state;
static const char *last_error;
static uint32_t ring[IQ4PROD_INPUT_WORDS], consumed[4], remaining[4];
static uint32_t ticks, total_words, limit_calls, calls, rearmed, disabled, overwritten;
static bool automatic_source, exhausted;
static void __dmb(void) { }
static uint32_t save_and_disable_interrupts(void) { return 17; }
static void restore_interrupts(uint32_t saved) { assert(saved==17); }
static void disable_producers(void) { disabled++; }
static void store_snapshot(const worker_snapshot_t *s) { state.snapshot=*s; }
static worker_snapshot_t load_snapshot(void) { return state.snapshot; }
static iq4prod_progress_t iq4prod_dma_progress(void);
static iq4prod_progress_t iq4prod_dma_publish(uint64_t published_words);
static uint32_t cycles(void) {
    ticks+=64; calls++;
    if (calls>limit_calls) { state.worker_stop=true; return ticks; }
    // Each trigger is finite and preserves its monotonically advancing source
    // address. There is no chain/reload when the count reaches zero.
    for(unsigned s=0;s<4;s++) {
        if(registers.ch[s].al1_transfer_count_trig) {
            assert(!remaining[s] && !(registers.ch[s].ctrl_trig&DMA_CH0_CTRL_TRIG_BUSY_BITS));
            assert(registers.ch[s].al1_transfer_count_trig==IQ4PROD_BATCH_WORDS);
            registers.ch[s].al1_transfer_count_trig=0;
            remaining[s]=IQ4PROD_BATCH_WORDS;
            registers.ch[s].ctrl_trig|=DMA_CH0_CTRL_TRIG_BUSY_BITS;
            rearmed++;
        }
        if(calls%(s+1)) continue;
        for(unsigned n=0;n<7 && remaining[s];n++) {
            assert(consumed[s]<state.published);
            assert(ring[consumed[s]%IQ4PROD_INPUT_WORDS]==consumed[s]);
            consumed[s]++; remaining[s]--;
            if(!remaining[s]) registers.ch[s].ctrl_trig&=~DMA_CH0_CTRL_TRIG_BUSY_BITS;
        }
    }
    if(automatic_source) {
        iq4prod_progress_t p=iq4prod_dma_progress();
        uint32_t low=consumed[0];
        for(unsigned s=1;s<4;s++) if(consumed[s]<low)low=consumed[s];
        assert(p.reusable_words<=low);
        uint32_t end=(uint32_t)p.reusable_words+IQ4PROD_INPUT_WORDS;
        if(end>total_words)end=total_words;
        if(end>state.published) {
            for(uint32_t n=state.published;n<end;n++) {
                // All four DMA reads of the previous physical slot retired.
                if(n>=IQ4PROD_INPUT_WORDS) {
                    for(unsigned s=0;s<4;s++)assert(consumed[s]>n-IQ4PROD_INPUT_WORDS);
                    overwritten++;
                }
                ring[n%IQ4PROD_INPUT_WORDS]=n;
            }
            p=iq4prod_dma_publish(end);
            assert(!p.errors && p.running && state.published==end);
        }
        if(low==total_words)state.worker_stop=true;
    } else {
        exhausted=true;
        for(unsigned s=0;s<4;s++) exhausted &= consumed[s]==total_words && !remaining[s];
        if(exhausted)pio0->fdebug|=1u<<PIO_FDEBUG_TXSTALL_LSB;
    }
    return ticks;
}
'''
    fixture += "\nstatic iq4prod_progress_t iq4prod_dma_progress(void) {\n" + progress + "\n}\n"
    fixture += "\nstatic iq4prod_progress_t iq4prod_dma_publish(uint64_t published_words) {\n"
    fixture += function_body(source, "iq4prod_dma_publish") + "\n}\n"
    fixture += "\nstatic void input_worker(uint32_t ignored) {\n"
    fixture += function_body(source, "input_worker") + "\n}\n"
    fixture += r'''
static void reset(void) {
    memset(&state,0,sizeof state);memset(&registers,0,sizeof registers);
    memset(pio_ports,0,sizeof pio_ports);memset(consumed,0,sizeof consumed);memset(remaining,0,sizeof remaining);
    ticks=calls=rearmed=disabled=overwritten=0;limit_calls=200000;exhausted=false;
    state.owned=state.live=state.result.running=true;state.sm_mask[0]=7;state.sm_mask[1]=1;
    for(unsigned s=0;s<4;s++)state.dma[s]=s;
}
static void worker_case(bool live_source) {
    reset();automatic_source=live_source;total_words=live_source?5*IQ4PROD_INPUT_WORDS:2*IQ4PROD_BATCH_WORDS;
    state.published=live_source?IQ4PROD_INPUT_WORDS:total_words;
    state.result.published_words=state.published;
    for(uint32_t n=0;n<state.published;n++)ring[n]=n;
    for(unsigned s=0;s<4;s++) {
        remaining[s]=IQ4PROD_BATCH_WORDS;registers.ch[s].ctrl_trig=DMA_CH0_CTRL_TRIG_BUSY_BITS;
    }
    input_worker(0);
    assert(calls<limit_calls && state.worker_ready);
    for(unsigned s=0;s<4;s++) {
        assert(consumed[s]==total_words && state.snapshot.completed[s]==total_words);
        assert(state.snapshot.granted[s]==total_words && !remaining[s]);
    }
    assert(rearmed==4*(total_words/IQ4PROD_BATCH_WORDS-1));
    if(live_source)assert(!state.snapshot.errors && !disabled && overwritten==4*IQ4PROD_INPUT_WORDS);
    else assert(exhausted && state.snapshot.errors==IQ4PROD_ERR_SOURCE_EMPTY && disabled==1 && !overwritten);
}
static void publication_cases(void) {
    for(unsigned slow=0;slow<4;slow++)
    for(unsigned delta=0;delta<=IQ4PROD_INPUT_WORDS+1;delta++) {
        reset();state.published=6000;state.result.published_words=6000;
        for(unsigned s=0;s<4;s++)state.snapshot.completed[s]=s==slow?2048:4096;
        uint64_t next=2048+delta;
        iq4prod_progress_t p=iq4prod_dma_publish(next);
        bool valid=next>=6000 && delta<=IQ4PROD_INPUT_WORDS;
        assert(p.reusable_words==2048);
        assert(p.errors==(valid?0:IQ4PROD_ERR_OWNERSHIP));
        assert(p.running==valid && state.worker_stop==!valid && disabled==(unsigned)!valid);
        assert(state.published==(valid?next:6000));
    }
    reset();state.result.published_words=UINT32_MAX;state.published=UINT32_MAX;
    for(unsigned s=0;s<4;s++)state.snapshot.completed[s]=UINT32_MAX-4095;
    assert(iq4prod_dma_publish(UINT32_MAX).running);
    assert(iq4prod_dma_publish((uint64_t)UINT32_MAX+1).errors==IQ4PROD_ERR_OWNERSHIP);
}
int main(void) {
    publication_cases();worker_case(true);worker_case(false);
    puts("finite input DMA: PASS (16394 publication cases, all-four retirement, five ring laps, finite512 grants, independent completions, terminal SOURCE_EMPTY/no replay)");
}
'''
    test, executable = directory / "finite.c", directory / "finite"
    test.write_text(fixture)
    subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    str(test), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
    return dict(publication_cases=16394, ring_laps=5, batch_words=512,
                terminal_source_empty=True, actual_worker_body=True)


def main():
    with tempfile.TemporaryDirectory(prefix="iq4prod-native-") as temporary:
        executable = Path(temporary) / "iq4prod_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{SRC}",
                        str(SRC / "iq4prod.c"), str(ROOT / "host/native/iq4prod_test.c"),
                        "-o", str(executable)], check=True)
        native = json.loads(subprocess.check_output([str(executable), "selftest"]))
        program = np.frombuffer(subprocess.check_output([str(executable), "program"]), dtype="<u2")
        assert len(program) == 11 and program[7:11].tolist() == program[:4].tolist()
        # SMs within a block reuse one11-word copy; programs for other blocks
        # have independent instruction memory budgets.
        assert len(program) <= 32
        cases = 0
        rng = random.Random(0xb6_2350)
        for length in (1, 2, 7, 31, 257):
            words = [rng.getrandbits(32) for _ in range(length)]
            for previous in (0, 0xffffffff, 0xa754319c):
                for base in (0x20000000, 0x20070000):
                    expected = reference(words, previous, base)
                    for parity in (0, 1):
                        for delays in range(4):
                            actual = stream(executable, words, previous, base, parity, delays)
                            np.testing.assert_array_equal(actual, expected[parity::2])
                            cases += 1
        words = [rng.getrandbits(32) for _ in range(17)]
        for parity in (0, 1):
            expected = reference(words, words[-1], 0x20030000)[parity::2]
            got = stream(executable, words * 5, words[-1], 0x20030000, parity, 3)
            np.testing.assert_array_equal(got, np.tile(expected, 5))
            long_words = [rng.getrandbits(32) for _ in range(1025)]
            got = stream(executable, long_words, 0xcad7e531, 0x20030000, parity, 1)
            np.testing.assert_array_equal(got, reference(long_words, 0xcad7e531, 0x20030000)[parity::2])
        integrated = writers(executable)
        credits = finite_dma_policy(Path(temporary))
    print(json.dumps(dict(native=native, stream_cases=cases, finite_empty_source_parities=2,
                          source_and_rx_delay_modes=4, repeated_source_laps=5,
                          integrated=integrated, finite_dma=credits, device_rate_measured=False)))


if __name__ == "__main__":
    main()
