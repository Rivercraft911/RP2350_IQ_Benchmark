# B6: four alternate-row PIO producers

**Both stages are implemented and pass native verification. Sustained device
operation is unmeasured.** `bench4prod8` supplies immutable cyclic codes;
`bench4prod8live` generates new packed 8PSK codes at runtime and grants finite
input DMA batches. Both feed B5's four OUT8 writers through four independent
PIO-RX control/sample pairs. No CPU constructs a row address or sample at runtime.
The live experiment reserves core 1 for input service; it is not a complete
FEC/ingress transmitter or a qualified 16-Msym/s result.

Branch `rd/b6-pio-producers` is based on B5 `5d7ddab`. B5's SRAM-pointer commands
and ordinary startup remain available. Everything new is opt-in under
`IQ4HIST_EXPERIMENT=ON`, in `iqbench` only. No Pico, serial port, flash operation,
clock/VREG/QMI change, Git metadata write, or existing result evidence was used
or modified. The coordinator owns all device measurements.

## Implementation and startup proof

The portable [program](../../firmware/src/iq4prod.h) has exactly eleven words:

```text
0  MOV ISR,X           7  MOV ISR,X
1  OUT X,2             8  OUT X,2
2  IN X,2              9  IN X,2
3  MOV X,ISR          10  MOV X,ISR; wrap to0
4  MOV ISR,Y
5  IN X,14
6  IN NULL,2; autopush16
```

IN shifts left, OUT right, autopull threshold32, autopush threshold16; FIFOs
remain unjoined (four words each way). X retains newest-low-two-bit history;
Y is the 64-KiB-aligned table base shifted right16. The output path updates
history and emits `base | ((history & 16383)<<2)`. The history-only path updates
the same history and never pushes. `MOV ISR` resets the IN count before both
constructions: the history-only IN reaches2, whereas the address INs reach16.
Higher obsolete history bits in X are harmless; only the low14 enter an address.

Streams are **I-even, Q-even, I-odd, Q-odd**. Even SMs enter at0; odd SMs enter
at7, consuming symbol0 without output before emitting symbol1. The first address
therefore appears after7/11 non-stalled PIO cycles respectively; subsequent
addresses appear every11 cycles, each representing the next same-parity symbol.
Each SM reads its axis's **complete** code stream, including the other parity.
Decimating the input would lose the six-symbol FIR history.

`iq4prod_seed(previous_word)` reverses the last six chronological two-bit codes
into X's newest-low ordering. Cyclic startup uses each axis ring's final word;
live startup deliberately uses six zero codes on both axes. Initialization
disables autopull/autopush while injecting base and history, empties OSR with
OUT NULL32, enables the shift features, starts input DMA, fills all TX FIFOs,
then executes an explicit PULL fence before enabling the producers. No source
word is skipped by a late opportunistic autopull.

Every code acquisition is OUT X2. At a source-word seam an empty OSR stalls
before changing X; it cannot substitute stale OSR contents. Non-OUT cycles can
refill OSR, and a final nonempty OUT can refill concurrently. A full RX FIFO
stalls the final address IN until its autopush completes once. These rules are
checked against local RP2350 build2025-07-29 **§11.5.4/§11.5.4.1 printed p909 and
§11.5.4.2 p910**, including MOV ISR count reset and PULL's autopull fence.
There is no MOV/IN OSR key extraction and no explicit nonblocking PULL.

[The device module](../../firmware/src/iq4prod_dma.c) claims four TX-paced input
DMAs and four producers. [The sibling output entry](../../firmware/src/dual_dma.c)
uses each producer's own RX DREQ to move one numerical address directly to its
own sample channel's `AL3_READ_ADDR_TRIG`. That sample channel sends one word
to its own writer TX FIFO and chains back to its control channel. All four
controls start; there is no router, BSWAP, shared RX reader, or CPU descriptor.
The existing B5 writer entry points, X seeds, four-byte sample packing, IQ_FLAG,
prefill, explicit OSR loads and atomic GPIO28/writer start/stop are reused.

## Stage 2 ownership and source

Live mode uses **four finite normal-mode input DMAs**, each reading512 words
per grant. RP2350 **§12.6.2.2 p1097 and §12.6.2.2.1 p1098** specify that normal
mode halts after count0 and all in-flight transfers finish. No input channel
chains or retriggers itself. Thus an arbitrary service interruption can stop
the stream, but cannot read beyond its finite grant or silently replay a ring.

Ownership is expressed in absolute paired-axis code-word counts:

1. Initially core 0 fills4096 words/axis. Every store precedes publication.
2. Core 1 alone observes each input DMA BUSY and rearms it. After BUSY clears,
   that channel's completed count advances to its prior grant end. All source
   reads in that chunk have retired, including the final write to the PIO FIFO.
3. A channel receives another512-word grant only when its new grant end is no
   greater than the published paired-axis prefix. A DMB follows the worker's
   publication read; core 0 uses a DMB before releasing that prefix.
4. `reusable_words = min(completed[0..3])`. Core 0 writes the next512-word chunk
   only when `published - reusable <= 4096-512`. All four readers have retired
   the old contents of those physical slots. It writes both axes completely,
   then publishes once. DMA address rings wrap16KiB; PIO history never resets.

The worker exports snapshots through an odd/even sequence counter with barriers;
64-bit statistics cannot tear. Live `input_issue_words` is retained as a runner
field name but means **completed batch words**, as explicitly identified by
`cursor_kind="completed_batch_words"`; it is not a physical-output cursor or an
exact issue count. `granted_words[4]` bounds every permitted input read, including
in-flight reads. There is no extra speculative-read guard after BUSY retirement.
Monotonic publication, capacity, and uint32 absolute-total limits are checked.
The current command's60-second limit is well below that total limit.

Core 1 runs the existing `tx_worker_start` task interface, with its interrupts
disabled only during this task, and rejoins the original task loop on stop.
It polls no faster than once per64 SYS cycles, checking four input channels;
every32 rounds it checks both producer FDEBUG registers. Core 0 checks faults
and publication every128us and rejects supervision gaps above256us. A producer
TXSTALL is sticky **SOURCE_EMPTY**, whether caused by unavailable publication or
a late finite-grant rearm. RXSTALL is ordinary backpressure. Publication failure,
DMA errors and illegal FIFO access have separate errors.

**Memory safety does not depend on a software timing promise.** Finite grants
provide it. Seamless grant renewal does depend on real arbitration/FIFO runway;
there is no proved worst-case service bound. The worker reports maximum observed
round gap in SYS cycles for the coordinator. Core 0 stops writers and GPIO28
before joining/stopping producers and aborting their DMAs. Producer disable uses
atomic CTRL clears, so a concurrent worker fault cannot restore a stopped clock.
Worker join and producer DMA abort have1ms timeout paths that retain claims and
prevent buffer restoration. B5's output abort loop remains an inherited unbounded
loop. A failed release is a failed run, never permission to reuse source storage.

[The runtime source](../../firmware/src/iq4prod_source.c) works a whole packed
16-symbol group at a time. Let `xorshift32` use shifts13,17,5, starting from
`0x1234abcd`. Each group takes two successive words `i` and `r`, then computes:

```text
q = (r & 0x55555555) | ((~(i ^ (i>>1) ^ r) & 0x55555555)<<1)
```

For code `a|b<<1`, the B1 alphabet is `[+C,-S,+S,-C]`. Q's bits obey
`d=~(a^b^c)`, so exactly one axis has magnitude C and the other S, with independent
sign choices: all eight valid rotated 8PSK points, no invalid diagonal points.
The group stream has PRBS period2^32-1 groups (about71.6minutes at16Msym/s),
rather than repeating a prebuilt4096-word ring. It is a deterministic component
test source, not coded DVB-S2 or an iid-spectrum certificate.

The replacement interface is a contiguous, complete paired-axis packed-word
chunk followed by `iq4prod_dma_publish(total_words)`. FEC/ingress can fill the
same owned slots, preserving partial groups and continuous FIR history. This
change does not implement their mapping, admission or scheduling. Reserving
core 1 for this service means it cannot simultaneously run today's core-1 FEC
task. A future complete transmitter needs a different scheduling/resource proof.

## Budgets

Let `d=cpw`. One N4 symbol takes `8d` SYS cycles; `Rs=SYS/(8d)`, per-axis sample
rate is `SYS/(2d)`, and GPIO28 has that same clock frequency. At SYS128,d1 this
is16Msym/s and64MSPS/axis; d2 gives8Msym/s and32MSPS. Each alternate writer gets
**16d SYS cycles per one-symbol row**. Each producer needs88 instructions per
16 symbols:68.75% of its128-cycle budget at d1,34.375% at d2. These are exact
instruction ratios, not measured PIO/DMA throughput. Producers use divider1.
The command also admits SYS256 with legal d, rejecting per-axis rates above
65MSPS. No code here changes the system clock or `bench4fec.c`'s SYS check.

The local datasheet **§2.1 p24 / §2.1.1 p25** puts all three PIO blocks, USB and
DMA registers behind one shared FASTPERI port, ideally one access/SYS cycle.
**§12.6 p1095** gives DMA's collective one-read/one-write per-cycle ceiling.
Count a DMA transfer as one read/write pair, but a PIO RX→DMA-register pointer
as **two FASTPERI accesses**. Per symbol, both axes combined:

| Work | DMA transfers | FASTPERI accesses |
|---|---:|---:|
| Two one-word sample rows, SRAM→writer | 2 | 2 |
| Two emitted addresses, producer RX→sample trigger | 2 | 4 |
| Four full code streams, one word/16 symbols each | 0.25 | 0.25 |
| Cyclic total | **4.25** | **6.25** |
| Additional stride17 pins32 capture | 8/17 | 8/17 |

At d1: DMA53.125% /59.007% capture off/on; FASTPERI78.125% /84.007%.
At d2 these graph percentages halve. At SYS128,d1 there are68M DMA transfers/s
and100M FASTPERI accesses/s; capture raises these to75.529M and107.529M/s.
Capture adds7.529M SRAM writes/s,30.118MB/s, for about544us per4096 observations.
Sample/source SRAM reads are9bytes/symbol =144MB/s at16Msym/s (128MB/s samples,
16MB/s duplicated packed input). This is a global count, not a per-bank guarantee.

Live CPU service adds port traffic beyond that graph. Its requested upper bound
is4/64 =6.25 percentage points for BUSY polls, plus2/(32*64)=0.097656 points
for FDEBUG. Four rearm writes per8192 symbols add0.006104 points at d1. Thus a
conservative live port envelope is **84.479% without /90.361% with capture**,
before core-0 polling, USB and arbitration. The worker actually takes more than64
cycles on many rounds, reducing its request rate; this does not prove latency.
Its fixed-cycle polling contribution does not halve at d2. Rearm writes are CPU
port accesses, not additional DMA transfers or DMA channels.

Fourteen-bit rows would require8.25 FASTPERI accesses per symbol,103.125% at d1
before capture or software. They are excluded. A router would also erase this
design's port margin. Higher priority cannot increase physical port capacity.
This corrects the older frontier's second-chain projection: counting DMA
transfers alone omits the second peripheral access of every PIO pointer.

| PIO block | SM allocation | Shared instruction words |
|---|---|---:|
| PIO0 | I-even,Q-even,I-odd + DDR clock | 11+2 =13/32 |
| PIO1 | Q-odd + sparse capture | 11+3 =14/32 |
| PIO2 | four B5 OUT8 writers | 4+4 =8/32 |
| Total | **10/12 SMs** | each block fits |

The program is shared within each occupied block; there are two installed
11-word copies. Four input + four control + four sample + one capture =
**13/16 DMA channels**, for cyclic and live. At idle standard claims, input
DREQs are0,1,2,8; producer RX DREQs4,5,6,12; writer TX DREQs16..19; capture RX
DREQ13. Every DREQ has exactly one DMA subscriber, as required by local
**§12.6.4.2 pp1101–1102**. Actual SM claims are exported and checked; GPIO base
and instruction-space constraints are checked by the respective backends.
Adding one credit channel/producer would need17 DMA channels. Choosing a single
producer as a shared reload trigger has no legal four-way completion barrier;
independent backpressure can change which input completes last. Finite software
rearm avoids that assumption and consumes no grant channels. Today's proposed
three-SM SPI source would make13SM total with capture, so it does not fit this
unchanged layout even though its three DMA channels would reach16.

## SRAM and CPU accounting

No new table, source ring or capture array is allocated. The64KiB byte LUT borrows
the first half of `ring_buf`; the remaining64KiB stays reserved for compatibility.
The two4096-word source rings share storage between their parity readers:
32KiB total, encoding65536 chronological symbols/axis. The16KiB capture borrows
`cap_buf`. Principal linked addresses in `firmware/build-b6/iqbench.elf.map`:

| Storage | Address / size |
|---|---|
| Existing output arena / byte table | 0x20000000 /131072B reserved,65536B used |
| I / Q code rings | 0x20020000 /0x20024000,16384B each |
| Existing link ring | 0x20028000,32768B |
| Capture | 0x20051fac,16384B borrowed |
| New runtime source hot function | lower SRAM,76B |
| New core-1 worker / state | SRAM9,1156B /312B plus16B linker veneers |
| Complete SRAM9 usage | 3424B,672B free |
| SRAM8 code / core-1 stack | 2032B /2048B,16B free |
| SRAM4–7 data / core-0 stack | 258016B /4096B,32B free |
| Lower SRAM after reserved1KiB heap | 208B free |

The initial lower-SRAM placement did not link (1320B overflow). Placing the new
worker and state in SRAM9 uses space freed by the existing core-0 stack move,
without borrowing another runtime arena or changing old object placement rules.
The worker fetches and reads its state from the same bank; that contention is a
performance variable. New code/data and ordinary firmware placement can still
change arbitration. Only the linker map establishes capacity, not throughput.

GCC14.2.1's source loop is **17 Thumb instructions per16 symbols**, including
two stores and the loop branch; no per-symbol loop, lookup or division. The M33
timing rules in [rp2350-notes.md](../rp2350-notes.md) give an estimate of about
**24 cycles/group =1.5cycles/symbol**, including taken-branch and complex-shift
use penalties but excluding setup, IRQs and contention. This is about18.75% of
one core at d1,9.375% at d2. A512-word fill estimates12288cycles,96us at SYS128.
The firmware measures runtime generation cycles separately from supervision;
prefill/table construction are outside the timed interval. Host runtime is not
an M33 timing measurement.

The ordinary all-BUSY worker path has about71 Thumb instruction slots and five
taken branches, estimating80–100cycles before bank/port stalls. Four simultaneous
rearms, faults and snapshot publication take longer. Core 1 remains reserved and
spins throughout: report **100% core reservation**, never infer spare FEC budget
from `worker_service_cycles`. That counter excludes snapshot publication and
spin, whereas `worker_max_gap_cycles` includes elapsed round-to-round time.
The reserved core therefore costs8d SYS cycles per transmitted symbol, including
spin, in addition to the estimated1.5 core-0 generation cycles/symbol and
supervision. Core-0 supervision and all device timings remain measurement gates.

## Native verification

All tests run without opening USB. `cc -Wall -Wextra -Werror` builds the new
portable/native bridges. Native results establish representation, policy and
orchestration; their simulated clocks and DMA are not RP2350 arbitration models.
Both `make build` and `make build BUILD=firmware/build-b6` completed without
compiler warnings, and the complete `make check` passed with exit0.
`git diff --check` and relative Markdown link validation passed. The inherited
waveform plot self-test notes four absent gitignored historical firmware images;
their historical binary hashes were not rechecked.

- `test_iq4prod.py` executes the actual eleven opcodes:262144 key/base/parity
  cases,12288 seed cases,240 random streams, separate source/RX delays,
  terminal empty-source fences, five cyclic laps,1856 disabled-state cases,
  and first-row timing7/11. Streams of1025 words cross finite-grant seams
  without resetting history.393216 writer/clock slots match an independent
  scalar FIR, including even/odd row order, IQ_FLAG, low bits and GPIO28.
- The same test extracts the actual input worker, live progress and publish
  leaves against finite-DMA mocks: five ring laps with independently delayed
  readers, epoch checks against unpublished reads/early overwrite, terminal
  starvation and16394 publication cases. A late service never grants stale data.
- `test_iq4prod_source.py` checks94277 packed groups /1508432 symbols against
  scalar valid constellation pairs, arbitrary publication partitions and guards.
- `test_dual_fifo.py` executes real setup/dispatch leaves, all four start bits,
  independent DREQ checks and68 invalid-source/configuration cases, alongside
  existing B5/router/descriptor/stop regressions.
- `test_iq4prod_trial.py` has11 synthetic runner tests, including damaged data,
  flags/clock/CRC, transport, identities, resource/accounting failures, finite
  reference windows, runtime source jumps and mocked starvation CLI dispatch.
- `test_bench4prod.py` executes the actual command body with real portable source,
  table and CRC code: both clock ratios, priorities/inversion, runtime publication,
  starvation and setup/output/capture/publication/cleanup failure paths.

The runner uses B4's independent scalar FIR for the65536-symbol cyclic period.
Live captures use GF(2) xorshift jump-ahead plus a32768-symbol scalar FIR window,
bounded around the coarse completed-batch cursor; no ring or generated table is
used as waveform truth. All4096 observations must match a single stride17 offset.
It also reconstructs the final physical source ring from absolute publication
and checks CRC, including rotated ring wraps. A sparse capture is not proof of
every emitted sample.

## Reproduce and hand off to the coordinator

Build and native checks only (safe here):

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-b6 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON \
  -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-b6
make check
B6_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-b6/git_rev.h)
```

The explicit stack option on first configure ensures the4KiB compile definition
despite the inherited CMake option-order issue. Use **iqbench.uf2**, absolute path:
`/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.claude/worktrees/codex-b6-pio-producers/firmware/build-b6/iqbench.uf2`.
The coordinator loads it using their usual procedure. The commands below open
serial **only when the coordinator invokes them**. The runner first verifies
`info`; it neither builds, flashes nor sets clocks. Use a fresh output directory
each time, retaining the runner's source/binary hashes and raw failed captures.
The built identity is `5d7ddab57b-dirty`; its UF2 SHA256 is
`c111c7a32a09126191eb3d6a7fb47678ee8dc0bed86a05eb601d9977142275cf`,
and ELF SHA256 is
`85ae7ad19f5f8ddca3dd9b3c68a49eff58d7c7a5cfd9bbe423b72732d423c49b`.
A dirty revision alone does not identify edits; rebuilds can change these hashes.

```sh
# Stage1: cpw2 control and cpw1 target, both priorities.
python3 host/iq4prod_trial.py --ms 3000 --cpws 2 1 --priorities 0 13 \
  --inverts 0 --build firmware/build-b6 --expected-git "$B6_FW_GIT" \
  --out results/b6-pio-producers/cyclic-short

# Required inverted-clock control: data must still match exactly.
python3 host/iq4prod_trial.py --ms 3000 --cpws 2 1 --priorities 0 13 \
  --inverts 1 --build firmware/build-b6 --expected-git "$B6_FW_GIT" \
  --out results/b6-pio-producers/cyclic-inverted

# Stage2: new runtime data and finite grants, both rates/masks.
python3 host/iq4prod_trial.py --live --ms 3000 --cpws 2 1 --priorities 0 13 \
  --inverts 0 1 --build firmware/build-b6 --expected-git "$B6_FW_GIT" \
  --out results/b6-pio-producers/live-short

# Required starvation control; capture at50ms, stop publication at100ms.
python3 host/iq4prod_trial.py --live --starve-ms 100 --ms 3000 --cpws 2 1 \
  --priorities 0 13 --inverts 0 --build firmware/build-b6 \
  --expected-git "$B6_FW_GIT" --out results/b6-pio-producers/live-starved

# Only after short positives and controls pass:12 runs per stage.
python3 host/iq4prod_trial.py --ms 60000 --reps 3 --cpws 2 1 \
  --priorities 0 13 --inverts 0 --build firmware/build-b6 \
  --expected-git "$B6_FW_GIT" --out results/b6-pio-producers/cyclic-60s
python3 host/iq4prod_trial.py --live --ms 60000 --reps 3 --cpws 2 1 \
  --priorities 0 13 --inverts 0 --build firmware/build-b6 \
  --expected-git "$B6_FW_GIT" --out results/b6-pio-producers/live-60s
```

Manual lines are `bench4prod8 2 3000 0 0`, `bench4prod8 1 3000 0 13`, and
`bench4prod8live 1 3000 0 13 0`; use both priority0/13 at each rate. Replace
duration with60000 for qualification, invert with1 for its control, or final
live argument with100 for starvation. On a separately configured SYS256 image,
use `--clock-khz 256000 --cpws 4 2` for8/16Msym/s; this branch does not supply
the clock-support work. Default trials require SYS128 and cpw2/1.

Positive classification must be `digital_cyclic_producers_clean` or
`digital_live_producers_clean`, `faults=[]`, exit0, full duration, zero output/
pointer errors and TXSTALLs, restored selectors/PRBS/resources and correct rate.
Expect table CRC1578312796; cyclic source CRC782600642 and previous words
`[1555404632,903819826]`; live initial source CRC3373999265 and previous `[0,0]`.
Topology is four producers/four writers/two chains per axis, PIO-generated
addresses, one-symbol/one-word OUT8 rows. All4096 bus words must match, with
GPIO28=IQ_FLAG and both phases present. Inverted controls must retain zero data
mismatches while failing clock parity in4096/4096 observations and classify
`clock_inversion_detected` without other faults.

The live positive additionally requires advancing runtime publication beyond4096
words, final-ring CRC agreement, completed≤granted≤published for all four inputs,
at most4096 words owned, and valid worker/source-cycle accounting. Starvation
must classify `source_starvation_detected`, reason `SOURCE_EMPTY`, sticky producer
TXSTALL and its specific error, no unrelated errors, and finite read limits within
publication. Consequent writer TXSTALLs are allowed only for this negative
control. Stop must occur after100ms and within one ring duration plus2ms:
about106.1ms at cpw1 or110.2ms at cpw2. The early capture still needs all4096
correct data/clock observations. No sentinel or source restart is permitted.

## Risks and dead ends

B5's clean SRAM-pointer measurements motivate the16-cycle four-chain schedule;
they do not measure this PIO-pointer graph's higher port load. The decisive
unknowns are RX→trigger latency, simultaneous short-block handoffs, capture
arbitration and live finite-batch renewal. A short pass is insufficient given
the merged B2 image's rare40–51s faults. Priority0 and13 are both mandatory.
The worker may fail its source deadline despite safe ownership; preserve that
failure rather than relaxing TXSTALL checks or silently restarting a source.

Rejected: decimated producer input (wrong FIR); duplicated RX DREQ subscribers
(illegal credits);14-bit PIO rows at d1 (103.125% port); router graph (extra
port traffic); four additional grant DMAs (17channels); unbounded ENDLESS live
rings (stale replay); a selected-input shared reload trigger (no completion
barrier); ordinary lower-SRAM placement (link overflow). Finite software grants
trade a dedicated core and port polling for legal resources and explicit failure.

AFE/RF timing, spectrum/EVM, real FEC/PLFRAME production, ingress, temperature,
and complete transmitter scheduling remain open. B4's rounded8-bit held-DAC
model margin is unchanged and small; it is not new analog evidence here.

Intended small logical commits for the coordinator; none created:

1. `firmware/src/iq4prod.{c,h}`, `host/native/iq4prod_test.c`, `host/test_iq4prod.py`:
   **Implement and verify alternate-parity PIO row producers.**
2. `firmware/src/iq4prod_dma.{c,h}`, `firmware/src/dual_dma.{c,h}`, `host/test_dual_fifo.py`:
   **Add four independent RX chains and finite live input grants.**
3. `firmware/src/iq4prod_source.{c,h}`, `firmware/src/bench4prod.{c,h}`,
   `firmware/src/main.c`, `firmware/CMakeLists.txt`,
   `host/iq4prod_trial.py`, `host/test_iq4prod_source.py`,
   `host/test_iq4prod_trial.py`, `host/test_bench4prod.py`, `Makefile`:
   **Expose opt-in cyclic and runtime8PSK producer trials.**
4. `docs/rd/b6-pio-producers.md`:
   **Record B6 budgets, native evidence and coordinator device gates.**
