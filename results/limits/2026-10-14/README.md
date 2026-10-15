# RP2350 limits experiments, 2026-10-14

These experiments extend the existing optimized transmitter rather than replace its LUT,
360-bit LDPC layout or DMA/PIO output. They were run on the USB-powered
`pimoroni_pico_plus2_rp2350`, with all external GPIO connections removed, stock voltage,
and 128 or 150 MHz system clocks. No AFE7071, PA, antenna or RF receiver was connected.

The main findings are a measured BCH improvement, complete encoding plus shaping on one CPU,
a 64-KiB 8PSK shaper, and a faster output-interface component test. The second CPU
remains idle in the one-core transmitter mode; DMA, PIO, interrupts and the SPI emulator
still operate. These are digital benchmark results, not RF, environmental or flight validation.

## Code exercised

- `bchkernel 0|1|2`: retain the baseline slicing-by-four BCH encoder, select a five-word
  degree-160 C specialization, or select its streamed Thumb-2 implementation. Other BCH
  degrees fall back to the existing supported implementation; selection is reported in logs.
- Optional `single_core` argument on `txs2`/`pvtx`: core 0 refills the shaper ring between BB
  construction, BB scrambling/BCH, LDPC and PL mapping stages. It publishes complete encoded
  frames and preserves the partial symbol word across frame boundaries. The original two-core
  mode remains the default.
- `bench8psk`: isolated L12, four-SPS pulse shaping at angles pi/8 + k*pi/4, using two signed
  binary amplitude planes and 64 KiB of tables. It borrows and restores the existing QPSK
  tables/input buffer. It does not implement DVB-S2 8PSK framing or continuous transmission.
- `ddrtest`: PIO emits a stored cyclic pattern at one 16-bit bus word per system clock;
  HSTX independently generates a candidate DDR clock. This tests an output component,
  separately from the production SDR-clocked shaper/encoder path.
- `host/limits.py`: immutable per-run JSON logs, local source/build manifests, positive and
  negative probes, and restoration of 128 MHz plus baseline BCH selection on exit. It does
  not build or flash firmware.

Native validation passed: all 42 code/pilot combinations against the independent Python
PLFRAME reference; 756 BCH pattern/kernel comparisons against bit-serial division; and
three consecutive different frames in each of 126 code/pilot/kernel combinations, checking
staged versus full encoding including unflushed symbol state. The 8PSK test passed 12
seed/block-history cases against an independent quantized convolution. PV-SPI native
protocol checks, four startup tests and two USB flight-service tests also passed. Both
`iqbench` and `pvflight` compile; only `iqbench` was flashed for this campaign.

## BCH and frame encoding

Isolated device timing from [quick-2](quick-2/runs.jsonl), 128 MHz, normal frames, pilots enabled,
eight timing repetitions. All reported PLFRAME CRCs match the independent reference.

| Kernel | 2/3 BCH cycles | 2/3 complete-frame cycles | 5/6 BCH cycles | 5/6 complete-frame cycles |
|---|---:|---:|---:|---:|
| 0: baseline | 113,056 | 357,940 | 141,443 | 391,259 |
| 1: five-word C | 90,193 | 335,077 | 112,838 | 362,654 |
| 2: streamed Thumb-2 | 80,794 | 325,678 | 101,103 | 350,919 |

The 2/3 BCH pass uses about 28.5% fewer cycles; the complete encoder uses about 9.0% fewer. This is
not the proposed fourfold reduction to 25,000 cycles. LDPC already uses the rotated 360-bit
group approach. Its current profile distinguishes group construction, accumulation and
transpose; the entire LDPC cost cannot be attributed to a remaining bit-at-a-time scatter.

Two other proposed shortcuts need correction. RP2350's Hazard3 configuration explicitly has
`EXTENSION_ZBC = 0` (RP2350 datasheet section 3.8.8, p.302), so a hardware carry-less multiply
instruction is unavailable. The DMA sniffer's fixed CRC functions do not directly implement
the DVB-S2 160/168/192-bit BCH polynomial.

Chained DMA lookup remains an unmeasured experiment. The current LUT is separable: distinct
I/Q histories address two 32-KiB tables. A ready-interleaved joint L12 QPSK table would require
256 MiB. Two contiguous axis bursts also do not produce the current PAIRS order
`I01,Q01,I23,Q23`. A DMA version needs PIO reordering or several gathered fragments, plus CPU
history/pointer generation and bounded descriptor rings. At 8 Msym/s, two 32-bit descriptors
per symbol already require 64 MB/s of CPU descriptor writes, versus 2 MB/s for today's packed
symbol stream. The contiguous stored-pattern DDR test does not establish lookup/chaining
throughput. A two-SPS, two-pointer component test would be the simpler starting point.

## Measured rate envelope

All rows use normal QPSK 2/3, pilots, L12 and BCH kernel 2 unless identified otherwise.
`busy` is timed encoder plus shaper work divided by elapsed core cycles; it excludes untimed
monitor work and is not a guarantee of deadline margin. The input called "repeated frame"
is freshly encoded during the timed run, not replayed precomputed I/Q samples.

| Clock | CPUs | SPS | Input | Rate, Msym/s | Duration | Combined one-core busy | Result |
|---|---:|---:|---|---:|---:|---:|---|
| 128 MHz | 2 | 4 | repeated frame | 8.000 | 3 s | — | zero reported faults |
| 150 MHz | 2 | 4 | repeated frame | 9.375 | 60 s | — | zero reported faults |
| 128 MHz | 1 | 4 | repeated frame | 1.000 | 3 s | 16.4% | zero reported faults |
| 128 MHz | 1 | 4 | repeated frame | 4.000 | 3 s | 67.3% | zero reported faults |
| 150 MHz | 1 | 4 | repeated frame | 6.250 | 3 s | 88.5% | zero reported faults |
| 128 MHz | 1 | 4 | emulated TS/SPI | 4.000 | 3 s | 71.8% | zero reported faults |
| 150 MHz | 1 | 4 | emulated TS/SPI | 4.6875 | 3 s | 71.7% | zero reported faults |
| 128 MHz | 1 | 4 | emulated TS/SPI | 5.333 | 60 s | 94.2% | zero reported faults |
| 150 MHz | 1 | 4 | emulated TS/SPI | 6.250 | 60 s | 94.2% | zero reported faults |
| 128 MHz | 2 | 2 | repeated frame | 10.667 | 3 s | — | zero reported faults |
| 150 MHz | 2 | 2 | repeated frame | 12.500 | 60 s | — | zero reported faults |
| 128 MHz | 1 | 2 | repeated frame | 6.400 | 3 s | 85.7% | zero reported faults |
| 150 MHz | 1 | 2 | repeated frame | 7.500 | 60 s | 85.5% | zero reported faults |
| 128 MHz | 1 | 2 | repeated frame | 8.000 | 3 s | 99.4% | 895 underruns; capture did not align |

Sources: [quick-2](quick-2/runs.jsonl), [two-sps-1](two-sps-1/runs.jsonl),
[soak-1](soak-1/runs.jsonl), [final soak-2](soak-2/runs.jsonl). The final one-core SPI soaks encoded 9,617 and 11,269
runtime frames respectively, with zero reported transport errors or null packets. Their
minimum shaper lead reached zero: these are high-utilization demonstrations with little
timing reserve, not a recommended flight operating point.

All five 60-second profiles passed twice. The final `soak-2` repeat includes the counter fix:
TXSTALL is collected after disabling output, covering the last processing slice even if
the main monitoring loop did not run again before the deadline. Every final stream had zero
underruns, ownership errors and TX stalls, with 8,176 sampled bus words matching the reference.

At 2/3 with pilots, usable BB data after the 80-bit BBHEADER is
`(43040 - 80) / 33282 * symbol_rate`: 10.33 Mb/s at 8 Msym/s, 12.10 Mb/s at 9.375,
and 16.13 Mb/s at 12.5. This does not subtract any additional application overhead.

The two-SPS firmware runs use the existing RRC coefficients, without an exported AFE
pre-equalizer. Digital throughput does not establish acceptable DAC image rejection. The
existing [analog screening model](../../reference/sps_analysis.json) is magnitude-only,
uses typical filter curves and is sensitive to the assumed corner. Its 8-Msym/s two-SPS
tune-0 image is about -19 dBc; tune-4 plus finite pre-EQ reaches about -45 dBc in that model.
Those values are neither measured RF rejection nor a declared emission requirement.

## 8PSK: memory fits, throughput does not yet improve

| Clock | Table memory | Measured cycles/symbol | Shaper-only ceiling |
|---|---:|---:|---:|
| 128 MHz | 64 KiB | 37.372 | 3.425 Msym/s |
| 150 MHz | 64 KiB | 37.372 | 4.014 Msym/s |

Both device C and assembly produce CRC32 `4224917667` for 65,536 symbols seeded by
`0x1234abcd`, matching the independent Python quantized reference. Native tests also compare
block histories and direct complex convolution: maximum axis error is below the conservative
1.071-LSB bound. Baseline QPSK tables and PRBS are restored and rechecked after this test.
The full device-sized Python reference has a maximum axis error of 1.008705 LSB.

QPSK already has two binary axis LUTs. Rotated 8PSK has four levels per axis, decomposed here
as `x = A*s + B*t`, with `s,t` in `{+1,-1}`. This preserves small tables but adds sample reads,
packed additions and output masking. It is not a free reuse of the current 10.8-cycle kernel.
The current 8PSK gross bit rate is lower than the faster QPSK baseline. At 150 MHz, a shaper
would need less than 24 cycles/symbol to exceed the gross rate of 9.375-Msym/s QPSK, before
accounting for a real 8PSK encoder and continuous-output overhead.

## Faster output component

[ddr-3](ddr-3/runs.jsonl), repeated in [final ddr-4](ddr-4/runs.jsonl), ran for 100 ms at 128 MHz: target bus rate 128 million 16-bit
words/s, corresponding to 64 MS/s per I/Q DAC. The output SM reported no TX stall. Clock
capture contained 4,096 words with a stable alternating pattern; 8,192 captured data bus
words matched the stored source at a fixed stride of 17 words and one cyclic offset. The
production 8-Msym/s transmitter was then rerun for three seconds without reported faults.

The data capture is deliberately sparse, not a verification of every outgoing bus word.
The experiment does not measure pad setup/hold, differential clock conversion, phase across
the PIO/HSTX interface, AFE clock-domain behavior or RF output. It also does not integrate
the encoder or RRC shaper at that rate. Production `iqout.c` still uses two system-clock
instructions per bus word. The AFE's 65-MS/s-per-DAC / 130-Mword/s input ratings do not by
themselves close the MCU generation or board timing budgets.

The AFE7071's DDR mode uses its **single differential DACCLKP/N input**, with CLK_IO disabled;
I is captured on the rising edge and Q on the falling edge (datasheet pp.25-28). CLK_IO modes
are SDR. The experiment's HSTX output on GPIO16 is a single-ended clock candidate, not a
drop-in DDR CLK_IO connection. An integrated implementation needs the correct clock buffer,
DACCLKP/N routing, initial edge/IQ alignment, setup/hold and synchronization.

## Negative results and provenance

- [quick-1](quick-1/runs.jsonl) had 17 underruns at 128 MHz, one-core, four-SPS,
  emulated SPI, 5.333 Msym/s. A later quick run had none. The later scheduler separates
  BB construction from BB+BCH, and the 60-second soaks above exercise that implementation.
  The original combined slice reached 171,186 cycles, exceeding the approximately 147,456
  cycles of queued output available at that point. The separate stages reduce the longest
  slice to about 132,000 cycles. Average utilization alone did not expose that deadline miss.
  The earlier failure remains evidence of scheduling sensitivity; it is not overwritten.
- [two-sps-1](two-sps-1/runs.jsonl) retains the failed one-core 8-Msym/s ceiling probe.
- [ddr-1](ddr-1/runs.jsonl) and [ddr-2](ddr-2/runs.jsonl) retain unsuccessful clock/data
  checks while capture setup was being developed. DDR-3 uses steady-state capture with a
  documented fixed stride and checks the complete captured sequence; the old failed
  harnesses do not establish that the underlying source data rate was impossible.

Each directory has a manifest recording HEAD, dirty status, local source hashes and build
ELF/UF2 hashes at the start of that run. Early runs used `741defe613` plus working-tree
changes. Final code is commit `02efcc8d74ec29396c79a545d4aae025c0687b0f`; only documentation
and results were dirty during the final tests. The final manifests also hash the independent
reference sources. [final-flash.json](final-flash.json) records the successful `picotool -v`
flash verification before `soak-2` and `ddr-4`; their manifests identify that ELF/UF2.
Sources and validation evolved during earlier runs, so compare manifests and individual
records rather than treating a shared dirty revision or main-file compile timestamp as
identical firmware. An early source snapshot alone does not establish which binary was flashed.

`underruns`, ownership errors, TX stalls and PV transport counters cover the runtime;
captured bus words are only a finite exact snapshot. PV-SPI here is generated by an on-chip
PIO emulator, not a wired payload computer. No continuous end-to-end RF decode, complete
long-run sample comparison, external SPI margin, spectral mask or link closure is claimed.

## Reproduce

From the repository root, with the SDK/toolchain configured as for the main benchmark:

```sh
python3 host/test_dvbs2_native.py
python3 host/test_iq8.py
make BUILD=firmware/build-limits build
picotool load -v -x firmware/build-limits/iqbench.uf2 -f
python3 host/limits.py quick --build firmware/build-limits --out results/limits/new-quick
python3 host/limits.py two-sps --build firmware/build-limits --out results/limits/new-two-sps
python3 host/limits.py ddr --build firmware/build-limits --out results/limits/new-ddr
python3 host/limits.py soak --ms 60000 --build firmware/build-limits --out results/limits/new-soak
```

To regenerate the independent 65,536-symbol 8PSK CRC used by the device sweep:

```sh
python3 -c 'import sys,zlib; sys.path.insert(0,"host"); import test_iq8 as t; t.GROUPS=4096; print(zlib.crc32(t.reference(0x1234abcd)[0].tobytes()))'
# 4224917667
```

Use new output directories; the runner refuses to replace an existing `runs.jsonl`. Disconnect
external GPIO wiring before these pin-mux/output experiments. `--port` selects a specific USB
serial device. The sweep preserves expected ceiling-probe failures and reports unexpected
validation faults. Build and flash explicitly before running; the sweep does neither.

## Next experiments

1. Establish margin around the one-core rate envelope with real payload SPI and bounded
   service work. Measure stage maxima as well as average utilization. Resumable LDPC or
   mapping chunks may improve latency reserve but do not remove their CPU cost.
2. Prototype a three-SPS layout/kernel. At 150 MHz and two clocks/bus word its interface
   ceiling is 12.5 Msym/s, DAC 37.5 MS/s. With roll-off 0.20 its first image starts at 30 MHz,
   compared with 17.5 MHz for two SPS at the same symbol rate. Existing even-SPS PAIRS
   packing and block arithmetic require changes; a 25% CPU saving is not established.
3. Test higher existing QPSK code rates in the integrated pipeline when the link permits.
   Normal 5/6 gives about 15.14 Mb/s at 9.375 Msym/s with pilots, versus 12.10 Mb/s at 2/3,
   without a new constellation shaper. This exchanges coding margin for throughput.
4. Measure the AFE's actual filter response and images before adopting two SPS. A finite
   equalizer needs headroom and tolerance analysis; the current magnitude-only simulation
   does not establish phase compensation or part/temperature spread.
5. Optimize the 8PSK kernel toward the measured-throughput break-even target before adding
   its DVB-S2 interleaver/mapper and continuous streaming. Then evaluate amplifier and link
   tradeoffs; fitting LUT memory alone has already been demonstrated.
6. Integrate and time the faster DDR output separately, preserving an independent clock
   and verifying AFE timing on actual hardware. Passing the stored-pattern component check
   does not double full-transmitter throughput.
7. Treat OFDM as a separate waveform experiment: timed fixed-point IFFT, pilots, cyclic
   prefix, coding, buffering and receiver synchronization. The present binary-history RRC
   LUT is not an arbitrary OFDM generator, and amplifier peak headroom remains a trade.
