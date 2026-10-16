# B3: alternating one-symbol 8PSK rows

Implemented an opt-in **live four-writer mode** of `bench4fec`, with the existing
GCS → BCH/LDPC → rotated 8PSK → four-level L7/N4 table. It preserves live PV-SPI,
bounded input credits, GPIO28 DDR and all 4,096 independent capture observations.
The two-writer command remains the default. No Pico, serial port or flashing operation
was used; **stall-free device operation remains unmeasured**.

The practical design reuses A2's sole-RX control/router graph. Each axis has separate
even/odd sample channels and writer FIFOs, returning every **32 SYS cycles at cpw2**,
instead of 16. Its control/router handoffs remain serialized: this is not two
independent control/sample chains with independently relaxed 32-cycle deadlines.
It adds two FASTPERI accesses per symbol, fits **11 SMs / 16 DMA channels**, and is the
smallest live experiment for testing whether additional writer runway fixes the stalls.
A literal duplicate-producer live design exceeds the resources; the closest cyclic
alternative is budgeted below, but is not implemented or claimed as live FEC.

## Regression investigation

Both `2081cb740b` and `0b89b341ce` were built from Git archives, with SDK 2.2.0 and
GCC Arm 14.2.1. The merged build explicitly enabled the existing 4-KiB SRAM-high stack.
Detailed addresses, raw executable-section comparisons and counterfactual experiments
are in [the regression investigation](b3-regression.md), with retained
[comparison data](b3-linker-comparison.json) and
[reproduction script](../../host/b3_linker_compare.py).

**The source comparison narrows the failure mechanism; it does not identify a proven
image-specific root cause.** Failed merged runs show output TXSTALL with intact
transport, zero pointer/null-trigger faults and substantial input-credit runway.
That identifies a missed sample refill deadline, rather than source exhaustion.
Six clean B2 runs are insufficient to establish a zero long-soak failure rate.

| Compared setting | B2 versus merge |
|---|---|
| DMA claim order | Identical: PV 0/1/2; I input/credit 3/4; Q 5/6; controls 7/8; samples 9/10; capture 11 |
| DMA HIGH_PRIORITY | Identical: input/credit/control/sample high; PV/capture low |
| PIO/SM | Identical: writers PIO0 SM0/1; PV PIO1 SM0/1/2 + capture SM3; pointers PIO2 SM0/1 + clock SM2 |
| IRQs | Same core-0 PIO1 IRQ0 CS handler; output/input DMA IRQ enables cleared, raw quiet/null status polled |
| Large arenas, iq4ptr credits/context | All identical addresses; table `0x20000000`, code rings `0x20020000/0x20024000`, credits `0x2007eec0` |
| Live encoder, iq4ptr, mapper, table and PV source | Source-identical; executable object sections identical before relocation |
| A2 changes in `dual_dma.c` | Setup branches and 16-byte router-target state; unchanged running fault/capture/stop sections; no router runs in two-writer mode |
| Core-1 SRAM code | BCH wrapper grows 32 B; most later functions move +0x20, preserving striped-bank bits 3:2 |
| Flash code | Pack, bench and encoder-task addresses move +0x7b8, changing XIP placement |

SRAM placement is a variable, but these maps do **not** support a wholesale bank rotation:
the major arenas stay fixed, and +0x20 code / +0x10 BSS moves preserve bits 3:2
(checked RP2350 §2.2.3, printed p31). C2's new t=8 leaf is not entered by kernel 2 / 5/6;
the BCH dispatcher still changes the instruction/traffic phase. The stack move is not
necessary for the fault: the coordinator's merged SCRATCH_Y runs also failed.

Ranked hypotheses, with mask 13 and cpw2 held fixed:

1. **Rare short-row handoff latency under coincident traffic.** Run schedules 0–3 for
   60 s each, then four writers. Schedule sensitivity tests DMA issue/order margin;
   improvement only with four writers supports additional FIFO runway. Average bandwidth
   does not bound the tail latency.
2. **XIP/BCH-dispatch placement changes competing CPU traffic phase.** In separate
   archived merge trees, restore only B2 `dvbs2.c`, then only B2 `dual_dma.c`, retaining
   the SRAM-high stack. Repeat the original four-argument command with fresh boots in
   randomized A/B/A/B order. A recovery narrows the perturbation; follow with controlled
   XIP alignment changes. These are separate investigation builds, not B3 source edits.
3. **USB or capture contributes a rare traffic collision.** Compare a lab build that
   suppresses capture, and another moving launch to one-quarter duration. Existing
   40–51 s failures occur well after the 30 s capture, so launch alone is not established.
   A USB-background-service control can isolate USB; keep supervision and restore USB
   before printing. Such controls cannot satisfy the final capture-based pass criteria.
4. **The apparent regression is undersampled.** Repeat original B2 and merged images
   after fresh boots, randomized order, identical USB conditions, retaining every run.
   A shared nonzero hazard rate weakens an image-specific attribution.

Channel numbering, explicit HIGH_PRIORITY settings, PIO assignments, DMA IRQ setup,
table/ring/credit placement and the pointer program did not change. Schedule 3 is a new
sensitivity probe, not restoration of a different B2 numbering. Unsigned DWT stage
deltas handle the 33.55 s wrap; timer/word counters control streaming, with no
wrap-dependent 40-second threshold found.

## Implementation and ownership

Device syntax is now:

```text
bench4fec cpw ms invert [prio [writers [schedule]]]
```

`writers=2` and `schedule=0` are defaults. Writers must be 2 or 4; schedules are:

| Schedule | Control/router HIGH_PRIORITY | Sample HIGH_PRIORITY | Claim order |
|---:|---:|---:|---|
| 0 | 1 | 1 | controls, then samples |
| 1 | 1 | 0 | controls, then samples |
| 2 | 0 | 1 | controls, then samples |
| 3 | 1 | 1 | adjacent control/sample IDs |

Input and credit DMA remain high priority in every schedule. BUS_PRIORITY is a separate
selector; keep it at 13 for the comparison. HIGH_PRIORITY affects DMA internal scheduling,
not the bus manager priority (checked RP2350 CTRL register, printed pp1128–1129).

For each axis, C alone reads the pointer RX FIFO and writes the selected sample's
`AL3_READ_ADDR_TRIG`. A completed two-word sample transfer chains to R, which reads the
opposite target from the existing aligned two-entry selector ring and writes C's
`AL2_WRITE_ADDR_TRIG`. Only C starts at startup. Ordered completion selects row k for
writer k mod 2 without changing an active sample's address. The same graph already
handles A2; B3 adds four-sample writer programs, not a second RX-DREQ consumer.

Each new writer executes `OUT; JMP X--; SET X,3 [3]; JMP [3]`: eight active PIO slots,
then eight silent slots. At cpw2 each tick is two SYS cycles, giving a 32-cycle loop.
Starts/X are the existing `(0,1,2,1)` / `(3,4,0,0)` pattern. I writes even bus slots with
GPIO14 high; Q writes odd slots with GPIO14 low; GPIO15 stays zero. The GPIO28 clock
starts atomically with the writers through PIO1 CTRL. All four FIFOs and OSRs prefill.
Silent non-OUT slots do not stall on an empty OSR; the next mandatory OUT still requires
data. This creates runway, not an arbitration guarantee.

The source producer and its 64-byte grant ring are unchanged. Core 0 publishes completed
I/Q words together; only the guarded minimum input cursor releases storage. Six symbols
of history and partial mapper groups persist across frames, pilots, code-ring and
selector-ring wraps. Grants are whole 512-word batches followed by zero sentinels;
eight input words remain guarded. Polling stays 128 µs with a 256 µs deadline. Output
faults stop all writers and GPIO28 before pointer claims are released. Selector restore
still returns writer/row/schedule/priority/clock settings to their prior values.

## First-principles budgets

`Rs = SYS / (8 × cpw)`. At cpw2, one N4 symbol occupies 16 SYS cycles at either clock.
SYS128 gives Rs=8 MHz and DACCLK=32 MHz; SYS256 gives Rs=16 MHz and DACCLK=64 MHz.
No clock, VREG, QMI, peripheral-clock or SPI-divider firmware changes were made, and
the `bench4fec.c` SYS guard line is unchanged. Current B3 firmware still requires SYS128.

| Live four-writer service | SMs | Instruction words | DMA channels |
|---|---:|---:|---:|
| PIO0 pointers + GPIO28 DDR | 2 + 1 | 10 shared + 2 = 12 | 4 input/credit |
| PIO1 PV RX/CS/emulator + capture | 3 + 1 | 4 + 5 + 10 + 3 = 22 | 3 PV + 1 capture |
| PIO2 even/odd I/Q writers | 4 | 4 + 4 = 8 | 2 C + 2 R + 4 samples = 8 |
| Total | **11 / 12** | **each block ≤32** | **16 / 16** |

On an otherwise idle board, schedule 0 claims C-I/Q=7/8, R-I/Q=9/10,
samples I-even/Q-even/I-odd/Q-odd=11/12/13/14, capture=15. Schedule 3 gives
C-I=7/sample-I-even=8, C-Q=9/sample-Q-even=10, R-I=11/sample-I-odd=12,
R-Q=13/sample-Q-odd=14, capture=15. PV and input claims retain 0–6.

The checked local RP2350 datasheet §2.1 printed p24 lists PIO0/1/2, USB and DMA control
registers behind one shared FASTPERI port; §2.1.1 p25 allows at most one zero-wait access
per SYS cycle. Both endpoints of RX → trigger transfers consume that port.

| Work per symbol, both axes | DMA transfers | FASTPERI accesses |
|---|---:|---:|
| Four sample words, SRAM → writer TX | 4 | 4 |
| Two pointers, RX → sample trigger | 2 | 4 |
| Two selector words, SRAM → C destination trigger | 2 | 2 |
| Packed input, two words / 16 symbols | 0.125 | 0.125 |
| Main path | **8.125** | **10.125** |
| Credit reloads, two / 512 input words | 1/4096 | 1/4096 |
| Active pins32 capture, one observation / 17 bus slots | 8/17 | 8/17 |

Main-path DMA issue utilization is 8.125/16=**50.78125%**; the stronger shared-port
utilization is 10.125/16=**63.28125%**. The original direct path is 6.125 transfers and
8.125 accesses/symbol, or 38.28125% DMA / 50.78125% port. Routers cost 16 M additional
transfers and accesses/s at SYS128/cpw2. Table reads are 128 MB/s, packed input reads
4 MB/s and router reads 64 MB/s: **196 MB/s SRAM reads** before SPI/FEC/CPU traffic.
GPIO FIFO writes are 128 MB/s; all rates double at SYS256/cpw2.

At SYS128, continuous 32-MHz SPI bounds RX+emulated TX at 2 M word transfers/accesses/s;
its 16×333-word emulator reload adds at most 187.69/s. Source credits add 1,953.125/s.
For steady GCS 5/6, the exact payload-driven average is
`(6720 / 22194 / 1316) × (666 + 1/16)` = **0.1532472836 transfers/accesses per symbol**,
or 1.225978269 M/s at Rs8; prefill and admission alter finite-run totals.
Capture adds **3.764706 M/s**, or 2.941176 port points, for **1.088 ms** per 4096-word
capture. This is the IN32 pins capture, not the legacy two-IN16-observations-per-DMA path.
Including that conservative SPI ceiling, credits and active capture gives approximately
**55.2866% DMA / 67.7866% FASTPERI**. CPU register polls, CS IRQ control writes, USB,
arbitration and wait states are additional; these are optimistic service ceilings, not
worst-case latency bounds. If SPI scales to 64 MHz with SYS256, the percentages repeat.

The pointer program remains **145 PIO cycles / 16 symbols = 9.0625 cycles/symbol**,
56.640625% of its cpw2 budget before backpressure. Writer programs emit four samples
and return every two symbols; the common C/R sequence still services one row per symbol
on average. The native event model tolerates an isolated row-64 I completion blackout of
72 SYS slots with two writers and 135 with four. Those thresholds depend on the model's
scheduler and are **simulations**, not M33 timings or measured DMA latency tolerance.

At SYS128/cpw1, even the direct engine needs 8.125 accesses in eight cycles =101.5625%
before SPI/capture. The routed engine needs 126.5625%. Extra chains do not solve this;
the frontier report's transfer-only SYS128 16-Msym/s argument is invalid. SYS256/cpw2
preserves the 16-cycle symbol ratio instead of solving it through chaining alone.

No persistent SRAM is added by B3. The existing 16-byte A2 selectors are reused.
The final B3 map retains these key allocations:

| Allocation | Address / size |
|---|---|
| Shared table, borrowed `ring_buf` | `0x20000000` / 131072 B |
| Two input rings / PV slots | `0x20020000` / 32768 B; `0x20028000` / 32768 B |
| Capture / signed-plane staging | `0x20051fac` / 16384 B; `0x20055fac` / 32768 B |
| Output state / iq4ptr state | `0x2003e5f8` / 576 B; `0x2003e838` / 144 B |
| iq4ptr credits / bench context | `0x2007eec0` / 64 B; `0x2007ef00` / 224 B |
| Lower-RAM BSS end | `0x2003f9f8`: 1544 B total to RAM limit, 520 B after minimum heap |
| SRAM-high end / core-0 stack | `0x2007efe0`; stack `0x2007f000..0x2007ffff` (32 B gap) |
| Scratch X / Y code | 2032 / 1952 B; core-1 2048-B stack stays in X |

The actual ARM pack leaf is still 43 instructions: four LDRH, two STR, eighteen ORR,
sixteen AND, two LSL and BX. Using [the project's M33 timing rules](../rp2350-notes.md),
**45–60 cycles/group**, or 2.8–3.75 cycles/symbol, is a contention-free estimate; XIP,
calls, IRQs and loop work can add cost. That is about 17.6–23.4% core 0 at cpw2.
Healthy supervision remains approximately 400–650 cycles/poll, about 2.4–4.0% core 0.
Routing adds no CPU work per row. A normal 22194-symbol frame has 355104 cycles/core at
cpw2 at either SYS. These are instruction/planning estimates, not native timing evidence.
The historical B2 measurements (about 25% core 0 / 89% core 1) are useful comparison points,
not measurements of B3 or an assurance that SYS256 has identical contention.

## Rejected approaches and limits

* Two controls draining one RX FIFO/DREQ are prohibited by checked RP2350 §12.6.4.2
  pp1101–1102: independent channel request-credit counters are not a shared queue.
* Four duplicate pointer SMs with independent live input/credit and control/sample pairs
  require 4 pointer + 4 writer + 3 SPI + 1 clock + 1 capture = **13 SMs**, and 8 input/credit
  + 8 output + 3 SPI + 1 capture = **20 DMA channels**. Neither resource fits.
* The closest independent-chain cyclic variant drops PV/SPI/FEC: **10 SMs / 13 DMA**,
  with four pointers in PIO0 (one shared 16-word push/discard program), clock+capture
  PIO1 (5 words), writers PIO2 (8 words). Each producer discards alternate symbols while
  retaining history (113 cycles/input word), uses its own RX DREQ, and emits 8 rows per
  input word. Duplicated source input raises total traffic to 6.25 DMA / 8.25 port accesses
  per symbol, not 8.125. It also needs a four-RX backend and atomic PIO1-clock support.
  This budgeted alternative is not implemented; the delivered mode retains real live FEC.
* RXF_PUTGET storage does not provide FIFO backpressure/ownership for alternated DMA
  readers (§11.4.8.2 p897); per-row acknowledgments reintroduce port traffic.
* A plain DMA address ring cannot stride 64 bytes between sample trigger registers.
  Doubling the full-history row needs 4^8 × 8 × 2 = 1 MiB; it does not fit SRAM.
* Router traffic can hurt a sustained service deficit despite improving isolated-blackout
  runway. All 16 DMA channels are occupied; A2's SYS128/cpw1 capture failure is a warning
  to measure B3 at its different cpw2 ratios, not evidence it passes or fails.
* The existing output backend has an unbounded abort wait; pointer stop has a bounded
  abort and retains claims on timeout. This inherited limitation was not broadened.
* Sparse digital captures and a 60-second run do not establish every output word,
  temperature/power soak, AFE setup/hold, RF spectrum/EVM or receiver acquisition.

## Native verification and build

`host/test_iq4rows_order.py` executes actual pointer/writer opcodes, builds the real C
LUT and compares output to the independent frozen-tap FIR. Its eight tests cover row and
writer parity, both axes, cpw2/4, delayed handoffs, the 4096-word source wrap, selector
wraps, exact finite-grant exhaustion, inverted GPIO28 and stop during control/each sample
word/router. Wrong routing and excessive recurring delay are rejected. The shared
event scheduler is not a model of the RP2350 bus arbiter.

`host/test_dual_fifo.py` compiles production setup/stop leaves with native register shims
under `-Wall -Wextra -Werror`: new two/four-writer row2 validation, exact row counts,
priorities and sole RX-DREQ ownership join the existing lifecycle/fault tests.
`host/test_dacclk.py` now includes the row2 four-writer layout. Unchanged iq4ptr lifecycle
tests check delayed abort retirement and credit/ownership bounds.

The 11 new runner tests retain the independent GCS → scalar BCH/LDPC → rotated 8PSK
PLFRAME → frozen L7 FIR oracle. They check all 16 two-row bus phases, corruption even
with a recomputed transport CRC, flag/spare/clock bits, complete capture, SPI provenance,
topology/clock/build identity, credit/stop accounting and mocked CLI dispatch/cleanup.
SYS256 validation tests use synthetic metadata and establish no device result.

Reproduce without hardware:

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-b3 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-b3
python3 host/test_iq4rows_order.py
python3 host/test_iq4fec_trial.py
python3 host/iq4fec_trial.py --self-test
make check

# Rebuild the two historical revisions and regenerate the retained comparison.
python3 host/b3_linker_compare.py --prefix /tmp/b3-compare-local \
  --json /tmp/b3-linker-comparison-local.json
```

Both iqbench/pvflight default and experiment builds completed without compiler warnings.
The default build was also reconfigured with explicit `CORE0_STACK_SRAM_HI=ON`: the
pre-existing CMake option-declaration order can otherwise give a 2-KiB stack on the first
clean configure. The experiment image has the verified 4-KiB stack. `make check` passed in full (exit 0), including the two new tests; the runner self-test
also passed. `git diff --check` and relative Markdown link checks passed.

## Coordinator device commands

Use this worktree's **`firmware/build-b3/iqbench.uf2`**, not pvflight. The current embedded
identity is `0b89b341ce-dirty`; UF2 SHA256 is
`70cb9fae558882bad5015f33ff195d7399eab3cd59f28f68a2dad4304a14428d`, ELF SHA256
`1fea6a00161591caf509417bc9917fc3d0fbddfc8a30c7e593055a3c04743cd6`.
Rebuilds after source/commit changes can change hashes. The runner archives binaries,
source hashes and configuration; retain them with every run and use fresh directories.
Only the coordinating engineer should load the image and execute these commands.

```sh
B3_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-b3/git_rev.h)

# First: root-cause scheduling probes, 8 Msym/s SYS128, 60 s EACH, mask 13.
python3 host/iq4fec_trial.py --ms 60000 --clocks 128000 --cpws 2 --inverts 0 \
  --prios 13 --writers 2 --schedules 0 1 2 3 \
  --expected-git "$B3_FW_GIT" --build firmware/build-b3 \
  --out results/b3-8psk-rows/root-cause-local

# Then: alternating rows at the same clock/rate/priority; baseline schedule first.
python3 host/iq4fec_trial.py --ms 60000 --clocks 128000 --cpws 2 --inverts 0 \
  --prios 13 --writers 4 --schedules 0 \
  --expected-git "$B3_FW_GIT" --build firmware/build-b3 \
  --out results/b3-8psk-rows/alternating-local

# Separate 60 s inverted-clock negative control: data must remain exact.
python3 host/iq4fec_trial.py --ms 60000 --clocks 128000 --cpws 2 --inverts 1 \
  --prios 13 --writers 4 --schedules 0 \
  --expected-git "$B3_FW_GIT" --build firmware/build-b3 \
  --out results/b3-8psk-rows/inverted-local
```

Equivalent device lines, after `clock 128000`, `bchkernel 2`, `ldpcpack 1`:
`bench4fec 2 60000 0 13 2 0`, `bench4fec 2 60000 0 13 2 1`,
`bench4fec 2 60000 0 13 2 2`, `bench4fec 2 60000 0 13 2 3`;
`bench4fec 2 60000 0 13 4 0`; negative `bench4fec 2 60000 1 13 4 0`.
Schedule probes may fail and the runner deliberately exits nonzero while retaining all
records. A failure is useful evidence, never permission to relax capture checks.

Positive pass: `classification=digital_live_clean`, `faults=[]`, `reason=complete`,
full 60-second elapsed duration, `writers=4`, `dma_schedule=0`, `pointer_pio=0`,
`pointer_routing=alternating_dma`, `sym_rate=8000000`, table CRC 431963165, zero pointer /
output /DMA /TXSTALL faults, positive credit runway, max poll gap≤256 µs, coherent
publication/issue bounds, advancing runtime encoder/GCS/PV counters and independent
BBFRAME/accepted-message CRCs. All 4096 observations must match one fixed independent
waveform offset and GPIO28 must equal IQ_FLAG with both phases present. Restore flags
must be true. The inverted run must retain all these checks with 4096/4096 clock-parity
mismatches and `classification=inverted_clock_detected`; its expected detection exits 0.

Repeat alternating positives and negative controls several times after fresh boots,
interleaving two-writer controls. One pass establishes that run, not a new soak claim.
If only four writers fail, try schedules 1/2/3 as separate 60-second sensitivity runs.
If both fail at cpw2, repeat cpw4 at SYS128 (4 Msym/s) as a slower diagnostic.

After the coordinator integrates `rd/oc256` and enables the bench SYS guard in that
branch, repeat **all three runner commands** with `--clocks 256000`, the combined build
path/identity and fresh output directories. Keep **cpw2**, priority 13 and the same
writer/schedule settings. Expected Rs becomes 16 MHz, DACCLK 64 MHz, BB capacity
38.7564 Mb/s. Live on-chip ingress must supply that rate (nominal 64-MHz SCK if half=2);
a 32-MHz source cannot carry it. Retain actual SCK/build metadata. These conditional
commands exercise runner support; this B3-only image rejects SYS256, as required.

## Intended small logical commits

Git metadata was treated as read-only; no commit or bundle was created.

1. `firmware/src/dual_dma.c`, `firmware/src/dual_dma.h`, `host/test_dual_fifo.py`,
   `host/test_dacclk.py`, `host/test_iq4rows_order.py`:
   **Add opt-in alternating two-word axis rows with four PIO writers.**
2. `firmware/src/bench4fec.c`, `firmware/src/bench4fec.h`, `firmware/src/main.c`,
   `host/iq4fec_trial.py`, `host/test_iq4fec_trial.py`, `Makefile`:
   **Expose live 8PSK writer and DMA-schedule probes with independent validation.**
3. `REPORT.md`, `docs/rd/b3-regression.md`, `docs/rd/b3-linker-comparison.json`,
   `host/b3_linker_compare.py`:
   **Record B3 regression comparison, resource budgets and coordinator trials.**
