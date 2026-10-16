"""Compile the B4 axis-start and DMA leaves against native register shims.

Checks exact one-word transfer counts and claimed RX/DREQ validation. It does
not model DMA handoff latency or claim device throughput.
"""
from pathlib import Path
import subprocess
import tempfile

from test_dual_faults import function_body
from test_dual_fifo import FIXTURE

ROOT = Path(__file__).resolve().parents[1]

CHECKS = r'''
static void reset(void) {
    memset(&state, 0, sizeof state); memset(&dma, 0, sizeof dma);
    memset(channels, 0, sizeof channels); memset(ports, 0, sizeof ports);
    for (unsigned p=0;p<3;p++) ports[p].index=p;
    pio1->claimed[0]=pio1->claimed[1]=true;
    selected_sample_bits=14;selected_row_words=2;selected_writers=2;
    selected_schedule=0;selected_dac_clock=1;starts=stopped=0;
}
int main(void) {
    for (unsigned bits=8;bits<=14;bits+=6)
    for (unsigned words=1;words<=8;words*=2)
    for (unsigned cpw=0;cpw<=5;cpw++)
    for (unsigned writers=2;writers<=4;writers+=2) {
        reset();assert(dual_dma_set_sample_bits(bits));
        assert(dual_dma_get_sample_bits()==bits && dual_dma_set_row_words(words));
        selected_writers=writers;
        bool want=writers==2 && words==(bits==8?1u:2u) &&
            (cpw==2 || cpw==4 || (bits==8 && cpw==1));
        assert(dual_dma_start_axis_fifo(&pio1->rxf[0],&pio1->rxf[1],12,13,cpw)==want);
        assert(starts==(unsigned)want && stopped==(unsigned)!want);
        if (want) {
            assert(state.owned && state.fifo_source && state.shared_axis_row);
            assert(state.writers==2 && state.row_words==words && state.sample_bits==bits);
            assert(state.output_pio==pio0 && !state.control_bswap);
            assert(divider_arg==cpw*256 && !log_arg && state.schedule==0);
            axis_fifo_dma(1,8,3,&pio1->rxf[0],12);
            assert(channels[1].count==1 && channels[8].count==words);
            assert(channels[1].read==&pio1->rxf[0]);
            assert(channels[1].write==&dma.ch[8].al3_read_addr_trig);
            assert(channels[1].config.dreq==12 && channels[1].config.chain==1);
            assert(!channels[1].config.read_inc && !channels[1].config.write_inc);
            assert(channels[8].write==&pio0->txf[3] && channels[8].config.dreq==3);
            assert(channels[8].config.chain==1 && channels[8].config.read_inc);
            assert(!channels[8].config.write_inc && !channels[8].config.bswap);
        }
    }
    for (unsigned bad=0;bad<6;bad++) {
        reset();selected_sample_bits=8;selected_row_words=1;
        if (bad==0) pio1->claimed[1]=false;
        if (bad==1) state.owned=true;
        assert(!dual_dma_start_axis_fifo(bad==2?NULL:&pio1->rxf[0],
            bad==3?&pio1->rxf[0]:&pio1->rxf[1],bad==4?11:12,bad==5?12:13,1));
        assert(!starts);
    }
    reset();assert(!dual_dma_set_sample_bits(0) && !dual_dma_set_sample_bits(16));
    assert(dual_dma_get_sample_bits()==14);
    assert(dual_dma_set_sample_bits(8));state.owned=true;
    assert(!dual_dma_set_sample_bits(14) && dual_dma_get_sample_bits()==8);
    assert(!dual_dma_set_row_words(2));
    reset();selected_row_words=1;
    assert(!dual_dma_start_div256((const uint32_t *)(uintptr_t)0x20010000,
        (const uint32_t *)(uintptr_t)0x20020000,8,256));
    assert(!starts && stopped==1);
    puts("B4 native DMA setup: PASS (96 format/rate/writer cases, one-word row count, RX/DREQ ownership, idle selectors, descriptor rejection)");
}
'''


def main():
    source = (ROOT / 'firmware/src/dual_dma.c').read_text()
    fixture = '#define IQ4HIST_EXPERIMENT 1\n' + FIXTURE
    signatures = [
        ('bool', 'dual_dma_set_row_words', 'unsigned row_words'),
        ('bool', 'dual_dma_set_sample_bits', 'unsigned bits'),
        ('unsigned', 'dual_dma_get_sample_bits', 'void'),
        ('static void', 'claim_init', 'void'),
        ('static bool', 'valid_rx_source', 'const volatile uint32_t *rx, unsigned dreq'),
        ('static void', 'axis_data_dma', 'int control, int data, uint sm'),
        ('static void', 'axis_fifo_dma',
         'int control, int data, uint sm, const volatile uint32_t *rx, unsigned dreq'),
        ('bool', 'dual_dma_start_axis_fifo',
         'const volatile uint32_t *rx_i, const volatile uint32_t *rx_q, unsigned dreq_i, unsigned dreq_q, unsigned cpw'),
        ('bool', 'dual_dma_start_div256',
         'const uint32_t *desc_i, const uint32_t *desc_q, unsigned count_log2, unsigned divider256'),
    ]
    functions = [f'{result} {name}({args}) {{\n{function_body(source, name)}\n}}'
                 for result, name, args in signatures]
    with tempfile.TemporaryDirectory(prefix='iq4writer8-') as name:
        path = Path(name)
        (path / 'test.c').write_text(fixture + '\n'.join(functions) + CHECKS)
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-Wno-pointer-to-int-cast', '-Wno-unused-function',
                        f'-I{ROOT / "firmware/src"}', str(path / 'test.c'),
                        '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], check=True)


if __name__ == '__main__':
    main()
