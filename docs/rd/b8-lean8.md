# B8: bounded, frame-parallel complete 8PSK transmitter

Implemented, built and independently verified on the host; **device qualification is pending**.
`b8lean8` combines real on-chip PV-SPI GCS ingress, C3 frame-parallel BCH/LDPC, direct packed
8PSK axis codes, ordered publication, B7's two PIO address producers, automatic DMA input
grants, OUT8 writers and GPIO28 DDR clock. It reserves neither core for grant service. The
8 Msym/s / approximately 70% per-core target is a qualification gate, not a measured result.

Everything is opt-in in `IQ4HIST_EXPERIMENT=ON` **iqbench**. Existing commands and their
defaults are unchanged; pvflight does not include B8. No Pico, serial port, flashing command,
Git metadata write or existing results evidence was used or modified.

## Why this graph

The [B7 measurements](../../results/b7-live8/2026-10-15/README.md) establish the complete
two-producer OUT8 graph at 8 Msym/s, including priority 0. [B6](b6-pio-producers.md) establishes
the four-producer waveform graph at 16 Msym/s, but its dedicated core-1 grant worker conflicts
with FEC. Adding the three-SM on-chip SPI source and capture to B6 requires **13 SMs**. Adding
four R2 reload channels as well would require **20 DMA channels**. Neither fits.

B7's `iq4ptr_prepare_live8` already implements the
[R2 reload topology](../../results/r2-hw-offload/2026-10-15/README.md): each input DMA chains
to a one-word reload DMA, which writes the next finite count into its input channel's
`AL1_TRANS_COUNT_TRIG`. Normal-channel retriggering reloads its configured count; it does not
need a CPU rearm or a TRIGGER_SELF mode. Existing rolling credits end in a zero sentinel.
The measured R2 result is 24/24 exact **finite-prefix** trials, not a new B8 measurement.

This choice frees core 1 with no DMA/PIO backend modification. Faster FEC alone cannot make
this two-chain graph reach 16 Msym/s: its measured approximately 12-cycle row loop has only
an 8-cycle deadline there. The cpw1 command is an intentional limit probe.

## Implementation and ownership

The command is
`b8lean8 code ms cpw invert prio options starve`, defaulting to `8 3000 2 0 13 27 0`.
Supported normal-frame code indices are 5 (2/3), 6 (3/4), 8 (5/6); all have pilots and Gold 0.
cpw2 means 8 Msym/s, 32 MSPS per axis, 64 M interleaved bus words/s. cpw1 means 16 Msym/s
and 64 MSPS per axis. SYS must be 128 MHz; B8 never changes it.

[b8_ingress.c](../../firmware/src/b8_ingress.c) replaces C3's synthetic memory source with
one real, sequential `pvspi_next_packet` consumer on core 0. It builds GCS MATYPE 0x72,
UPL/SYNCD 0, DFL Kbch−80, SYNC 0xb9 and header CRC-8. The current 188-byte container remains
borrowed across slices and frame boundaries until completely consumed. A transient empty
queue preserves partial bytes and returns WAIT, rather than dropping or padding input.
Actual GCS byte/packet/frame counters and the first four raw BBFRAME CRCs are retained.
This command implements GCS; there is no TS selector.

Every adapter call prepares at most 64 words. With option 8, it fuses byte swapping and BB
scrambling into FE stores; without it, copy/scramble and tail clearing also run in bounded
64-word calls. Both paths finish through `dvbs2_frontend_begin_prepared`, avoiding C3's
original whole-frame copy/clear at its GCS→BCH transition. Once READY, a job can safely call
`c3_job_step(job, NULL)`; the synthetic GCS stage is never entered. BCH uses 64 words/step,
LDPC one existing unit, mapper at most eight 32-symbol chunks plus reached constants.

[b8_lean8.c](../../firmware/src/b8_lean8.c) assigns even frames to core 0 and odd frames to
core 1. Core 0 alone fills both jobs in source order, then releases prepared jobs through a
DMB and status flag. Both cores retain private FE, BCH and LDPC state. Core 0 alone publishes
completed frames, in order, using C3's deferred first-word merge and private trailing carry.
Frames are never padded individually, and FIR history never resets at a frame seam.

Admission reserves the complete future output interval before a mapper can write anything:

```text
ceil((next_frame + 1) * 22194 / 16) <= reusable_words + 4096
```

This includes an extra conservative reservation for a private partial tail. It counts
unpublished stores from admitted jobs, not just the published prefix. `reusable_words` comes
only from the existing guarded minimum DMA read cursor, never from elapsed time. At most two
jobs exist, and only one is being filled. Adjacent mappers have disjoint physical stores;
only the ordered publisher merges their boundary. The first two complete frames provide
2774 complete code words and four private trailing symbols before output starts. Frame 2
cannot be admitted until at least 66 guarded words have become reusable with this ceiling.

The existing grant publisher installs zero sentinels before releasing valid 512-word counts.
Every grant lies within the complete paired-axis publication. A late CPU cannot authorize
unpublished data: the DMA reaches a zero grant and stops. PIO/FIFO stall flags and the null
trigger remain sticky evidence. `starve=1` deliberately admits no frames beyond the two
startup frames; it must reach SOURCE_EMPTY, not replay the ring.

Core 0 services publication/faults at a requested 64 us cadence, with a hard **128 us observed
gap** fault. Each work call also records and faults slices above 16384 cycles at SYS128.
This is bounded work granularity plus measured fault detection, not a worst-case arbitration
proof. Typical leaf instruction estimates are below the remaining 64 us margin; the device
must establish that the combined service gap really stays within 128 us.

On stop, the writers and GPIO28 stop first, then pointer DMA retires. Admitted FILL/RUN jobs
drain while SPI remains available, both workers join, and all successful completed jobs
publish in order before CRCs. Final `published_groups` may therefore exceed the stopped
DMA's `published_words` by the bounded admitted suffix. This is reported, not hidden.
Selectors are restored and borrowed ordinary tables/input buffers rebuilt after quiescence.
A pointer abort failure retains its claims and prevents input/link buffer restoration.

## Hardware and traffic budgets

Counts below come from the actual existing backends; B8 adds no channel, SM or instruction
program. Each DREQ has a single DMA subscriber.

| PIO | SM allocation | Instruction words |
|---|---|---:|
| PIO0 | two OUT8 writers | 4 / 32 |
| PIO1 | PV receive, CS/READY gate, on-chip master, sparse capture | 4+5+10+3 = 22 / 32 |
| PIO2 | two full-history address producers, GPIO28 clock | 7+2 = 9 / 32 |
| Total | **9 / 12 SMs** | each block fits |

DMA is **12 / 16 channels** with capture: two input, two credit reload, two address control,
two sample, three PV-SPI (RX/master/rewind), one capture. PIO1 is full despite three globally
spare SMs. The seven-instruction producer consumes 112 SYS cycles per 16-symbol word,
43.75% of its 256-cycle cpw2 budget; cpw1 is 87.5%. Each writer chain gets 16 SYS cycles per
row at cpw2, versus the measured 12-cycle loop: nominal four-cycle margin plus FIFO runway.

A producer RX→DMA trigger costs **two** FASTPERI accesses. Counts for both axes per symbol:

| Work | DMA read/write transfer pairs | FASTPERI accesses |
|---|---:|---:|
| two SRAM→writer sample rows | 2 | 2 |
| two producer RX→sample-trigger addresses | 2 | 4 |
| two input streams, one word per 16 symbols each | 0.125 | 0.125 |
| two reloads per 8192 symbols | 1/4096 | 1/4096 |
| total before capture/SPI/software | **4.125244** | **6.125244** |
| stride-17 pins32 capture, while active | +8/17 | +8/17 |

At 8 Msym/s these are 25.783% of ideal DMA capacity and 38.283% of the shared fast port;
capture raises them to 28.724% and 41.224%. The 32 MHz serial source contributes at most
1 Mword/s each for RX and master DMA, another 1.563 percentage points, plus a small rewind
transfer per 5328 words. IRQs, CPU CSR reads, USB and arbitration are additional. These
are access-count bounds, not latency measurements. The fast-port counting follows the
checked explanation in [B6](b6-pio-producers.md#budgets) and [RP2350 notes](../rp2350-notes.md).

Sample/input SRAM reads are 8.5 bytes/symbol, **68 MB/s** at 8 Msym/s. Packed output stores
are another 0.5 bytes/symbol, **4 MB/s**, plus BB/FEC workspace traffic. No CPU computes a
sample, materializes a signed-plane ring, converts plane groups, or builds DMA descriptors.
These totals do not establish bank-specific bandwidth.

## SRAM and cycle planning

Release experiment map/DWARF, GCC Arm 14.2.1, SDK 2.2.0. No pre-existing object was explicitly
moved. New code remains in flash; copied C4 leaves remain inside the borrowed context.

| Storage | Address / bytes |
|---|---|
| Existing ring reservation / OUT8 table | 0x20000000 / 131072 reserved, first 65536 table |
| B8 context, in unused ring tail | **0x20010000 / 53584**, end 0x2001d150 |
| Shared Gold words | 0x200197b4 / 5536 |
| Mapper table/code reservation | 0x2001ad54 / 1936 |
| LDPC executable reservation | 0x2001b4e4 / 7168 |
| Ingress state | 0x2001d0e4 / 64 |
| I / Q code rings | 0x20020000 / 0x20024000, 16384 each |
| PV slots, borrowed link ring | 0x20028000 / 32768 reserved, 31968 used |
| Existing Q / I table (SPI emulator borrows Q) | 0x20040000 / 0x20048000, 32768 each |
| Sparse capture, borrowed cap_buf | 0x20051fac / 16384 used of 57344 reserved |
| Lower .data / .bss | 0x20030110 / 40880; 0x2003a0c0 / 23280 |
| Lower minimum heap | 0x2003fbb0 / 1024, **80 bytes remain** |
| SRAM4–7 data / core-0 stack | 258024 / 4096; **24 bytes remain** below 0x2007f000 |
| SCRATCH_X code / core-1 stack | 2032 / 2048; **16 bytes remain** |
| SCRATCH_Y existing code/state | 3688; **408 bytes remain** |

The context leaves 11952 bytes in its tail allocation, which is temporary borrowed capacity,
not free static SRAM. It contains both complete C3 jobs, shared mapper/LDPC arenas and all
B8 counters. Existing B6 scratch worker code/state remains linked but B8 never starts it.
The B8 ingress object contributes zero static data/BSS; its optimized function is 964 bytes
in this linked image. Recheck the map after every merge: tiny placement differences already
changed measured two-core FEC substantially in the
[bank experiment](../../results/ldpc-banks/2026-10-15/README.md).

Per assigned alternate frame, SYS128 gives **710208 cycles/core at 8 Msym/s**, or 355104 at
16 Msym/s. A 70% planning target allows 497146 cycles/core at 8 Msym/s. Options 27 selects
SRAM LDPC, table mapper and fused BB preparation while leaving scratch-Y BCH off, avoiding
its measured same-bank contention. Bit 16 is accepted for C3/C4 mask compatibility; B8 uses
hardware retirement/credit admission and has no timer deadline division to cache.

The measured isolated one-core 5/6 stage figures are approximately BCH 106k, LDPC 135k,
mapper 99.5k; 2/3 is 84k, 142k, 99.6k. They are **prior measurements**, not B8 timings.
The new fused steady-state input loop has seven Thumb instructions/word (two loads, REV,
EOR, compare, store, taken branch). Using the timing assumptions in
[rp2350-notes.md](../rp2350-notes.md), about eight cycles/word gives 13.5k for 1683 words at
5/6, before packet boundaries, callback validation, header, tail clearing and service.
Budget **20–35k/source frame**, or 40–70k per core-0 assigned frame because core 0 feeds both
parities. First-four-frame CRC diagnostics cost extra and remain included in runtime maxima.

Planning envelope, **estimates rather than measurements**: at 5/6, allowing 8–15% contention
on the 340.5k compute sum gives 368–392k/core assigned frame. Core 0 then adds 40–70k ingress
and an unmeasured 25–60k service/dispatch allowance: 433–522k, about **61–74%**. Core 1's
compute alone is about **52–55%**, plus dispatch. Thus under 70% is plausible but unproven,
and the upper envelope misses it. No host elapsed time was converted into M33 cycles.
3/4's current flash t=12 BCH may also miss the headroom target.

`core[].busy_cycles` excludes startup and intentional idle waits, includes runtime slices,
and core 0 includes both ingress streams and foreground supervision. Final draining is
conservatively charged against the live elapsed budget. IRQ time overlapping charged work
is included; interrupts during excluded idle waits are not separately counted. These are
the repository's foreground work figures, not an exact total interrupt-inclusive occupancy
measurement. Require additional margin below 70% before claiming that stronger result.

## Native verification

Both `make build` and the required Release experiment build pass; introduced C code compiles
without warnings. `make check` passes its 103 Python invocations, including these new tests
(all run without USB):

- `host/test_b8_ingress.py`: 408 frames over three rates, 17 option/budget/starvation cases,
  masks including 0/27/31, tiny and oversized budgets capped at 64. Real PV messages are
  validated, borrowed packet lifetime is revoked at each callback, arbitrary WAITs retain
  partial words, dirty FE tails clear, and zero budgets make no writes. Full packed rings
  and carries match independent Python GCS → scalar BCH/LDPC → complex 8PSK after every pair;
  first-four BB CRCs match. ASan/UBSan also passes.
- `host/test_b8_bench.py`: executes the actual production command with real ingress/FEC and
  a virtual-time DMA/PIO/core-1 environment. All rates × options 0/27 match independent final
  ring CRCs through wraps. Jobs overlap, and odd frames finish before earlier even frames.
  Tests cover finite starvation, overlong service/slice faults, selector/pointer/output/SPI
  setup failures, resource admission, guards, serialization and restoration. ASan/UBSan passes.
  Its capture shim verifies serialization only; it does not simulate GPIO/FIR arbitration.
- `host/test_b8_lean8_trial.py`: ten test groups check the independent all-rate scalar
  GCS/FEC/PL/FIR reference, agreement with B7's 5/6 reference, real frame seams, 4096-snapshot
  matching, clock inversion, late corruption, physical final-ring CRCs, ownership, 128 us
  limits, 70% rejection, malformed metadata and fail-closed starvation classification.
  Offline defaults and `--self-test` cannot construct a board connection.

The device runner reconstructs a bounded window from actual PV pattern bytes, scalar BCH
and LDPC, scalar complex PLFRAME points, geometric four-level codes and the frozen L7 FIR.
It never uses a firmware LUT, native encoder output or captured ring as waveform truth.
It also reconstructs the last approximately four frames for complete physical ring CRCs,
excluding the final private partial word. A sparse capture still samples only 4096 positions.
Existing waveform self-tests may report absent gitignored historical binaries; those old
firmware hashes cannot be revalidated here.

## Coordinator reproduction and pass criteria

Safe build/native commands, completed here:

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
make check
```

Coordinator only: load `firmware/build-exp/iqbench.uf2` through the usual process. Absolute
artifact path is
`/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.claude/worktrees/codex-b8-lean8/firmware/build-exp/iqbench.uf2`.
Use an explicit real port in `B8_PORT` and fresh results directories. Runners never flash.
The manifest pins source and build SHA-256s; the dirty Git identifier alone is insufficient.
The reviewed build identifies as `02962753ad-dirty`: UF2 SHA-256
`2fbc1e725dbd6bc2622ac41eabf267ee20dc843852aa37dd9330d4ea71b2fdcd`, ELF SHA-256
`cff217a3d46d06e6da353640fbce500cba11f8206d778e0e6a9b9555190ca774`.
Rebuilding may change those hashes; use the runner's actual manifest.

```sh
B8_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-exp/git_rev.h)
python3 host/iqbench.py --port "$B8_PORT" raw info
python3 host/iqbench.py --port "$B8_PORT" raw stackcheck

# Same-image FEC diagnosis; these use synthetic memory GCS, not the SPI waveform source.
python3 host/iqbench.py --port "$B8_PORT" raw 'c3pair 8 40 0 0 27'
python3 host/iqbench.py --port "$B8_PORT" raw 'c3pair 8 40 0 2 27'
python3 host/iqbench.py --port "$B8_PORT" raw 'c3fec 8 3000 0 2 128 27'

# Same-image option off/on A/B, full independent pins/ring/source checks.
# max-busy1 is cadence-only diagnostic; it cannot qualify the 70% headroom claim.
python3 host/b8_lean8_trial.py --port "$B8_PORT" --build firmware/build-exp \
  --expected-git "$B8_GIT" --out results/b8-lean8/ab --ms 3000 \
  --codes 5 8 6 --cpws 2 --prios 13 0 --options 0 27 --inverts 0 --max-busy 1

# Required 60s qualification at both priorities; default per-core target is70%.
python3 host/b8_lean8_trial.py --port "$B8_PORT" --build firmware/build-exp \
  --expected-git "$B8_GIT" --out results/b8-lean8/qualify-60s --ms 60000 \
  --codes 8 --cpws 2 --prios 13 0 --options 27 --inverts 0

# Inverted clock: all4096 clock checks must invert, while waveform samples remain exact.
python3 host/b8_lean8_trial.py --port "$B8_PORT" --build firmware/build-exp \
  --expected-git "$B8_GIT" --out results/b8-lean8/invert --ms 3000 \
  --codes 8 --cpws 2 --prios 13 0 --options 27 --inverts 1

# Finite starvation: no frames beyond startup; SOURCE_EMPTY/null trigger required.
python3 host/b8_lean8_trial.py --port "$B8_PORT" --build firmware/build-exp \
  --expected-git "$B8_GIT" --out results/b8-lean8/starve --ms 3000 \
  --codes 8 --cpws 2 --prios 13 0 --options 27 --starves 1

# 16Msym/s limit probe: retain failure and raw records; nonzero exit is expected.
python3 host/b8_lean8_trial.py --port "$B8_PORT" --build firmware/build-exp \
  --expected-git "$B8_GIT" --out results/b8-lean8/probe-16m --ms 3000 \
  --codes 5 8 --cpws 1 --prios 13 0 --options 27 --max-busy 1
python3 host/iqbench.py --port "$B8_PORT" raw stackcheck
```

Manual equivalents: `b8lean8 8 60000 2 0 13 27 0`, priority control
`b8lean8 8 60000 2 0 0 27 0`, off control `b8lean8 8 3000 2 0 13 0 0`, inverted
`b8lean8 8 3000 2 1 13 27 0`, starvation `b8lean8 8 3000 2 0 13 27 1`, probe
`b8lean8 8 3000 1 0 13 27 0`.

Positive gate: requested identity/profile, full duration, zero errors/stalls/null triggers,
4096/4096 independent sample matches at one finite offset, correct clock parity, exact
first-four BB and final physical-ring CRCs, valid PV accounting, monotonic
`reusable <= min(issue) <= max(issue) <= grant <= DMA publication <= final mapped groups`,
bounded final drain suffix, both parity frame counts, strictly positive credit runway,
service gap ≤128 us and each measured work slice ≤16384 cycles. Require both busy figures
≤70% for the foreground target, plus IRQ allowance for total occupancy, and intact stack/code
paint before/after. The runner enforces the foreground target by default.

Inversion must show **4096/4096 clock mismatches against normal I-slot-high**, with exact
waveform samples. Starvation must stop early with pointer SOURCE_EMPTY bit64, a nonzero null
trigger mask, exactly two published frames and exact startup source/ring CRCs. PIO-empty
and writer-stall bits are allowed consequences; unrelated faults fail. Mid-run capture is
explicitly skipped because the finite startup prefix empties before the capture time.
A healthy-looking starvation run fails its control. The 16M probe cannot qualify this
graph merely by lowering utilization or accepting a short run.

## Remaining risks, dead ends and route to 16 Msym/s

The principal open result is complete B8 throughput under real SPI IRQ, SRAM and DMA load.
Foreground CPU headroom, service maxima, code/stack paint and startup cold maxima must be
measured in this image. Bank arbitration is placement-sensitive; host scheduling proves no
M33 timing. The inherited output stop loop and worker join/drain can wait indefinitely on
broken hardware; the pointer module has its own bounded abort/claim-retention path. No new
claim is made about external ingress, AFE pad timing, analog spectrum, EVM or receiver decode.

Rejected here: B6 plus three-SM self-test ingress (13SM), four dedicated R2 reload channels
with all peripherals (20DMA), polling grants on core1 (conflicts with FEC), endless cyclic
input (can replay unpublished data), signed-plane conversion (extra ring/work), scratch-Y BCH
option31 (measured contention), whole-frame BB admission (known service-gap failure), and
claiming a faster two-writer cpw1 row loop merely from faster FEC.

A resource-feasible later 16 Msym/s graph is B6's four producers/four writers, an **external** PV
receiver with just RX+CS SMs and no claimed emulator resources, and completion-interrupt
input rearm. With capture it would use 12 SMs and 14 DMA channels. This requires changing pvspi's current
unconditional emulator claims and integrating/proving IRQ rearm latency; it is a design,
not implemented or measured here. The external sender also needs higher throughput:
32 MHz single-lane SPI carries at most 32*1316/1332 = 31.616 Mb/s payload before handshake gaps,
below 38.76 Mb/s for 5/6 at 16 Msym/s. Even 2/3's approximately 30.97 Mb/s leaves little wire margin.
Thus 16 Msym/s needs faster FEC **and** a different output/ingress resource arrangement. B8's direct
packed rings, ownership, bounded adapter and ordered publisher can be reused in that graph.

Intended small logical commits for the coordinator; none was created:

1. `firmware/src/b8_ingress.{c,h}`, `host/native/b8_ingress_test.c`,
   `host/test_b8_ingress.py`: **Prepare real PV-SPI GCS jobs in bounded slices.**
2. `firmware/src/b8_lean8.{c,h}`, `firmware/src/main.c`, `firmware/CMakeLists.txt`,
   `host/test_b8_bench.py`: **Integrate opt-in frame-parallel 8PSK with automatic DMA grants.**
3. `host/b8_lean8_trial.py`, `host/test_b8_lean8_trial.py`, `Makefile`:
   **Verify B8 pins and retained rings against independent scalar references.**
4. `docs/rd/b8-lean8.md`, `docs/rd/README.md`:
   **Record B8 budgets, native evidence and coordinator qualification commands.**
