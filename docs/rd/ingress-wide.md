# Wide payload ingress: source-clocked four-bit PV messages

**Device result (coordinator, 2026-10-16):** with the on-chip sender, four-bit ingress ran 60 s clean
at 30.5, 40.2 and 47.8 Mb/s (8, 10.7, 12.8 MHz) and 76.9 Mb/s for 3 s at 21.3 MHz; CRC and short
controls detected. External timing unmeasured. See [results](../../results/ingress-wide/2026-10-16/README.md).

2026-10-16, `rd/ingress-wide`. **Implementation and native verification; no device
measurement.** No Pico, serial port, flash operation or Git metadata was touched.

The selected experiment receives the existing 1332-byte PV message on four data
pins, with one sender-supplied clock, CS_N and READY. It preserves message CRC,
sequence numbers and one-message admission credits. Width one is the control in
the same image. This avoids independent-lane reordering and keeps one hardware
CRC stream. It is a transport benchmark, not a newly integrated transmitter or a
CM5 driver.

## Why widen the bus

The external CM5 evidence is [the SPI campaign](../../results/cm5-spi/README.md);
the ingress ceiling calculation is in [the frontier](../frontier/frontier.md).
At 25 MHz, 1332 bytes on the wire and a 10 us gap carry

```
Rpayload = 1316 * 8 / (1332 * 8 / SCK + gap)
         = 24.132 Mb/s at SCK = 25 MHz, gap = 10 us.
```

Even zero gap gives only 24.700 Mb/s. The required 25.84, 27.94, approximately
31, and 47 Mb/s therefore need a faster or wider physical transport. The last
number is a coded-bit transport requirement; retaining PV containers does not
by itself implement an upstream-FEC framing/mapping protocol. The benchmark
uses full TS_DATA containers to keep its reference independent and established.

For width W, replace SCK by W*SCK in that equation. These are **calculated
ceilings**, with 10 us gaps and no CPU/backpressure pauses:

| Useful Mb/s | One SPI, MHz | Two independent SPI, MHz each | Four independent SPI, MHz each | Four-bit bus, MHz | Eight-bit bus, MHz |
|---:|---:|---:|---:|---:|---:|
| 25.84 | 26.81 | 13.24 | 6.58 | 6.70 | 3.35 |
| 27.94 | 29.05 | 14.33 | 7.12 | 7.26 | 3.63 |
| 31 | 32.33 | 15.92 | 7.90 | 8.08 | 4.04 |
| 47 | 49.80 | 24.33 | 12.03 | 12.45 | 6.22 |

Independent lanes send distinct complete messages concurrently, each with its
own gap. They are not four bits of the same message and have different gap
amortization. Do not multiply the measured single-lane throughput without also
accounting for the sender and receiver scheduling.

## Candidate comparison

Counts below are proposed **external-receiver** resources, excluding the
emulated sender. The existing [PV-SPI](../../firmware/src/ingress/pvspi.c) uses a
four-word receiver plus a five-word CS/READY gate; it actually reserves an
emulator SM and two extra DMA channels even with its emulator disabled. The
candidate counts distinguish useful production resources from that legacy
allocation behavior.

| Candidate | SMs / instruction words | DMA | Pins, excluding ground | CPU and limitations |
|---|---|---|---|---|
| Two independent mode-0 SPI lanes | 4 SM; 18 words with direct absolute-pin programs; a shared relative-pin program can use 9 per block | 2 RX minimum | 8 with per-lane READY; 7 with a redesigned shared credit | Merge whole messages by sequence; CRC problem below |
| Four independent SPI lanes | 8 SM; 36 direct words spread across blocks, or 9 shared words per two-lane block | 4 RX minimum | 16 with per-lane READY; 13 with shared credit | Does not fit beside B6; more slot/reorder state |
| Four-bit source-clocked PIO | 2 SM; 9 basic words, **13 implemented with length/extra-edge checks** | 1 RX | 7 | No lane merge, no CPU unpacking, one CRC stream |
| Eight-bit source-clocked PIO | Same SM/program/DMA counts | 1 RX | 11 | Lower clock, but no documented usable contiguous eight-pin carrier bus |
| RP2350 hardware SPI master | 0 SM / 0 PIO words | 2, RX plus dummy TX | 4 with GPIO CS and READY | APB traffic; requires CM5 slave capability and changed role/flow control |

The hardware CRC sniffer selects **one** DMA channel, not four independent CRC
contexts (RP2350 build 2025-07-29, section 12.6.8.2 p1107 and Table 1179 p1136,
checked against the supplied source text). Parallel SPI lanes need software
CRC, serialized receptions, or a later serialized CRC-DMA pass over immutable
slots. That pass needs another channel and reads the data again. Sharing one
READY among lanes additionally requires reserving every participating slot
before exposing credit. A bounded sequence window must handle wrap, missing
messages, duplicated messages, timeout and backpressure without deadlocking
the oldest message. None of that is free in the nearly full SRAM image.

Hardware SPI has two instances, eight-entry 16-bit FIFOs, DMA and 4..16-bit
words (section 12.3 p1046). Its clock divider gives a nominal 64 MHz at
clk_peri=128 MHz with CPSDVSR=2 and SCR=0 (sections 12.3.3.3 p1048 and
12.3.4.6.1 p1051); this is divider arithmetic, not a measured sustainable
link. Mode-0 hardware CS pulses between words (section 12.3.4.10 pp1053–1054),
so a whole-message GPIO CS is needed to preserve PV framing. GPIO READY must
be sampled before starting that CS. Automatic-CS mode 1 has different framing
and is not the existing mode-0 protocol.

PIO RX consumes one FASTPERI read and one SRAM write per packed 32-bit word.
At useful rate R Mb/s, that is R*(1332/1316)/32 million transfers/s, independent
of bus width or number of lanes. At 47 Mb/s: **1.4866 M transfers/s**, 5.9464
MB/s of SRAM writes and **1.1614%** of the 128 MHz FASTPERI port. Rearm/IRQ,
sniffer and CPU register accesses are additional. An on-chip sender adds the
same rate of FIFO writes plus source SRAM reads. RP2350 sections 2.1 p24 and
2.1.4 p26 put SPI on APB and PIO/DMA-control/USB on FASTPERI. APB reads take at
least three SYS cycles and writes four. At 47 Mb/s, 16-bit SPI RX plus dummy TX
therefore costs at least 20.81 M APB cycles/s, or 16.26% of SYS128; eight-bit
words double this. It avoids the PIO FIFO port but remains a bus consumer.

The established **GCS adaptation** cost is 0.61 cycles/payload bit, not a
measurement of this receiver: 0.610 M cycles/s per Mb/s, **0.4766% of one
SYS128 core per Mb/s**, or 22.40% at 47 Mb/s. Widening changes neither that
copy/BBFRAME work nor FEC. RX shifting, packing, transfer and wire CRC require
zero CPU instructions per bit in the selected design. Per-message close,
validation and optional full-pattern verification still cost CPU; the command
reports them separately for device measurement. Software CRC on multiple SPI
lanes would add a full pass: the existing slicing-by-four C loop has four
table loads per four bytes plus shifts/XORs and a loop branch, so its cost
must be budgeted from its M33 disassembly and measured, not assumed to be
included in 0.61. No new host timing is substituted for M33 cycles.

## Pins and electrical timing

The local [hardware draft](../../hardware/devboard/README.md) explicitly marks
its pin plan superseded. [PV-SPI v1](../pv-spi-spec.md) says the final carrier
connector/pinout is unfinished. No current Yapogee schematic/netlist in this
repository establishes free pins. **The following is a bench assumption and
requires a carrier routing change**, not a claim of drop-in compatibility:

| Signal | Benchmark GPIO |
|---|---|
| D0..D3, least significant bit of each nibble on D0 | 17..20 |
| Sender clock, idle low | 21 |
| CS_N | 26 |
| READY, active high | 22 |

Width one uses D0 only. This repurposes the existing PV-SPI pins, adds GP21
and moves CS_N to GP26; original `pvtx` pin assignments are unchanged. DAC
GPIO6..14 and GPIO28 are untouched. On Pico Plus 2 GP26 has a 1 kohm branch
to GP40: leave GP40 an input. GP21/26 may already have carrier control duties;
the historical draft cannot settle that. An eight-bit contiguous GP18..25 bus
is unsuitable for this bench: GP23/24 are not listed as header pins and GP25
is the board LED. A new bare-RP2350B carrier could route another contiguous
group, but no such allocation is established here. Multi-SPI's extra pins
are also assumptions; MISO/status adds one pin per lane if implemented.

I/O is assumed common-ground 3.3 V CMOS, with both ends meeting the actual
voltage/drive specifications. RP2350 Table 1436 pp1339–1340 gives VIH>=2.0 V,
VIL<=0.8 V at 3.3 V. No CM5 output-voltage compatibility is newly established.
Do not use the unrelated QMI clock-to-pad figures as a guaranteed PIO input
setup/hold specification. Leave PIO input synchronizers enabled for the
asynchronous external source (section 11.5.6.3 p913: two-cycle synchronization,
per-pin bypass available). Data changes on falling edges and is sampled after
rising edges, high nibble first. All data bits and clock use the same enabled
synchronizer path. The receiver executes WAIT-high, IN, WAIT-low, JMP per beat;
at most four nonstalled instructions/beat, with additional edge-detection
latency. SYS128 is 7.8125 ns/cycle.

At 12.8 MHz the period is 78.125 ns, each half 39.0625 ns (five SYS cycles).
At 16 MHz each half is 31.25 ns (four SYS cycles). The command permits a
three-cycle half (21.333 MHz), as a stress point; it is not a certified
external timing limit. The two-flop/WAIT/IN phase must leave the data stable
at the actual sample; source clock/data skew, interconnect delay, slew and
metastability can consume that window. Scope clock and every data pin at the
RP2350 pads, across temperature/load, and compare capture errors before
qualifying an external rate. Short matched traces, a solid return path and
source series-resistor footprints are implementation proposals, not measured
signal integrity. The internal sender shares SYS and cannot validate this
asynchronous electrical margin.

## What is implemented

`pvwide` is compiled only into experimental `iqbench`, under the existing
`IQ4HIST_EXPERIMENT` option. It uses borrowed idle transmitter buffers and
does not alter `pvspi.c`, `pvproto.c`, autonomous `pvflight`, or default output
selection. [The implementation](../../firmware/src/ingress/pvwide.c) has these commands:

```
pvwide ms width half fault [emulate]
# ms100..60000; width1/4; half3..16 SYS clocks; fault0/1/2; emulate defaults1
```

`emulate=1` installs three SMs and three DMA channels on idle PIO2, using 25
instruction words: RX8, gate5, sender12. Sender DMA byte-swaps 32-bit reads;
MSB-first OUT4 drives the high nibble before the low nibble. IN4 shifts left
and RX DMA byte-swaps again, so SRAM contains the original bytes without a CPU
shuffle. The sender loops over sixteen immutable messages with a two-channel
DMA rewind. It waits for READY before every CS assertion and inserts at least
1536 SYS cycles of CS-high gap (12 us), plus loop/control instructions.

`emulate=0` is an external receive-only benchmark: two SMs, one DMA, 13 words,
same pins, fault must be zero. It accepts ordinary v1 headers/lengths and
65536-value sequence wrap; `half` does not set an external source's clock and
`wire_hz=0` explicitly means unmeasured. The host sweep runner qualifies only
the internal deterministic source; external sender evidence must be compared
with its own transmitted counts, byte totals, sequence and CRC chains.

The receiver counts every bit/nibble. After exactly 10656/width beats, it
parks at its terminal WAIT. One extra clock edge sets sticky PIO IRQ1, even
when the excess is smaller than a DMA word. CS rising raises IRQ0 through the
five-word admission gate. Closing checks terminal PC, no extra-edge flag,
exact DMA count, BUSY clear, no AHB error, empty FIFO and CS high. A bounded
3 us drain allows the last PIO/DMA operations to retire. CRC publication
follows retirement. Every close resets ISR history, shift count and X before
arming another buffer and posting one credit. READY falls on CS falling in
PIO, and stays low through IRQ/realignment. Repeated consumer polls cannot
post duplicate tokens. A full queue withdraws credit until a slot is consumed.

The consumer calls the existing `pv_accept` with the hardware residue result,
checks accepted emulator messages byte for byte, and accumulates an extra
software CRC over accepted payload. Each service call is bounded to 24 slots,
so a continuously refilling queue cannot prevent the duration check. The
software payload CRC is verification work, separate from the hardware wire
CRC. Fault1 flips byte20 of source message0 **after** its CRC is generated,
once per sixteen messages. Fault2 omits the last four bytes of **every**
message, with a word-aligned packed source; no short message can be accepted.

Shutdown first forces READY low, drains the admitted transfer, then disables
SMs/interrupts, breaks DMA chains before abort/cleanup (RP2350-E5), releases
claims and restores pin/pad/SIO and PIO output state, synchronizer settings and
sniffer registers. External shutdown allows 10 ms; a source still holding CS
low sets `shutdown_inflight` and an error. The command is idle-only and checks
PIO2, IRQ, instruction space, DMA and sniffer availability before borrowing
storage. It is not an API for starting beside an already-running B6 engine.

## Cycle, SRAM and integration budgets

The calculated rate limits below use nominal 12 us emulator gaps, omitting a
few control instructions, CPU pauses and arbitration. Device `payload_mbps`
includes startup and final drain, so it must fall below these ceilings.

| Width | half | Clock MHz | Payload ceiling Mb/s | Purpose |
|---:|---:|---:|---:|---|
| 1 | 5 | 12.8 | 12.47 | Same-image one-bit control |
| 4 | 10 | 6.4 | 24.58 | Just above the old external ceiling |
| 4 | 8 | 8 | 30.52 | QPSK 5/6 and 9/10 target |
| 4 | 6 | 10.667 | 40.22 | Approximately 31 Mb/s payload target |
| 4 | 5 | 12.8 | 47.83 | Upstream-coded-bit transport target |
| 4 | 4 | 16 | 58.98 | Timing/throughput stress |

The actual sender has 2 control cycles under CS, 1539 cycles in the three
gap loops, a READY instruction and a new CS instruction: approximately
`2*half*(10656/width) + 1543` cycles/message with no stalls. At half5/width4
this gives **47.81 Mb/s**, still only 1.7% above 47 Mb/s. A pass at half4
would provide more transport reserve; neither rate is measured here.

M33 estimates follow [the project instruction timing notes](../rp2350-notes.md),
from GCC14.2.1 Release disassembly, with warm XIP and no extra bus contention:

- The existing SRAM `pv_crc32` loop has 18 instructions per four bytes,
  including five loads and its branch. With the taken-branch increment and
  the immediately consumed input load, about 20 cycles/word is an optimistic
  estimate: 329*20 plus setup, approximately **6.6k cycles per 1316 bytes**,
  **0.63 cycles/payload bit**. Shift penalties and bank conflicts can raise it.
  At 47 Mb/s this verification CRC alone uses approximately 23% of a core;
  it costs approximately **0.491% of a core per Mb/s**.
- The aligned successful `memcmp` loop executes nine instructions/word,
  including two loads, an untaken mismatch branch and a taken loop branch.
  About 10–12 cycles/word gives **3.3–4.0k cycles/message**, approximately
  0.32–0.38 cycles/payload bit, approximately **0.250–0.297% of a core per
  Mb/s**, another 12–14% of a core at 47 Mb/s.
- The fully retired successful close/reset/rearm path is approximately 197
  dynamic instruction slots (147 in the handler and 50 in reset), before
  register wait states, exception entry/return and XIP misses. An optimistic
  **240–340 cycles/message** estimate gives 0.023–0.032 cycles/payload bit or
  **0.0178–0.0252% of a SYS128 core per Mb/s**. Extra pin-latch restoration is
  shutdown work, outside this path. It needs measurement; this is not a
  worst-case service bound.
- Consumer bookkeeping, excluding callees, is approximately 92 instruction
  slots for the emulator (80 external), plus around 27 if it must rearm after
  a full queue. A **120–180 cycles/message** planning estimate adds
  **0.0089–0.0134% of a core per Mb/s**, before the callee costs above.

Protocol validation is additional. The verification command can therefore
spend roughly 37–40% of a core at 47 Mb/s before that validation, even though
wire shifting/packing/CRC are offloaded. This work is
not the existing GCS BBFRAME adaptation, and it is not added to a live FEC
measurement. A production source could omit pattern comparison and payload
CRC only after independent link qualification; this benchmark always checks
them. `close_cycles` records per-IRQ bounded DWT deltas into a 64-bit total;
`consume_cycles` subtracts measured nested close work to avoid double counting.
These are instrumented regions, excluding the ISR prologue/epilogue and
exception entry/exit, empty polling and setup; unrelated IRQs can also affect
the consumer measurement. The
foreground currently spins for the whole benchmark, reserving one core; its
reported busy fractions do not establish that a live encoder can share it.

For the candidate comparison, these costs scale with **aggregate useful
Mb/s**, rather than multiplying by the lane count. Two/four SPI receivers
with software wire CRC add approximately **0.497% of a core per Mb/s**
(`0.628*1332/1316` cycles/payload bit) to the common 0.4766% GCS term, before
their unimplemented reorder and close work. Four/eight-bit parallel receivers
share the same zero-copy/hardware-CRC per-bit cost and approximately
0.018–0.025% close cost per Mb/s estimated above; widening to eight bits saves
clock rate rather than CPU instructions. Hardware-master SPI likewise has
zero CPU bit-service cost with RX/TX DMA and the RX sniffer, plus the common
GCS term and per-message service. Its per-message driver is not built, so a
numeric SPI interrupt/credit cost is **unverified**, rather than borrowed
from the PIO handler. These figures exclude the benchmark-only pattern and
payload verification passes, which were budgeted separately above.

Placement is first-class. No hot routine was moved to SRAM. The new ISR,
reset and consumer run from flash because lower SRAM cannot hold another
kernel; estimates assume warm XIP and must be rejected if device timing
disagrees. Linker-map addresses and final image identities are recorded in
the verification section below. The command borrows 24*1332 = **31968 B** of
`in_buf`, with its remaining **800 B** guarded. The emulator borrows
**21312 B** of `cap_buf` (21248 B for short messages) plus a four-byte guard.
External mode leaves `cap_buf` unused. PRBS `in_buf` is restored on return.
The new permanent BSS is one pointer; programs, slot CRC state, pin snapshots
and totals are on the command stack. No buffers, tables or existing FEC code
were moved to make space.

The [B6 budget](b6-pio-producers.md) needs a distinction between active work
and reservations: unchanged `dual_dma.c` reserves the capture SM, program and
DMA even when capture is inactive. B6 actually claims **10 SM and 13 DMA**:
PIO0 has three producers plus clock (13 words), PIO1 one producer plus capture
(14 words), PIO2 four writers (8 words). Only two SMs, both in PIO1, remain.
An eventual external wide receiver moved to PIO1 would reach **12/12 SM,
14/16 DMA, 27/32 PIO1 words** with this hardened 13-word receiver. Its basic
9-word form would use 23 words but lacks the exact-length checks. The current
standalone command deliberately requires idle PIO2; this integration is
**budgeted, not implemented or measured**. Its self-test's third SM cannot fit
the unchanged B6 reservations, and 25 sender/receiver words plus the producer
would overflow 32 even if the capture reservation were removed.

At 47 Mb/s, adding the external FIFO reads to B6's documented CPU-polling
FASTPERI envelope gives **85.640% without active capture / 91.522% with
capture**, before the new IRQ, credit/sniffer control accesses, USB and
arbitration. The observed 14-cycle row loop has only two cycles before its
16-cycle deadline. Average bandwidth arithmetic cannot prove those deadlines
under ingress. The 24-message queue holds 252672 payload bits: approximately
8.15 ms at 31 Mb/s or 5.38 ms at 47 Mb/s, plus one admitted transfer. Linux
scheduling gaps and simultaneous FEC/ingress load still need a combined test.

## CM5 work and open questions

The CM5 sender must produce the same little-endian header and CRC32, transmit
each full message under one CS assertion, high nibble first at width4, keep
data stable across the rising-clock sample, leave clock low between messages,
and obey READY immediately before every message. Retain at least 10 us CS-high
gap. Buffer scheduling gaps upstream, log sent messages/bytes/CRC chains and
READY wait durations, and compare all of them with RP2350 counters. Start at
1 MHz, then sweep clocks after voltage and timing inspection. Do not enable
the internal sender while a CM5 drives these pins.

These remain **open questions**, not assumed CM5 features:

- Which RP1 SPI controllers are routed and independently available on this
  carrier, which modes/word sizes are usable with its Linux driver, and can
  two/four operate concurrently at the needed sustained rates?
- Can the CM5 act as an SPI slave with usable buffering/DMA and admission
  control, if RP2350 hardware SPI is the master? The proven CM5 master path
  does not answer that.
- Can CM5 DPI produce a deterministic nibble/byte bus and clock on available
  pins, with exact CS boundaries and a way to stop safely between messages
  for asynchronous READY? Are there unavoidable pixel/line/frame blanking
  intervals, byte ordering, clock minima, DMA or camera/display conflicts?
- If DPI cannot obey credits directly, what bridge/FIFO or different
  sender interface is required, with what pins, latency and power?

No RP1 controller count/mode availability or DPI-as-generic-bus capability is
claimed from the existing single-lane CM5 tests. No CM5 driver is delivered.

## Native verification and coordinator commands

[The native test](../../host/test_pvwide.py) extracts the actual C program
builder, reset, close, ISR, arm and consumer bodies and compiles them with
`pvproto.c` under `-Wall -Wextra -Werror`. Its independent PIO decoder checks
the actual eight RX, five gate and twelve sender opcodes for both widths,
half3..16, +/-one SYS cycle of data synchronization skew, partial words,
one-byte-short and one-beat-short transfers, and one extra edge. It also
checks 333 distinct short DMA counts, BUSY/AHB failures, terminal PC, delayed
retirement, late CS, CRC failure publication, pause/full-queue/duplicate
credits, ISR counter reload and clearing, byte patterns and accepted CRC chains
against the existing CM5 sender plus Python zlib. IRQ gate service delays span
0..1000 us. The actual release body is exercised for both resource layouts,
including clearing every DMA chain before cleanup and restoring PIO pin
latches while the restoring SM is still owned. These models do not represent
pad analog behavior or shared-bus
arbitration. The runner's offline self-test rejects corrupt counters, identities,
CRC chains, source/guard restoration, missing progress and failed cleanup.

Build and offline verification (no USB):

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
make check
```

Final offline verification: **`make build`, the specified experiment configure
and `make build BUILD=firmware/build-exp`, and all 101 Python invocations in
`make check` passed**. Neither build emitted a compiler warning. The wide test
passed 138 independent PIO wire cases plus the production C fault/credit/reset,
consume and release checks. `git diff --check` and relative Markdown links
also pass. The initial standalone configure lacked SDK environment variables;
the explicit exports above resolved it. No device result was manufactured or
added to `results/`.

Final GCC14.2.1 experiment map (hex addresses; source/code not relocated):

| Object | Address | Size / scope |
|---|---|---|
| `ring_buf` | `0x20000000` | 128 KiB, existing idle output arena |
| `in_buf` / receive slots | `0x20020000` | 32 KiB, first31968 B borrowed |
| `cap_buf` / emulator source | `0x20051fac` | 56 KiB, first21312 B borrowed |
| `w_active` | `0x2003fb98` | 4 B permanent BSS |
| `w_rx_reset` | `0x1002bdf8` | 180 B XIP code |
| `w_consume` | `0x1002c418` | 460 B XIP code |
| `w_cs_isr` | `0x1002c5e4` | 652 B XIP code, includes inlined close/rearm |
| `bench_pvwide` | `0x1002c870` | 3516 B XIP code |
| Existing `pv_crc32` | `0x20037f6c` | 168 B, existing striped-SRAM placement |

The command's own compiler stack allocation is **824 B** (788-byte local
frame plus nine pushed registers), including the 312-byte state. Callees,
exception frames, caller and print paths are additional; `stackcheck` remains
mandatory. Core0 stack is `0x2007f000..0x20080000`. Lower BSS ends at
`0x2003fbb8`; the reserved 1024-byte heap leaves **72 B** before `0x20040000`.
SRAM_HI ends at `0x2007efe8`, only **24 B** below the stack reservation. These
are linker capacities, not measured stack high-water marks. `state_addr`,
`slots_addr`, `sender_addr` and program offsets are also emitted at runtime.

The built runtime identity is `a04e6ba3a7-dirty`; hashes identify this uncommitted
image more precisely:

```
firmware/build-exp/iqbench.uf2
  fdb5373218c66b81fa069cf07dc101e0443ee0b9b5b7a53555ad47b401c402db
firmware/build-exp/iqbench.elf
  a8c7a62246e2609c129896efe6723335e021a60711623b5a0b340bd32bfea0fd
```

Rebuilds can change placement and hashes; archive the actual build used.

The following are **instructions for the coordinator, not commands executed
here**. Use the normal image-loading procedure for `firmware/build-exp/iqbench.uf2`,
unconnected external GPIOs for all emulator runs, an explicit CDC `PORT`, and
a fresh output directory for every command. The runner pins the image's exact
runtime identity and archives UF2/ELF/config/source hashes. No runner builds
or flashes. Its rates are accepted payload bits divided by elapsed microseconds,
including startup and bounded shutdown.

```sh
python3 host/iqbench.py --port "$PORT" raw 'clock 128000'
python3 host/iqbench.py --port "$PORT" raw info
python3 host/iqbench.py --port "$PORT" raw stackcheck

# Same-image width1/4 A/B, same pins and clock, all controls.
python3 host/pvwide_trial.py --port "$PORT" --build firmware/build-exp \
  --out results/ingress-wide/ab --ms 3000 --widths 1 4 --halves 5 \
  --faults 0 1 2 --reps 3

# Throughput sweep; do not apply a high threshold to the slow controls.
python3 host/pvwide_trial.py --port "$PORT" --build firmware/build-exp \
  --out results/ingress-wide/sweep --ms 3000 --widths 4 \
  --halves 16 10 8 6 5 4 3 --faults 0 --reps 3

# 60s QPSK9/10 transport target.
python3 host/pvwide_trial.py --port "$PORT" --build firmware/build-exp \
  --out results/ingress-wide/qpsk-60s --ms 60000 --widths 4 --halves 8 \
  --faults 0 --min-mbps 27.94 --reps 3

# 60s approximately31Mb/s payload target, then47Mb/s transport target.
python3 host/pvwide_trial.py --port "$PORT" --build firmware/build-exp \
  --out results/ingress-wide/payload31-60s --ms 60000 --widths 4 --halves 6 \
  --faults 0 --min-mbps 31 --reps 3
python3 host/pvwide_trial.py --port "$PORT" --build firmware/build-exp \
  --out results/ingress-wide/coded47-60s --ms 60000 --widths 4 --halves 5 \
  --faults 0 --min-mbps 47 --reps 3

# At the chosen rate, expose both negative controls.
python3 host/pvwide_trial.py --port "$PORT" --build firmware/build-exp \
  --out results/ingress-wide/negative --ms 3000 --widths 4 --halves 5 \
  --faults 1 2 --reps 3
python3 host/iqbench.py --port "$PORT" raw stackcheck
```

Manual equivalents: `pvwide 3000 1 5 0 1`, `pvwide 3000 4 5 0 1`,
`pvwide 60000 4 5 0 1`, `pvwide 3000 4 5 1 1`, `pvwide 3000 4 5 2 1`.
Optional before/after FEC regression controls, run separately rather than
concurrently: `c3pair 5 40 0 0 27`, `c3pair 5 40 0 2 27`,
`c3pair 5 40 0 2 31`, and `c3fec 5 3000 0 2 128 27`. These use the existing
bit-exact FEC runner/reference workflow; they do not measure FEC plus wide RX.

Positive pass criteria: `digital_ingress_clean`, nonzero progress, requested
duration/SYS/divider/width/identity, `started=closed=consumed=ok`, exact
`validated_bytes=ok*1316`, zero errors, shorts, longs, overflow, late close,
drain timeout, bad header/CRC/sync, lost or pattern mismatch, independent
CRC-field and full-payload chain agreement, and true guard/source/restoration
flags. The target run must also exceed its requested useful-rate threshold.
Record close/consume cycles and stack paint; no stack paint may be exhausted.

Fault1 requires `bad_crc=ceil(closed/16)`, accepted count `closed-bad_crc`, and
the independent accepted-only chains. Lost is counted only when a later
accepted sequence exposes a rejected message: the initial rejected zero and
a final un-followed zero do not count. The runner calculates that boundary
exactly. It requires zero framing errors. Fault2 requires zero closed/accepted
messages, every started transfer short, one drain timeout per short,
`errors=1` (SHORT only), zero payload/CRC chains, no other error, intact guards
and restoration. Both negative controls exit successfully only when detected.
Retain every failed positive or unexpected negative as a failure.

For a future external sender, `pvwide 60000 4 5 0 0` selects RX-only; half5 is
ignored. Compare `validated_bytes` rather than `ok*1316` when sending partial
TS containers. Use 65536 sequence numbers and sender totals/chain evidence.
Do not run the emulator-specific host validator on arbitrary external data.

## Limits, dead ends and intended commits

No new measured-throughput or error-free-duration claim is made. Internal
loopback is synchronous, lightly loaded and bypasses a real interconnect.
Even an eventual 60 s pass cannot prove external timing, Linux scheduling,
simultaneous FEC/row deadlines, RF quality, or a ground receiver decode. The
sixteen-message emulator cannot distinguish an entire lost sixteen-message
period if all receiver accounting also misses it; external sequence space,
sender totals and independent end-to-end evidence remain essential.

Rejected for this implementation: independent SPI lanes (reorder/CRC/SM cost),
an eight-bit bench bus (pins), hardware-master SPI (unverified CM5 slave and
APB cost), raising one-lane SCK beyond the external evidence, and shortening
the gap alone (still below target). The existing unframed four-bit `link.c`
demonstrates PIO packing, but lacks PV message CRC/boundary/admission semantics;
it is not evidence that real payloads survive this new framed bus. A more
compact receiver that checks only whole DMA words misses partial excess and
was deliberately replaced by the exact-beat terminal/extra-edge latch. New
SRAM hot-code placement was rejected because even a small leaf would consume
the remaining lower-SRAM margin. Future integration should measure warm/cold
XIP and contention before deciding which old idle-only buffers can hold code.

No commits were created. Intended small logical commits:

1. `firmware/src/pvwide.{c,h}`, `firmware/src/app/main.c`, `firmware/CMakeLists.txt`:
   **Add opt-in framed four-bit ingress and on-chip sender benchmark.**
2. `host/test_pvwide.py`, `host/pvwide_trial.py`, `Makefile`:
   **Verify wide ingress against CM5 vectors and add strict coordinator sweeps.**
3. `docs/rd/ingress-wide.md`:
   **Document wider-link tradeoffs, resource budgets and measurement gates.**
