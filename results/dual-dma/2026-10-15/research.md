# Further RP2350 datapath experiments

These are ranked experiments for the current `experiment/yapogee-limits` work, based on
`684e05d` plus the development changes described below. They distinguish measured component
behavior, architectural calculations and untested estimates. All board work in this pass used
USB only, without an AFE or external payload sender. No result here qualifies RF output,
electrical timing at an AFE, or a complete higher-rate modem.

The starting point is the previous campaign's 16 Msym/s QPSK result at 128 MHz: L=12,
4 samples/symbol, paired lookup rows, four PIO writers and split BCH/backend encoding.
Its measured core 0 work was generation 45.51% + control 12.50% + BCH 39.55% = 97.56%; core 1's
backend was 91.91%. Those measurements motivate moving work out of both processors, rather
than extrapolating an isolated shaper kernel's throughput.

## 1. Generate the gather addresses in PIO

**Priority:** highest. **Expected benefit:** remove CPU descriptor generation and some of its
ring housekeeping. **Cost:** a new input format, four extra PIO state machines and four extra
DMA channels, an aligned/permuted table, and a new ownership proof. The benefit is a hypothesis
until the existing output engine runs with these generated pointers under full traffic.

The [pin-free program](../../../firmware/src/ptrprog.h) is 16 instructions shared by four state
machines. Each consumes one binary I or Q axis, updates a 13-bit history in X and publishes one
numerical address per four global symbols. Two phase-offset machines per axis cover alternate
two-symbol rows. Y holds a 128-KiB-aligned table prefix. An address is

```
address = (Y << 17) | ((X & 8191) << 4)
```

The generator keeps the newest history bit at bit 0. The existing paired table uses the opposite
history ordering, so integration must reverse each 13-bit key when placing table rows. This is
a table permutation; the four samples in each row need not change. Input halfwords have each
adjacent pair of bits swapped, allowing `IN OSR, 2` to insert chronological pairs in the required
order. A 16-bit DMA write replicates a halfword into both halves of the TX FIFO word; the program
uses only the first 16 shifted bits, with a manual PULL and threshold 16. These behaviors follow
the [RP2350 datasheet](https://pip-assets.raspberrypi.com/categories/1214-rp2350/documents/RP-008373-DS-2-rp2350-datasheet.pdf?disposition=inline),
PIO ISA/shift counters in sections 11.4–11.5 and DMA byte-lane replication in section 12.6.

The instruction model costs 14 PIO instructions per four symbols, plus one PULL per 16 symbols:
`57/16 = 3.5625 cycles/symbol`. At 128 MHz and 16 Msym/s the available interval is 8 cycles/symbol,
so this leaves substantial instruction-time margin in the address generators. It does not
predict the gather DMA's memory contention.

**Measured component proof:** [frozen `ptrbench 32` record](device-checks-2/pointers.json),
128 MHz, four pointer state machines, eight DMA channels, 32 repetitions of 4096 global
symbols each. All 32 repetitions completed, with zero address mismatches, TX stalls and AHB
errors. The mean was 3.59148 cycles/global-symbol, corresponding to 35.640 million global symbols/s
for this numerical-address component. The expected post-capture RX backpressure mask was 15.
The four captured 1024-address CRCs were 2456767275, 1773279408, 2149571791 and 1670686239.
[The independent host instruction model](../../../host/test_ptrprog.py) checks 200 combinations
of seeds, stream lengths, phases and aligned bases, including 16/32-bit boundaries. Its
`--record` mode reproduced all four captured CRCs without trusting the firmware's expected CRCs.

This benchmark copies addresses from PIO RX FIFOs to SRAM. It does not dereference the LUT,
drive the data bus, encode FEC or accept SPI input. Its rate must not appear as an integrated
transmitter rate.

**Smallest next experiment:** replace each of the four control DMA channels' descriptor-SRAM
source with the corresponding pointer SM's RX FIFO, paced by that RX DREQ. Keep the existing
data DMA transfer count 4 and data-completion→control chain. Only the completed data transfer
may permit installation of the next row address. First run precomputed input at 8 Msym/s,
then 16 Msym/s, with all 8192 sparse bus observations checked against the same independent
waveform reference. Exercise deliberate input starvation and a stopped pointer SM: both must
stop coherently and report failure. Only then add live FEC and SPI input.

**Integration constraints:**

- Current `ring_buf` is not guaranteed 128-KiB aligned. Numerical address generation has not
  solved the real table placement. A linker/memory-layout change needs a checked link map.
- Four output SMs + four pointer SMs + one capture SM + three PV-SPI self-test SMs exactly
  consume all 12 SMs. Four input DMA + four control + four data + capture 1 + PV-SPI self-test 3
  exactly consume all 16 DMA channels. Real-input mode can need fewer resources, but the
  present PV-SPI allocator still reserves three channels. PV-SPI now adds and removes its own
  programs, but sharing a PIO block still needs an explicit instruction/SM allocation. The
  legacy output bench also retains three DMA channels and two SM claims while idle; integration
  must release those claims before this full self-test resource budget can fit.
- Existing 64 KiB descriptor storage becomes available; two axis input rings can total 32 KiB.
  This is promising capacity, not a completed memory layout.
- RX FIFO backpressure safely paces pointer publication. It cannot detect the CPU overwriting
  source data still needed by a PIO or input DMA. Track all four input cursors, include FIFO/OSR
  lookahead in the ownership guard, and detect stale-ring replay independently of FIFO stalls.
- At 16 Msym/s, the approximate aggregate DMA traffic is 64 million sample-word transfers + 16
  million pointer/control transfers + 4 million input-halfword transfers per second. The 84
  million total is below a 128 MHz ideal one-read/one-write-per-cycle engine ceiling, but SRAM
  and peripheral arbitration can still break deadlines. That arithmetic is not a timing proof.
- FEC, input packing, transport and fault monitoring still require CPU time. Core 1 was already
  close to its limit; removing descriptor work cannot by itself increase full-modem rate.

## 2. Reduce the 8PSK sample work, then test the modem separately

**Priority:** second for increased bits per hertz; deferred during this pass after the pointer
proof became the concrete architectural experiment. **Expected benefit:** fewer instructions
and half the sample traffic with 2 samples/symbol. **Cost:** new shaper kernels and reference
checks, then an actual DVB-S2 8PSK mapper/interleaver and a link/SNR trade study.

The existing [8PSK experiment](../../../firmware/src/iq8.c) is L=12, N=4 and represents each
axis as the sum of two binary-amplitude planes. Its two tables occupy 64 KiB. The previously
measured assembly shaper costs 37.372 cycles/symbol. It is an isolated random-symbol waveform
experiment, not a DVB-S2 8PSK encoder.

There is a concrete smaller kernel to test. PIO can drive 14 data pins and supply the I/Q flag
separately, as the current paired QPSK writer already does. This removes four masks and two
flag ORs per symbol from the existing 8PSK body. At N=2, the body would contain four history
extracts, four table loads, two packed adds and two stores: 12 instructions before per-group,
load-use and bus costs. Tables fall to 32 KiB. The [Arm SADD16 instruction](https://documentation-service.arm.com/static/5f16e93d20b7cf4bc524af1d)
already supplies the two independent signed 16-bit additions; no new silicon feature is needed.

An estimated 16–22 cycles/symbol is a useful hypothesis, not a measured result. An 8 Msym/s
target allows 16 cycles/symbol at 128 MHz or 18.75 at 150 MHz, so even this reduced kernel may
need both cores or a different work split. The deciding test is a standalone N=2 kernel against
an independent quantized FIR reference, with the existing N=4 CRC preserved; then run its PIO
output under concurrent memory traffic. Check numerical headroom before discarding upper
sample bits. The changed sampling also needs reconstruction/EVM/spectral verification.

Normal DVB-S2 rate 2/3 8PSK requires the 3-column, 21600-row bit interleaver, constellation
mapping and the appropriate PL signalling/header/pilots/scrambling. These follow
[ETSI EN 302307 V1.2.1](https://etsi.org/deliver/etsi_en/302300_302399/302307/01.02.01_60/en_302307v010201p.pdf),
sections 5.3.3, 5.4 and 5.5, not simply relabelling the current QPSK output. For the normal 2/3
case with pilots, a frame has 42960 data-field bits and 22194 symbols
(`21600 data + 90 header + 14×36 pilots`). At 8 Msym/s that is 15.4853 Mb/s of data-field
capacity, versus 10.3263 Mb/s for normal 2/3 QPSK with pilots. This roughly 50% capacity gain
does not establish sufficient received SNR or compliant RF performance.

## 3. Measure contention before changing more SRAM placement

**Priority:** third. **Expected benefit:** potentially useful CPU margin without waveform or
protocol changes; no guaranteed percentage. **Cost:** small counter instrumentation and
controlled linker variants. The current changes already move BCH to scratchY and the tiled
transpose/mapping leaves to scratchX, so repeating those suggestions would add nothing.

The RP2350 has four 24-bit saturating bus performance counters, with access, contention and
upstream/downstream stall events; see BUSCTRL in sections 2.1.7 and 12.15 of the
[manufacturer datasheet](https://pip-assets.raspberrypi.com/categories/1214-rp2350/documents/RP-008373-DS-2-rp2350-datasheet.pdf?disposition=inline).
Use short fixed observation windows and record counter configuration plus saturation status.
Compare code fetch, FEC workspace and LUT DMA pressure before moving data. A counter increase
alone is not a regression: the decision metric is complete producer/encoder time and output
correctness at the same clock, input and run duration.

The development link map already places about 252 KiB in the 256-KiB SRAM4–7 region. The
lower SRAM allocation is also close to its 256-KiB region limit. There is no spare 384-KiB
table implied by the RP2350's 520-KiB headline SRAM figure, and scratch-bank code must leave
the checked stack region intact. Record the final ELF map and test maximum stack usage for
any new placement. Re-run the same sparse output check and starvation controls after each
variant; do not attribute a clock/build/input change to SRAM placement.

## 4. Carry the input and scheduling proof through to a real sender

**Priority:** fourth as an architecture change, but necessary before adopting a higher-rate
transmitter. **Expected benefit:** preserve end-to-end rate with realistic transport and
backpressure. **Cost:** external sender qualification and balanced frontend/backend work.

The [final SPI campaign](spi-2/runs.jsonl) passed 60 seconds at 16 Msym/s with the 32 MHz
on-chip PV-SPI source. Core 0 timed work was 84.91% (85.62% active); core 1 backend 75.79% +
TS work 17.24% = 93.03%. All captured observations and ingress CRC checks passed, with
117,732 accepted messages, no reported loss, and no null insertion. This remains a shared-clock
on-chip input test; an asynchronous external sender still needs qualification.

The [earlier frozen test](spi-1/runs.jsonl) was rejected for one short transaction and one
reported sequence loss despite clean output counters and an exact sparse snapshot. The
receiver now waits, within a 3 µs bound, for both zero DMA count and BUSY clear before reading
the CRC or reusing the slot. A [diagnostic soak](development-spi-drain-1/runs.jsonl) recovered
one deferred completion without losing a message. This supports a receive/DMA timing cause;
it does not uniquely identify the earlier failure. Late chip-select service is separately
counted and rejected. The failed evidence is retained alongside the passing final image.

For the present 1332-byte message, 1316-byte TS payload and 10 µs intermessage gap, the ideal
maximum TS rate at SCK frequency `f` is

```
R_TS = (1316×8) / ((1332×8)/f + 10e-6) bits/s
```

This gives 19.3957 Mb/s at 20 MHz, 23.1894 at 24 MHz and 30.6939 at 32 MHz. Normal 2/3 QPSK
with pilots at 16 Msym/s carries 20.6526 Mb/s of data-field capacity, so 20 MHz cannot continuously
fill that capacity even with an otherwise ideal sender; the minimum clock in this model is
about 21.322 MHz. TS payload rate is distinct from encoded symbol rate and from the 128 Mword/s
16-bit I/Q sample bus. Software latency and READY gaps reduce the ideal transport numbers.

Replacing the PIO receiver with the built-in SPI peripheral is not a speed shortcut. The
[RP2350 SPI slave timing requirement](https://pip-assets.raspberrypi.com/categories/1214-rp2350/documents/RP-008373-DS-2-rp2350-datasheet.pdf?disposition=inline)
is `clk_peri ≥ 12×SCK` (section 12.3.4.4), limiting it to 12.5 MHz at 150 MHz. A two-lane 12 MHz
or four-lane 8 MHz custom ingress is a testable alternative only if the payload sender can
provide that format; it changes the protocol and pins. First qualify the current PIO input
with an external asynchronous sender, long gaps, CRC/sequence faults and READY withdrawal,
while checking both ingress accounting and independently captured output.

## Attractive ideas that do not bypass the present limits

- DMA cannot read SIO, including the interpolators. The datasheet states this explicitly in
  section 12.6.6.3. An interpolator address trick still needs CPU reads unless another peripheral
  produces the address. The PIO proof above is such a producer.
- The Arm and Hazard3 personalities do not give four simultaneously running CPUs. Hazard3
  does not implement Zbc carryless multiplication. No single-cycle BCH CLMUL improvement is
  available on that premise.
- HSTX has eight outputs on GPIO 12–19. Its bit crossbar/shifter and TMDS support do not provide
  an arbitrary FIR accumulator or a drop-in replacement for the current 14-data-bit-plus-I/Q bus.
- A three-symbol L=12, N=4 binary-axis table has 16384 rows × 6 words = 384 KiB; padding each row
  to a power-of-two 32-byte address stride makes it 512 KiB. Those tables leave inadequate room
  for the rest of this design. Full 8PSK history tables are much larger: 128 MiB per four-level
  axis, or 1 TiB for a joint 12-symbol, 8-point I/Q history with four complex samples per row.
- The [AFE7071](https://www.ti.com/product/AFE7071) has two 14-bit 65-MSPS DACs and an interleaved
  input interface. The datasheet's analog filtering does not establish a digital interpolator
  that accepts an arbitrarily lower sample rate. Digital captures do not establish its clock
  phase, setup/hold, reconstruction images, output EVM or the satellite link budget.

The concrete result of this research pass is the independently checked PIO pointer component.
The next architectural decision is whether it can feed the existing four-writer gather engine
with full FEC and ingress, while preserving coherent fault detection and explicit resource
ownership. That integration is still work to do.

## FEC implementation review

The mapping leaf's six stream fields, stack argument offsets, zero-word case, partial-word
accumulators and masked ring indexing were independently reviewed against `symstream_put16`.
The byte-unshuffle table reuses the Gold-plane storage only after all initialization-time
readers finish. The q=60 LDPC packer fits its temporary columns inside the unused P workspace,
and in-place parity writes start only after all information bits have been consumed. No
correctness blocker was found in that review.

The three global assembly blocks were extracted to
[dvbs2_asm.S](../../../firmware/src/dvbs2_asm.S) for readability. Isolated compilation with the
SDK's GCC 14.2 Arm toolchain produced byte-identical code sections before and after extraction:
BCH plus continuation 260 bytes, transpose 308 bytes and mapping 160 bytes. The C prototypes,
portable fallbacks and inline rotate-XOR constraints were retained. Native focused checks pass
1152 independently defined transpose matrices, eight q=60 serial-parity/canary patterns, and
all sixteen partial stream fills with ring wrap. Native tests exercise the portable path;
frozen-build device CRCs remain the check on actual Arm execution and final placement.
