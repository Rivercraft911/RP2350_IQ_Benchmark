"""Run actual DMA poll functions against native register snapshots; no device access.

This checks fault classification, completed/partial capture counts, and queries after
resource release. It does not model DMA timing, interrupt assertion latency, or register
W1C side effects. IRQ_QUIET/raw-IRQ semantics come from RP2350 datasheet section12.6.5.
The extracted production functions are compiled, rather than reimplemented in Python.
"""
from pathlib import Path
import re
import subprocess
import tempfile
import fwsrc

ROOT = Path(__file__).resolve().parents[1]


def function_body(source: str, name: str) -> str:
    # Balanced tokens tolerate comments, strings, whitespace and the SRAM-name macro.
    tokens = [m for m in re.finditer(
        r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
        r'[A-Za-z_]\w*|[{}();]', source)
        if not m[0].startswith(('//', '/*', '"', "'"))]
    for index, token in enumerate(tokens):
        if token[0] != name:
            continue
        begin = index + (2 if tokens[index + 1][0] == ')' else 1)
        if tokens[begin][0] != '(':
            continue
        depth = 0
        for end in range(begin, len(tokens)):
            depth += (tokens[end][0] == '(') - (tokens[end][0] == ')')
            if depth == 0:
                break
        begin = end + 1
        if tokens[begin][0] != '{':
            continue
        depth = 0
        for end in range(begin, len(tokens)):
            depth += (tokens[end][0] == '{') - (tokens[end][0] == '}')
            if depth == 0:
                return source[tokens[begin].end():tokens[end].start()]
    raise AssertionError(f"missing production function: {name}")


FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "dual_dma.h"
#define PIO_FDEBUG_TXOVER_LSB 16
#define PIO_FDEBUG_TXSTALL_LSB 24
typedef struct { uint32_t fdebug; } pio_t;
static pio_t output, capture;
static pio_t *pio1 = &capture;
static struct dma_t { uint32_t intr; struct { uint32_t transfer_count; } ch[16]; } dma;
static struct dma_t *dma_hw = &dma;
static struct {
    bool owned, running, capture_started, capture_fifo_only, capture_complete, capture_frozen, fifo_source;
    int cap_dma, sm_cap;
    uint32_t capture_requested, captured;
    uint32_t output_sm_mask, txstall_i_mask, txstall_q_mask, output_debug_mask;
    uint32_t output_dma_mask, capture_dma_mask, capture_debug_mask;
    pio_t *output_pio;
    dual_dma_progress_t result;
} state;
static unsigned fifo_level, disables, cursor_reads;
static const char *last_error;
static unsigned pio_sm_get_rx_fifo_level(pio_t *p, unsigned sm) {
    (void)p; (void)sm; return fifo_level;
}
static void pio_sm_set_enabled(pio_t *p, unsigned sm, bool enabled) {
    (void)p; (void)sm; assert(!enabled); disables++;
}
static void halt_outputs(void) { state.running = state.result.running = false; }
static void sample_reads(bool check_time) { (void)check_time; cursor_reads++; }
'''

CHECKS = r'''
static void init(bool cap) {
    memset(&state, 0, sizeof state); memset(&dma, 0, sizeof dma);
    output.fdebug = capture.fdebug = 0; dma_hw = &dma;
    fifo_level = disables = cursor_reads = 0; last_error = "";
    state.owned = state.running = state.result.running = true;
    state.output_pio = &output; state.output_sm_mask = 15;
    state.txstall_i_mask = 5u << 24; state.txstall_q_mask = 10u << 24;
    state.output_debug_mask = (15u << 24) | (15u << 16);
    state.output_dma_mask = 0x5555; state.capture_dma_mask = 1u << 15;
    state.capture_debug_mask = 1u | (1u << 8); state.cap_dma = 15;
    state.capture_started = cap; state.capture_requested = 16; dma.ch[15].transfer_count = 16;
}
int main(void) {
    // Owned IRQ_QUIET channels can raise raw IRQ only for errors or null triggers;
    // unowned channels' ordinary completions must not contaminate our result.
    for (uint32_t irq = 0; irq < 65536; irq++) {
        init(true); dma.intr = irq;
        dual_dma_progress_t p = dual_dma_progress();
        uint32_t want = (irq & 0x5555 ? DUAL_DMA_ERR_OUTPUT_DMA : 0) |
            (irq & 0x8000 ? DUAL_DMA_ERR_CAPTURE_DMA : 0);
        assert(p.errors == want); assert(p.running == !want);
    }
    for (uint32_t flags = 0; flags < 256; flags++) {
        init(false); output.fdebug = ((flags & 15) << 24) | ((flags >> 4) << 16);
        dual_dma_progress_t p = dual_dma_progress();
        uint32_t want = (flags & 5 ? DUAL_DMA_ERR_TXSTALL_I : 0) |
            (flags & 10 ? DUAL_DMA_ERR_TXSTALL_Q : 0) |
            (flags & 240 ? DUAL_DMA_ERR_OUTPUT_DMA : 0);
        assert(p.errors == want); assert(p.running == !want);
        assert(p.txstalls_i == (unsigned)__builtin_popcount(flags & 5));
        assert(p.txstalls_q == (unsigned)__builtin_popcount(flags & 10));
    }
    init(true); dma.ch[15].transfer_count = 5; dual_dma_progress();
    assert(state.captured == 11 && !disables && !dual_dma_capture_done());
    dma.ch[15].transfer_count = 0; dual_dma_progress();
    assert(state.captured == 16 && disables == 1 && dual_dma_capture_done());
    dma.ch[15].transfer_count = 99; dual_dma_progress();
    assert(state.captured == 16 && disables == 1);
    capture.fdebug = 1;
    assert(dual_dma_progress().errors == DUAL_DMA_ERR_CAPTURE_STALL);
    init(true); dma.ch[15].transfer_count = 0; dual_dma_progress();
    dma.intr = state.capture_dma_mask;
    assert(dual_dma_progress().errors == DUAL_DMA_ERR_CAPTURE_DMA);
    init(true); dma.ch[15].transfer_count = 5; dual_dma_progress();
    state.capture_frozen = true; dma.ch[15].transfer_count = 0; // ABORT clears count
    dual_dma_progress(); assert(state.captured == 11 && !dual_dma_capture_done());
    init(true); state.capture_fifo_only = true; state.capture_requested = fifo_level = 8;
    dual_dma_progress(); assert(state.captured == 8 && disables == 1);
    fifo_level = 0; dual_dma_progress(); assert(state.captured == 8 && disables == 1);
    capture.fdebug = 1u << 8;
    assert(dual_dma_progress().errors == DUAL_DMA_ERR_CAPTURE_STALL);
    state.owned = false; dma_hw = NULL; // released channels must never be read
    assert(dual_dma_capture_count() == 8 && dual_dma_capture_done());
    assert(dual_dma_capture_errors() == DUAL_DMA_ERR_CAPTURE_STALL);
    for (unsigned fifo = 0; fifo < 2; fifo++) {
        for (uint32_t irq = 0; irq < 65536; irq++) {
            init(true); state.fifo_source = fifo; dma.intr = irq;
            dual_dma_faults_t f = dual_dma_faults();
            uint32_t want = (irq & 0x5555 ? DUAL_DMA_ERR_OUTPUT_DMA : 0) |
                (irq & 0x8000 ? DUAL_DMA_ERR_CAPTURE_DMA : 0);
            assert(f.errors == want && f.running == !want && !cursor_reads);
        }
        init(true); state.fifo_source = fifo;
        state.capture_fifo_only = true; state.capture_requested = fifo_level = 8;
        assert(dual_dma_faults().running && state.captured == 8 && !cursor_reads);
        output.fdebug = (5u << 24) | (10u << 24);
        dual_dma_faults_t f = dual_dma_faults();
        assert(f.errors == (DUAL_DMA_ERR_TXSTALL_I | DUAL_DMA_ERR_TXSTALL_Q));
        assert(f.txstalls_i == 2 && f.txstalls_q == 2 && !f.running && !cursor_reads);
        state.owned = false; dma_hw = NULL;
        dual_dma_faults_t after = dual_dma_faults();
        assert(after.errors == f.errors && !after.running && !cursor_reads);
    }
    init(false); state.fifo_source = true;
    dual_dma_progress_t rejected = dual_dma_progress();
    assert(rejected.errors == DUAL_DMA_ERR_CONFIG && !rejected.running && !cursor_reads);
    assert(strstr(last_error, "unavailable"));
    state.owned = false; dma_hw = NULL;
    assert(dual_dma_progress().errors == DUAL_DMA_ERR_CONFIG && !cursor_reads);
    puts("dual fault snapshots: PASS (old descriptor faults/captures; both-source fault-only polling; FIFO progress rejection; released queries)");
}
'''


def main():
    source = (ROOT / "firmware/src/output/dual_dma.c").read_text()
    functions = [("static void", "sample_capture", "uint32_t dma_interrupts"),
                 ("static void", "sample_faults", "void"),
                 ("dual_dma_progress_t", "dual_dma_progress", "void"),
                 ("dual_dma_faults_t", "dual_dma_faults", "void"),
                 ("bool", "dual_dma_capture_done", "void"),
                 ("uint32_t", "dual_dma_capture_count", "void"),
                 ("uint32_t", "dual_dma_capture_errors", "void")]
    code = FIXTURE + "\n".join(f"{result} {name}({args}) {{\n{function_body(source, name)}\n}}"
                                for result, name, args in functions) + CHECKS
    with tempfile.TemporaryDirectory(prefix="dual-faults-") as directory:
        test = Path(directory) / "faults.c"
        executable = Path(directory) / "faults"
        test.write_text(code)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        *fwsrc.INCLUDES, str(test), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
