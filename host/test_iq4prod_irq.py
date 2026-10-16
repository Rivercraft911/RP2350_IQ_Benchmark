"""Execute B9's actual finite-input IRQ/publication leaves under DMA shims.

This proves ownership and bounded grant policy, not RP2350 IRQ timing or bus
arbitration. Each physical source slot carries its absolute publication epoch.
"""
from pathlib import Path
import subprocess
import tempfile

from test_dual_faults import function_body

ROOT = Path(__file__).resolve().parents[1]


def stop_lifecycle(directory, source, enums, progress_type):
    """Stop must preserve pre-abort retirement and catch a late AHB fault."""
    fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
''' + enums + progress_type + r'''
enum { DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS=1u<<31, DMA_CH0_CTRL_TRIG_EN_BITS=1,
       DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB=11, DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS=15u<<11 };
#define DMA_IRQ_NUM(n) (10+(n))
static struct { struct { uint32_t ctrl_trig, al1_ctrl; } ch[4]; uint32_t intr, abort; } registers;
#define dma_hw (&registers)
static int ports[2], program;
#define pio0 (&ports[0])
#define pio1 (&ports[1])
static struct {
    bool owned, irq_installed, worker_started, worker_stop, added[2];
    unsigned irq_index, irq_priority, dma_mask, offset[2];
    int dma[4], sm[4], *pio[4];
    iq4prod_progress_t result;
} state;
static const char *last_error;
static unsigned ticks, cleanups, unclaims, sm_unclaims, removes, disabled, irq_removed;
static bool late_error, timeout_abort;
static void input_irq(void) { assert(0); }
static void irq_set_enabled(unsigned n, bool enabled) { assert(n==13 && !enabled); }
static void dma_irqn_set_channel_mask_enabled(unsigned n, uint32_t mask, bool enabled) {
    assert(n==3 && mask==15 && !enabled);
}
static void irq_clear(unsigned n) { assert(n==13); }
static void irq_remove_handler(unsigned n, void (*handler)(void)) {
    assert(n==13 && handler==input_irq);irq_removed++;
}
static void irq_set_priority(unsigned n, unsigned priority) { assert(n==13 && priority==128); }
static bool multicore_fifo_rvalid(void) { return true; }
static void tx_worker_wait(void) { assert(0); }
static void __dmb(void) { }
static uint32_t time_us_32(void) {
    ticks++;
    if(registers.abort && !timeout_abort) {
        assert(disabled);
        if(late_error)registers.ch[2].ctrl_trig|=DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS;
        registers.abort=0;
    }
    return ticks;
}
static void disable_producers(void) { disabled++; }
static iq4prod_progress_t iq4prod_dma_progress(void) {
    assert(!state.irq_installed);return state.result;
}
static void hw_write_masked(uint32_t *reg, uint32_t value, uint32_t mask) {
    *reg=(*reg&~mask)|(value&mask);
}
static void dma_channel_cleanup(unsigned ch) {
    assert(disabled && !registers.abort);
    if(late_error)assert(state.result.errors&IQ4PROD_ERR_DMA);
    registers.ch[ch].ctrl_trig=0;cleanups++;
}
static void dma_channel_unclaim(unsigned ch) { assert(ch<4);unclaims++; }
static void pio_sm_clear_fifos(int *pio, int sm) { assert(pio && sm>=0); }
static void pio_sm_unclaim(int *pio, int sm) { assert(pio && sm>=0);sm_unclaims++; }
static void pio_remove_program(int *pio, const int *p, unsigned offset) {
    assert(pio && p==&program && offset<32);removes++;
}
'''
    fixture += "static void iq4prod_dma_stop(void) {\n" + function_body(source, "iq4prod_dma_stop") + "\n}\n"
    fixture += r'''
static void reset(void) {
    memset(&state,0,sizeof state);memset(&registers,0,sizeof registers);
    ticks=cleanups=unclaims=sm_unclaims=removes=disabled=irq_removed=0;
    late_error=timeout_abort=false;
    state.owned=state.irq_installed=state.result.running=true;
    state.irq_index=3;state.irq_priority=128;state.dma_mask=15;
    state.added[0]=state.added[1]=true;
    for(unsigned s=0;s<4;s++) {
        state.dma[s]=state.sm[s]=s;state.pio[s]=pio0;
        state.result.input_words[s]=512;state.result.granted_input_words[s]=1024;
        registers.ch[s].al1_ctrl=DMA_CH0_CTRL_TRIG_EN_BITS;
    }
}
int main(void) {
    for(unsigned fault=0;fault<2;fault++) {
        reset();late_error=fault;iq4prod_dma_stop();
        assert(!state.owned && !state.result.running && !state.irq_installed);
        assert(cleanups==4 && unclaims==4 && sm_unclaims==4 && removes==2 && irq_removed==1);
        assert(state.result.errors==(fault?IQ4PROD_ERR_DMA:0));
        for(unsigned s=0;s<4;s++) {
            // Aborted BUSY/count can never make a partial grant look retired.
            assert(state.result.input_words[s]==512 && state.result.granted_input_words[s]==1024);
            assert(!(registers.ch[s].al1_ctrl&DMA_CH0_CTRL_TRIG_EN_BITS));
        }
    }
    reset();timeout_abort=true;iq4prod_dma_stop();
    assert(state.owned && !state.result.running && state.result.errors==IQ4PROD_ERR_STOP);
    assert(!cleanups && !unclaims && !sm_unclaims && !removes && irq_removed==1);
    timeout_abort=false;iq4prod_dma_stop();
    assert(!state.owned && cleanups==4 && unclaims==4 && irq_removed==1);
    puts("B9 IRQ stop: PASS (IRQ release, conservative partial-grant retirement, late AHB fault, abort timeout retains claims and supports retry)");
}
'''
    test, executable = directory / "stop.c", directory / "stop"
    test.write_text(fixture)
    subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    str(test), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)


def main():
    source = (ROOT / "firmware/src/output/iq4prod_dma.c").read_text()
    header = (ROOT / "firmware/src/output/iq4prod_dma.h").read_text()
    enums = header[header.index("enum {"):header.index("};") + 2]
    progress_type = header[header.index("typedef struct {"):header.index("} iq4prod_progress_t;") +
                           len("} iq4prod_progress_t;")]
    poll_start = header.index("typedef struct {", header.index("} iq4prod_progress_t;"))
    poll_type = header[poll_start:header.index("} iq4prod_irq_poll_t;") + len("} iq4prod_irq_poll_t;")]
    snapshot_type = source[source.index("typedef struct {"):source.index("} worker_snapshot_t;") +
                           len("} worker_snapshot_t;")]
    progress = function_body(source, "iq4prod_dma_progress")
    start = progress.index("if (state.live) {")
    stop, depth = progress.index("{", start) + 1, 1
    while depth:
        depth += (progress[stop] == "{") - (progress[stop] == "}")
        stop += 1
    progress = progress[:stop] + "\n" + progress[progress.rindex("if (state.result.errors)"):]
    fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
''' + enums + progress_type + poll_type + snapshot_type + r'''
enum { DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS=1u<<31, DMA_CH0_CTRL_TRIG_BUSY_BITS=1u<<24,
       PIO_FDEBUG_TXSTALL_LSB=24, PIO_FDEBUG_RXSTALL_LSB=16,
       PIO_FDEBUG_TXOVER_LSB=8, PIO_FDEBUG_RXUNDER_LSB=0 };
static struct { struct { uint32_t ctrl_trig, al1_transfer_count_trig; } ch[4]; uint32_t intr; } registers;
#define dma_hw (&registers)
static struct { uint32_t fdebug; } pio_ports[2];
#define pio0 (&pio_ports[0])
#define pio1 (&pio_ports[1])
static struct {
    int dma[4]; uint32_t sm_mask[2], published, dma_mask, snapshot_seq;
    bool owned, live, worker_stop, irq_mode;
    uint32_t irq_calls, irq_max_service_cycles;
    uint64_t irq_service_cycles;
    worker_snapshot_t snapshot;
    iq4prod_progress_t result;
} state;
static const char *last_error;
static uint32_t source_ring[IQ4PROD_INPUT_WORDS], consumed[4], remaining[4], disabled, ticks, irq_excluded;
static uint32_t triggers, partials, overwritten;
static void __dmb(void) { }
static uint32_t cycles(void) { return ++ticks; }
static uint32_t save_and_disable_interrupts(void) { assert(!irq_excluded); irq_excluded=1; return 17; }
static void restore_interrupts(uint32_t saved) { assert(saved==17 && irq_excluded); irq_excluded=0; }
static void disable_producers(void) { disabled++; }
static worker_snapshot_t load_snapshot(void) { assert(!(state.snapshot_seq&1)); return state.snapshot; }
static iq4prod_progress_t iq4prod_dma_progress(void);
static iq4prod_progress_t iq4prod_dma_publish(uint64_t published_words);
static void input_irq(void);
'''
    fixture += "\nstatic void input_irq(void) {\n" + function_body(source, "input_irq") + "\n}\n"
    fixture += "\nstatic iq4prod_progress_t iq4prod_dma_progress(void) {\n" + progress + "\n}\n"
    fixture += "\nstatic iq4prod_progress_t iq4prod_dma_publish(uint64_t published_words) {\n"
    fixture += function_body(source, "iq4prod_dma_publish") + "\n}\n"
    fixture += "\nstatic iq4prod_irq_poll_t iq4prod_dma_poll_irq(void) {\n"
    fixture += function_body(source, "iq4prod_dma_poll_irq") + "\n}\n"
    fixture += r'''
static void reset(uint32_t initial) {
    memset(&state,0,sizeof state);memset(&registers,0,sizeof registers);memset(pio_ports,0,sizeof pio_ports);
    memset(consumed,0,sizeof consumed);memset(remaining,0,sizeof remaining);
    ticks=disabled=irq_excluded=triggers=partials=overwritten=0;
    state.owned=state.live=state.irq_mode=state.result.running=true;
    state.sm_mask[0]=7;state.sm_mask[1]=1;state.dma_mask=15;
    state.published=initial;state.result.published_words=initial;
    for(uint32_t n=0;n<initial;n++)source_ring[n]=n;
    for(unsigned s=0;s<4;s++) {
        state.dma[s]=s;state.snapshot.granted[s]=IQ4PROD_BATCH_WORDS;
        remaining[s]=IQ4PROD_BATCH_WORDS;registers.ch[s].ctrl_trig=DMA_CH0_CTRL_TRIG_BUSY_BITS;
    }
}
static void apply_triggers(void) {
    for(unsigned s=0;s<4;s++) if(registers.ch[s].al1_transfer_count_trig) {
        uint32_t count=registers.ch[s].al1_transfer_count_trig;
        assert(!remaining[s] && !(registers.ch[s].ctrl_trig&DMA_CH0_CTRL_TRIG_BUSY_BITS));
        assert(count<=IQ4PROD_BATCH_WORDS && consumed[s]+count<=state.published);
        assert(state.snapshot.granted[s]==consumed[s]+count);
        remaining[s]=count;registers.ch[s].ctrl_trig|=DMA_CH0_CTRL_TRIG_BUSY_BITS;
        registers.ch[s].al1_transfer_count_trig=0;triggers++;partials+=count<IQ4PROD_BATCH_WORDS;
    }
}
static void consume(unsigned s, unsigned count) {
    while(count-- && remaining[s]) {
        assert(consumed[s]<state.published);
        assert(source_ring[consumed[s]%IQ4PROD_INPUT_WORDS]==consumed[s]);
        consumed[s]++;remaining[s]--;
        if(!remaining[s])registers.ch[s].ctrl_trig&=~DMA_CH0_CTRL_TRIG_BUSY_BITS;
    }
}
static void irq_event(void) { input_irq();apply_triggers();assert(!(state.snapshot_seq&1)); }
static void rolling(void) {
    const uint32_t total=20*IQ4PROD_INPUT_WORDS+19;
    reset(1025);
    for(uint32_t round=0;round<1000000;round++) {
        for(unsigned s=0;s<4;s++) if(round%(s+1)==0)consume(s,1+(round+s)%17);
        // Delays are independent of source production and intentionally much
        // longer than a FIFO runway: finite DMA still never reads stale words.
        if(round%13==0)irq_event();
        iq4prod_progress_t p=iq4prod_dma_progress();
        assert(!p.errors && p.running);
        iq4prod_irq_poll_t hot=iq4prod_dma_poll_irq();
        assert(hot.reusable_words==p.reusable_words && hot.published_words==state.published && !hot.errors);
        uint32_t low=consumed[0];
        for(unsigned s=1;s<4;s++)if(consumed[s]<low)low=consumed[s];
        assert(p.reusable_words<=low);
        uint32_t next=state.published+(round%71)+1;
        if(next>p.reusable_words+IQ4PROD_INPUT_WORDS)next=p.reusable_words+IQ4PROD_INPUT_WORDS;
        if(next>total)next=total;
        if(next>state.published) {
            for(uint32_t n=state.published;n<next;n++) {
                if(n>=IQ4PROD_INPUT_WORDS) {
                    for(unsigned s=0;s<4;s++)assert(consumed[s]>n-IQ4PROD_INPUT_WORDS);
                    overwritten++;
                }
                source_ring[n%IQ4PROD_INPUT_WORDS]=n;
            }
            p=iq4prod_dma_publish(next);apply_triggers();assert(!p.errors && p.running);
        }
        if(low==total)break;
        assert(round<999999);
    }
    irq_event();
    for(unsigned s=0;s<4;s++) {
        assert(consumed[s]==total && !remaining[s]);
        assert(state.snapshot.completed[s]==total && state.snapshot.granted[s]==total);
    }
    assert(partials>10 && overwritten==total-IQ4PROD_INPUT_WORDS && !disabled);
    // Idle channels at the finite frontier never replay on repeated IRQs.
    for(unsigned repeat=0;repeat<100;repeat++)irq_event();
    for(unsigned s=0;s<4;s++)assert(consumed[s]==total && !remaining[s]);
    uint32_t before=state.snapshot.polls;
    pio0->fdebug=1u<<PIO_FDEBUG_TXSTALL_LSB;
    assert(iq4prod_dma_poll_irq().errors==IQ4PROD_ERR_SOURCE_EMPTY && disabled);
    assert(state.snapshot.polls==before); // terminal drain has no later IRQ
    assert(iq4prod_dma_progress().errors==IQ4PROD_ERR_SOURCE_EMPTY);
}
static void prefix_and_faults(void) {
    // Exhaust a512-word grant with exactly17 more words published. All four
    // independently idle channels receive17, never a speculative full chunk.
    reset(529);
    for(unsigned s=0;s<4;s++)consume(s,512);
    irq_event();
    for(unsigned s=0;s<4;s++) { assert(remaining[s]==17);consume(s,17); }
    irq_event();assert(partials==4);
    uint32_t before=state.snapshot.polls;
    for(unsigned s=0;s<4;s++)assert(!remaining[s] && state.snapshot.granted[s]==529);
    // Publication after the last completion explicitly kicks idle readers.
    for(uint32_t n=529;n<600;n++)source_ring[n]=n;
    assert(iq4prod_dma_publish(600).running);apply_triggers();
    assert(state.snapshot.polls==before+1);
    for(unsigned s=0;s<4;s++)assert(remaining[s]==71 && state.snapshot.granted[s]==600);
    reset(1024);registers.ch[2].ctrl_trig|=DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS;irq_event();
    assert(iq4prod_dma_progress().errors==IQ4PROD_ERR_DMA && disabled);
    reset(1024);pio1->fdebug=1u<<PIO_FDEBUG_TXOVER_LSB;irq_event();
    assert(iq4prod_dma_progress().errors==IQ4PROD_ERR_PIO && disabled);
    reset(1024);for(unsigned s=0;s<4;s++)consume(s,512);
    pio0->fdebug=1u<<PIO_FDEBUG_TXSTALL_LSB;irq_event();
    assert(!triggers && state.snapshot.errors==IQ4PROD_ERR_SOURCE_EMPTY);
    reset(1024);state.snapshot.granted[3]=1025;registers.ch[3].ctrl_trig=0;
    irq_event();assert(iq4prod_dma_progress().errors==IQ4PROD_ERR_OWNERSHIP);
    reset(1024);state.owned=false;
    assert(iq4prod_dma_poll_irq().errors==IQ4PROD_ERR_CONFIG);
    reset(1024);state.irq_mode=false;
    assert(iq4prod_dma_poll_irq().errors==IQ4PROD_ERR_CONFIG);
    reset(1024);state.snapshot.completed[0]=UINT32_MAX-5;
    state.snapshot.completed[1]=UINT32_MAX-2;state.snapshot.completed[2]=UINT32_MAX;
    state.snapshot.completed[3]=UINT32_MAX-1;
    assert(iq4prod_dma_poll_irq().reusable_words==UINT32_MAX-5);
    state.snapshot.errors=IQ4PROD_ERR_DMA;
    assert(iq4prod_dma_poll_irq().errors==IQ4PROD_ERR_DMA && !state.result.running);
    reset(1024);pio1->fdebug=1u<<PIO_FDEBUG_TXOVER_LSB;
    assert(iq4prod_dma_poll_irq().errors==IQ4PROD_ERR_PIO && !state.result.running);
}
int main(void) {
    rolling();prefix_and_faults();
    puts("B9 IRQ finite grants: PASS (20 ring laps, four delayed readers, partial prefixes, publication kicks, stable terminal stop, underrun/DMA/PIO/ownership controls; no hardware timing model)");
}
'''
    with tempfile.TemporaryDirectory(prefix="b9-irq-") as temporary:
        test, executable = Path(temporary) / "irq.c", Path(temporary) / "irq"
        test.write_text(fixture)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        str(test), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)
        stop_lifecycle(Path(temporary), source, enums, progress_type)


if __name__ == "__main__":
    main()
