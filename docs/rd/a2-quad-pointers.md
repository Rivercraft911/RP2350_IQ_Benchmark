**A2 delivers an opt-in four-writer live pointer experiment, `dualptr4`.** It fits
SYS128, N4, GPIO28 DDR, live GCS/TS ingress and the existing sparse capture in
**11 of 12 state machines and all 16 DMA channels**. Native checks establish
address/row ordering, bit-exact source data and cleanup. **A clean 60-second
16-Msym/s device run is still unmeasured.** The attached Pico was not accessed,
flashed or opened over serial. No Git metadata or existing result evidence was
changed; all implementation changes remain in this worktree.

The baseline evidence is [A1](../../docs/rd/a1-qpsk-hwshape.md) and its
[device results](../../results/dualptr/2026-10-15/README.md): two pointer-fed writers
passed 8 Msym/s at about 25.7% core-0 work, but stalled at 16 Msym/s under live
traffic. The [optimized four-writer descriptor control](../../results/optimizations/2026-10-15/README.md)
and [integrated DDR image](../../results/integration/2026-10-15/README.md) passed the
complete 16-Msym/s GCS and TS pipelines. Their success supports testing additional
writer FIFO runway; it does not establish this new DMA graph's throughput.

The implementation is in [dual_dma.c](../../firmware/src/dual_dma.c),
[bench_dual.c](../../firmware/src/bench_dual.c) and [main.c](../../firmware/src/main.c), with
interfaces in [dual_dma.h](../../firmware/src/dual_dma.h) and [bench.h](../../firmware/src/bench.h).
`dualptr` retains its two-writer selection. `dualptr4 cpw ms source fault` selects
four writers; source 5 is TS and 6 is GCS, cpw 2/1 gives 8/16 Msym/s at SYS128,
and fault 1 freezes publication credits. Both commands use the same normal 5/6
code, pilots, packed mapper, streaming LDPC packer, sliced frontend and L8 table.
There is no CPU sample arithmetic or descriptor construction. Default startup and
ordinary descriptor commands retain their behavior. The new command is available
without a CMake flag; the separate B1 option is still optional.

Each axis has a sole RX-DREQ control channel C, two sample channels E/O, and a
small routing channel R. The exact repeating sequence is:

```text
startup: C.WRITE_ADDR = &E.AL3_READ_ADDR_TRIG; start C only

C: pointer RX FIFO -> selected sample channel's AL3_READ_ADDR_TRIG, count 1
E: LUT -> even writer TX FIFO, count 8; CHAIN_TO R
R: { &O.AL3_READ_ADDR_TRIG, &E.AL3_READ_ADDR_TRIG } circular SRAM pair
   -> C.AL2_WRITE_ADDR_TRIG, count 1
C: next pointer -> O.AL3_READ_ADDR_TRIG
O: LUT -> odd writer TX FIFO, count 8; CHAIN_TO R
R: select E and restart C; repeat
```

R updates C's destination and starts C through the same alias write. C and R
chain to themselves, disabling an additional completion trigger. Only C uses
the L8 byte swap. R is FORCE-paced, reads an aligned 8-byte ring, writes a fixed
register address and does not byte-swap. Each sample channel retains its own
writer TX DREQ. There is one active sample transfer per axis; the four writer
FIFOs retain separate even/odd rows and provide the additional runway.

This choice avoids two channels draining a shared RX FIFO. The locally checked
RP2350 datasheet explicitly prohibits multiple channels connected to the same
DREQ: each channel has its own request-credit counter (§12.6.4.2, printed
pp1101–1102). Merely alternating active channels does not establish that their
accumulated credits are valid. Here only C observes that RX DREQ for its entire
lifetime. R never reads the pointer FIFO.

The ordering argument uses the checked alias map (Table 1145, p1099), transfer
count reload on each trigger (§12.6.2.2, p1097), and ordered DMA completion and
chaining (§12.6, p1096; §12.6.3.2, p1100). C's trigger write completes before the
eight-write sample sequence can complete; the sample's final FIFO write completes
before R starts. R's write chooses exactly the opposite sample channel before
the next C can issue. Thus pointer row k goes to writer parity k mod 2, neither
channel's read address is changed while it is active, and no trigger relies on
a zero transfer count. Startup starts the two C channels, leaving both routers
idle. Starting a router at startup would select the wrong first owner.

The existing prefill waits for all four joined TX FIFOs to fill, then explicitly
loads all four OSRs before the atomic output/clock enable. Pointers run on PIO0;
writers run on PIO2. The PIO1 cross-block start mask enables/restarts only the
writers and DDR clock, leaving pointer SMs running. The clock and capture retain
the original [phase and stride validation](../../docs/dac-clock.md). All eight output
channels, including routers, enter the existing sticky fault mask and shutdown
loop, which clears EN and chains before aborting. Output and clock stop before
pointer resources are released. The four-writer command requires all DMA channels
and the relevant PIO blocks to be idle before starting.

The original A1 source ownership protocol is unchanged: core 1 alone maps into
one continuous packed symbol stream; core 0 publishes only its completed prefix.
Whole-frame reservations use the guarded minimum of both axis input cursors.
The zero-sentinel credit ring grants complete 512-word batches. Eight source
words remain guarded against in-flight retirement; the new router does not alter
the input DMA or its prefetch depth. Polls remain every 128 us, reject gaps above
256 us, and require at least 1360 granted words of runway at SYS128. These are
source-read bounds, not physical output cursors. Frame tails, FIR history and
credits do not reset at PLFRAME boundaries. The capture reference search retains
its finite preceding-frame window and checks every supplied observation.

The resource budget includes the on-chip SPI master and retained capture:

| Block / service | SMs | PIO instruction words | DMA channels |
|---|---:|---:|---:|
| PIO0: existing L8 pointer producers | 2 | 13 shared | 4: input + credit per axis |
| PIO0: GPIO28 DDR clock | 1 | 2 | 0 |
| PIO1: PV-SPI RX, CS/READY, emulator | 3 | 19 | 3 |
| PIO1: sparse capture | 1 | 3 | 1 |
| PIO2: even/odd I/Q writers | 4 | 12: two shared six-word programs | 8: C/R/E/O per axis |
| Total | **11 / 12** | **PIO0 15, PIO1 22, PIO2 12; each ≤32** | **16 / 16** |

There is no spare DMA channel. No SPI, capture or clock reduction is needed for
this design. The source pointer program remains 49 non-stalled PIO cycles per
16-symbol input word. At 16 Msym/s that word lasts 128 SYS cycles, so its nominal
instruction occupancy remains 38.3%, before RX backpressure.

At SYS128/divider 1, the bus sends 128 million 16-bit words/s, each axis receives
64 MS/s, and GPIO28 runs at 64 MHz. N4 therefore gives 16 Msym/s. The 65-MSPS
AFE limit gives 16.25 Msym/s at N4; 64/65 = 98.46% utilization. Divider 2 halves
these rates. No N2 or fractional-divider mode is introduced.

There is a units correction to the task's motivating deadline: an eight-word
L8 row contains sixteen axis samples, or four N4 symbols. At 16 Msym/s it occupies
250 ns = **32 SYS cycles**, rather than 16. The programmed four-writer layout
gives each writer 32 active and 32 silent bus slots, returning every 64 cycles.
With ideal immediate FIFO service, the next mandatory OUT allows approximately
68 SYS cycles after the opposite writer frees the final FIFO slot needed to
complete the prior sample DMA, versus approximately 36 for two writers. For
example, two-writer row1 can finish filling near slot26 and needs row2's first
word before OUT64; the serialized four-writer row3 can finish near slot58 and
needs row4's first word before OUT128. Boundary conventions affect these
approximate windows by one or two cycles. The **additional 32-cycle silent
interval is real runway**: a non-OUT instruction does not stall when an empty
OSR cannot autopull (§11.5.4.2, p910, checked locally). The FIFO need only supply
that first word before the next mandatory OUT; empty-OSR pull and OUT cannot
occur on the same cycle. R adds one read/write transfer and another chain
transition, and bus arbitration can spend the allowance.
Neither average bandwidth nor this timing model bounds worst-case hardware
latency under SPI/FEC/USB traffic.

The opcode model compares one isolated completion blackout after row32, using
the same simulated output-DMA arbiter and nominal surrounding handoffs. Two
writers pass through 35 simulated SYS slots of blackout and fail at 36; the
serialized four-writer graph passes through 64 and fails at 65. These are
**simulation thresholds**, not RP2350 DMA timings or a maximum tolerated live
bus delay. The comparison includes the extra router transfer. Explicit model
checks cover nonblocking empty NOPs/silent delay cycles, the empty-OSR OUT fence,
and concurrent refill on the final nonempty OUT.

The corresponding sustained-delay control is less favorable: imposing the
same completion delay after **every** row passes through 16 simulated slots
for two writers and 14 for this four-writer graph; the next value stalls each.
Those thresholds are specific to this event scheduler. They show the tradeoff:
four FIFOs absorb a longer isolated blackout, while the router adds recurring
work. If A1's live failure reflects a sustained service deficit rather than a
burst, this design may still fail. The device trials must distinguish these
cases; the positive blackout comparison does not settle them.

The bandwidth arithmetic at 16 Msym/s is:

| Traffic | DMA transfers/s | Shared FASTPERI accesses/s |
|---|---:|---:|
| Sample SRAM -> writer FIFOs | 64 M | 64 M |
| Pointer FIFO -> sample trigger (C) | 8 M | 16 M |
| Selector SRAM -> C destination trigger (R) | 8 M | 8 M |
| Packed 16-bit source -> pointer TX FIFOs | 2 M | 2 M |
| SPI RX + emulated TX, approximate | 1.64 M | 1.64 M |
| Source credits | 0.003906 M | 0.003906 M |
| Total, approximate | **83.64 M** | **91.64 M** |
| Additional pins32 capture | **7.529 M** | **7.529 M** |

The ideal DMA read/write-port utilization is 65.3%, rising to 71.2% during
capture. The stronger shared FASTPERI budget is 71.6%, rising to 77.5%. PIO and
DMA registers share that port (§2.1, p24), whose zero-wait subordinate limit is
one access/SYS cycle (§2.1.1, p25), checked in the local datasheet. Pointer
transfers count twice there because both endpoints are peripherals. Router
traffic adds 32 MB/s of SRAM reads and 32 MB/s of DMA-register writes; sample
table reads and FIFO writes are each 256 MB/s. A 4096-word capture lasts about
544 us at divider 1. Rates approximately halve at divider 2. These optimistic
averages exclude CPU polls, USB and arbitration penalties; they establish room
in the average budget, not a clean device result.

Only **16 persistent bytes** are added: the two aligned selector pairs at the
end of the output engine state. Existing field offsets are preserved. The table
still borrows 64 KiB within `ring_buf`; source, BB/FE and capture arenas are reused.
No additional LUT, source ring or sample ring is allocated. The default iqbench
link map places `in_buf` at 0x20000000, `ring_buf` at 0x2001f8e8, the selected LUT
at 0x20020000, `bb_buf` at 0x20050000 and `cap_buf` at 0x20051fac. The capture
borrows 0x20059fac..0x2005dfac; the following 8108 bytes hold the second FE slot.
The SRAM0–3 remainder after the reserved 1 KiB heap is 664 B for iqbench and
120 B for pvflight. SRAM4–7 has 4464 B free; scratch X/Y retain 16/304 B before
their reserved 2 KiB stacks. The new orchestration's stack frame is 704 B
including saved registers; nested calls and interrupts need additional space.

Both B1-option targets also link: iqbench has 568 B and pvflight only **24 B**
after the minimum heap. Use the default `firmware/build/iqbench.uf2` for A2 trials.
Placement remains a performance risk: selector reads share low SRAM with other
traffic, and the enlarged BSS moves later objects. An object comparison against
HEAD using the same GCC flags found all five `dual_dma.c` time-critical sections
identical (1256 instruction/data bytes before relocation). This checks the
unchanged hot instruction streams; it does not claim a cycle-neutral full image
or eliminate SRAM/bus contention.

The router adds no M33 work while running. Using the M33 timing rules in
[rp2350-notes.md](../../docs/rp2350-notes.md), the unchanged healthy pointer supervision
and output-fault leaves plus outer bookkeeping retain A1's approximate
**400–650 cycles/poll**, or 2.4–4.0% of core 0 at 128-us polls, excluding cache
misses, IRQs and contention. This is an instruction-based estimate, not native
timing or a new M33 measurement. Scaling the measured 8-Msym/s 22.5% BCH work
to 16 Msym/s suggests roughly **48–50% core-0 work** including supervision;
core 1 may remain near the measured 82% GCS / 94% TS loads. Arbitration and
placement can change all three. The runner records supervision and frontend
work separately; its pointer metric is not the descriptor control's broader
active-loop metric. Waiting cores spin; no sleep or power result is claimed.

Native verification uses the real portable C FEC and independent Python
references, without opening USB:

- [test_dual_fifo.py](../../host/test_dual_fifo.py) compiles the actual DMA setup and
  stop leaves with register shims and `-Wall -Wextra -Werror`. It checks selector
  alignment/targets, alias writes, exact counts, BSWAP, priorities, fixed writer
  DREQs, sole RX-DREQ ownership, two/four-writer start validation, existing
  descriptor configurations, and wrapped descriptor cursors. Stop cases include
  routers, AHB faults, incomplete captures and preservation of external claims.
- [test_quad_fifo_order.py](../../host/test_quad_fifo_order.py) models the production
  pointer/writer opcodes and routed DMA ordering with FIFO backpressure and
  delayed handoffs. Twelve tests compare exact L8 FIR samples, cover divider 1/2,
  independently skewed axes, 8704 source words crossing the actual 8192-word
  ring, selector-ring wraps, finite credits and stop at each handoff phase.
  Negative controls reject competing readers, wrong routing, missing BSWAP and
  excessive latency. The model is event/instruction evidence, not a model of
  the RP2350 bus arbiter.
- [test_dualptr_native.py](../../host/test_dualptr_native.py) exercises actual prefill,
  BB queue and frontend leaves with real C FEC: 33,282 complete packed words
  through sixteen TS/GCS frames match independent Python, crossing four source
  ring lengths and every pilot-frame carry phase. Frozen-credit controls stop
  at exactly 14,336 authorized words. These input/credit leaves are unchanged.
- [test_dualptr_trial.py](../../host/test_dualptr_trial.py) checks both sources/rates,
  both writer selections, all 4096 independent observations, correct/inverted
  clock and finite-credit controls, corruption with a recomputed serial CRC,
  firmware/topology identity, SPI provenance, cleanup and mocked CLI dispatch.
  Existing pointer tests exhaust the L8 address/history transform and table;
  existing DAC-clock tests cover the four-writer opcode/clock phase relationship.

Reproduction, completed here without hardware:

```sh
make build
make check

# Compatibility build for the already-merged B1 opt-in configuration.
PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0" \
PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1" \
cmake -S firmware -B firmware/build-b1 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON
make build BUILD=firmware/build-b1
```

Both builds completed for iqbench and pvflight without compiler warnings.
`make check` passed all 74 Python command invocations, including the twelve new
ordering tests and twelve runner tests. `git diff --check` and relative Markdown
links passed. Some existing result self-tests report absent gitignored firmware
images; those historical binary hashes were not rechecked. No new device run
was substituted for that missing historical evidence.

For the coordinating engineer, the A2 build directory is `firmware/build` and
the image is `firmware/build/iqbench.uf2`, whose absolute path is
`/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.claude/worktrees/codex-a2-quad-pointers/firmware/build/iqbench.uf2`.
Load that image using your normal procedure. The runners do not build or flash.
Use the revision embedded in the loaded image; a dirty revision alone cannot
distinguish edits, so retain the runner's UF2/ELF and source hashes. Every output
directory below must be fresh.

This build reports `dfb34c58ed-dirty`. Its UF2 SHA256 is
`cfc51ddb374f1687c9938bfabf0d673f93ee2312ab5c2920f0c48efabf223aa5`;
ELF SHA256 is
`5d6144e1bc040fe0e67a7b072ca690ae5adc49dba6548c66f86714ae933373d6`.
Rebuilding after source or commit changes can change these identities.

```sh
make build
make check
A2_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build/git_rev.h)

# First: 3 s at both 8 and 16 Msym/s, both inputs, same-rate descriptor controls.
python3 host/dualptr_trial.py --writers 4 --ms 3000 --clocks 128000 \
  --cpws 2 1 --sources 6 5 --faults 0 --clock-modes 1 --control \
  --expected-git "$A2_FW_GIT" --build firmware/build \
  --out results/dualptr/a2-short

# Then: separate inverted-clock and frozen-credit controls at both rates/inputs.
# fault1 + inverted clock is deliberately skipped so causes are not combined.
python3 host/dualptr_trial.py --writers 4 --ms 3000 --clocks 128000 \
  --cpws 2 1 --sources 6 5 --faults 0 1 --clock-modes 1 2 \
  --expected-git "$A2_FW_GIT" --build firmware/build \
  --out results/dualptr/a2-negative

# Only after the short runs pass: 60 s target GCS and its four-writer control.
python3 host/dualptr_trial.py --writers 4 --ms 60000 --clocks 128000 \
  --cpws 1 --sources 6 --faults 0 --clock-modes 1 --control \
  --expected-git "$A2_FW_GIT" --build firmware/build \
  --out results/dualptr/a2-gcs-60s

# Same 16-Msym/s target and control with TS ingress.
python3 host/dualptr_trial.py --writers 4 --ms 60000 --clocks 128000 \
  --cpws 1 --sources 5 --faults 0 --clock-modes 1 --control \
  --expected-git "$A2_FW_GIT" --build firmware/build \
  --out results/dualptr/a2-ts-60s

# Optional A1 regression at the measured clean 8-Msym/s point.
python3 host/dualptr_trial.py --writers 2 --ms 3000 --clocks 128000 \
  --cpws 2 --sources 6 5 --faults 0 --clock-modes 1 --control \
  --expected-git "$A2_FW_GIT" --build firmware/build \
  --out results/dualptr/a2-a1-control
```

For manual command access, use `python3 host/iqbench.py raw '<line>'` for each
selector: `clock 128000`, `dualprio 0`, `dualwriters 4`, `quadprofile 1`,
`bchkernel 2`, `mapkernel 1`, `ldpcpack 1`, `bchbank 1`, `dualclock 1`.
The positive lines are `dualptr4 2 3000 6 0` and `dualptr4 1 3000 6 0`, then
`dualptr4 1 60000 6 0` / `dualptr4 1 60000 5 0`. The descriptor control is
`dualstream 1 60000 2 6 4096 0 0 0 4 8` (source 5 for TS). Prefer the runner,
which retains captures, validates them, and restores `dualclock 0` and SYS128.

Expected positive metadata includes `cmd=dualptr4`, `writers=4`,
`pointer_pio=0`, `pointer_routing=alternating_dma`, `dac_clock_pio=0`,
`sym_rate=16000000.0` at cpw1, `table_crc=3433922394`, and zero pointer/output
errors or TX stalls. Pass requires `classification=digital_live_clean`, the full
requested duration, advancing runtime FEC/BB/SPI counters, matching independent
BBFRAME CRCs and accepted-message CRC chain, all 4096 bus observations matching
one fixed independent waveform offset, and zero GPIO28/IQ_FLAG mismatches with
both phases present. Credit runway must remain ≥1360 words, poll gap ≤256 us,
neither input reader may exceed grants, and selectors/buffers must restore.
Controls must classify `cpu_descriptor_control_clean`. CPU reduction is an
outcome to record, never a substitute for these checks.

`dualclock 2` with fault0 must retain correct data and fail parity in all 4096
snapshots: `inverted_clock_detected`. `dualclock 1` with fault1 must freeze grants
one quarter through the requested duration and stop early on SOURCE_EMPTY with
a terminal null-trigger IRQ, without reading beyond the frozen grant:
`credit_starvation_detected`. Terminal owned TX stalls are permitted for that
control; unrelated DMA/ownership/configuration/poll faults fail it. The runner
returns nonzero for any unexpected failure and retains failed records.

Several approaches were rejected. Four independent alternate-row pointer SMs,
each computing the full key stream, would require 13 SMs and 20 DMAs with the
existing input/credit scheme. Two alternately active RX consumers violate the
documented DREQ rule. A plain DMA write ring cannot jump between data-channel
trigger registers spaced 64 bytes apart: supported 32-bit increments do not
provide that stride. An eight-symbol full-history row-pair table needs
2^(7+8) ×16×4 = 2 MiB. A single data channel alternating writer destinations
also needs its TX DREQ changed per row. The router solution avoids those changes
with 16 bytes of state, at the cost of one extra DMA transfer per axis row.

The principal open question is latency under live bus traffic, including the
short capture interval. All 16 DMA channels are occupied. Native models do not
establish a maximum FASTPERI arbitration delay or 60-second device reliability.
If only A2 stalls while the descriptor control passes, retain both records and
compare with the unchanged A1 point; do not relax the capture/credit checks.
Increasing SYS alone cannot preserve the adopted integer-divider 16-Msym/s
N4 bus at SYS150. Reducing SPI SM/channel use is unnecessary for the present
fit, but could free a DMA channel for a future hardware scheduling alternative.
The existing output backend still has an unbounded DMA-abort wait; pointer
abort retains A1's bounded stop and claims on failure. Clock-driver permission,
AFE setup/hold at the actual pins, RF spectrum/EVM, reconstruction images and
receiver acquisition remain outside this digital experiment, as in the
existing clock report. No new RF qualification is claimed.

Intended small logical commits, for the coordinating engineer to create:

1. `firmware/src/dual_dma.c`, `firmware/src/dual_dma.h`,
   `host/test_dual_fifo.py`, `host/test_quad_fifo_order.py`, `Makefile`:
   **Route sole-RX pointer DMA alternately to four QPSK writers.**
2. `firmware/src/bench_dual.c`, `firmware/src/bench.h`, `firmware/src/main.c`,
   `host/dualptr_trial.py`, `host/test_dualptr_trial.py`:
   **Add opt-in live dualptr4 command and strict trial validation.**
3. `REPORT.md`: **Document A2 budgets, native evidence and coordinator trials.**
