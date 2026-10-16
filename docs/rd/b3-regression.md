# B3 offline B2-versus-merge regression investigation

The small retained machine-readable comparison is [b3-linker-comparison.json](b3-linker-comparison.json).
Reproduce it without hardware using [host/b3_linker_compare.py](../../host/b3_linker_compare.py):
`python3 host/b3_linker_compare.py --prefix /tmp/b3-compare-fresh --json /tmp/b3-compare.json`.
The default prefix is `/tmp/b3-regression`; export directories must not already exist.
`--skip-build` instead compares existing builds. This script uses read-only `git archive`,
`make build`, the configured ARM tools and a standard-library ELF32 reader.

This is source, object-code and linker-map evidence, not a device root-cause measurement.
No Pico, serial port, flashing tool or Git metadata write was used. The observed fault is
a short-row writer deadline miss: the failed records have an output TXSTALL, zero pointer
errors/null triggers, over 1,000 code words of credit runway, intact transport and a clean
earlier capture. This rules against a publication underflow in those records. It does not
identify which competing access delayed the critical sample refill. Six clean B2 runs
and five merged runs are insufficient to establish B2's long-soak hazard rate.

## Reproduction builds

Both revisions were exported without checking out or modifying Git metadata:

```sh
mkdir -p /tmp/b3-regression-b2 /tmp/b3-regression-merge
git archive 2081cb740bd8588c9a0efa159422b552e4fc5f26 | tar -x -C /tmp/b3-regression-b2
git archive 0b89b341ce1bf4cecfec1c16db5632d3b23572bf | tar -x -C /tmp/b3-regression-merge
```

In each directory, configure/build using the SDK/toolchain required by the task:

```sh
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-b3-regression -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON \
  -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-b3-regression
```

For B2 omit `CORE0_STACK_SRAM_HI` (that revision has no such option). Because an archive has
no Git directory, its generated version initially reads `unknown`. The two temporary
`firmware/git_rev.cmake` files were then changed solely to write the original ten-digit
revision strings (`2081cb740b`, `0b89b341ce`), and both were rebuilt. This makes the embedded
identity length comparable; these are investigation builds, not claims of matching the
original measured UF2 hashes. Both iqbench and pvflight built successfully without compiler
warnings. Logs are `/tmp/b3-regression-{b2,merge}-{config,build}.log`.

The map files are:

* `/tmp/b3-regression-b2/firmware/build-b3-regression/iqbench.elf.map`
* `/tmp/b3-regression-merge/firmware/build-b3-regression/iqbench.elf.map`

Their SHA256 values are respectively
`d5d8d903a0e83cb13f336bf6baf8a22b719300fac2ed5706cce7975c73429a96` and
`50c4b958d831420ba0446dcec19fdf855073b61f362222c558dbbea49a80b515`.
The maps include temporary absolute paths, so these hashes are local evidence identifiers,
not portable reproducibility expectations.

One pre-existing CMake ordering trap was found: `if(CORE0_STACK_SRAM_HI)` that sets
`PICO_STACK_SIZE=0x1000` precedes declaration of the option. A first clean configure without
an explicit `-DCORE0_STACK_SRAM_HI=ON` relocated a **2 KiB** stack; explicit ON or a second
configure gives the intended 4 KiB. All final merged addresses below use explicit ON.
This does not explain the measured regression: both stack-placement variants failed, and
the failing writer precedes final JSON printing. The B3 build instructions should keep
the explicit setting.

## What did and did not change

`git diff 2081cb7 0b89b34 -- firmware/src/bench4fec.c firmware/src/iq4ptr.c
firmware/src/pvspi.c firmware/src/iq4hist.c firmware/src/dvbs2_8psk.c` is empty.
The entire live source/credit/publication protocol is unchanged.

The following comparison extracted every nonempty executable section from the two ARM
object files with `arm-none-eabi-objcopy --dump-section` and compared raw bytes before
link-time relocation:

| Object | Identical executable sections / bytes | Differences |
|---|---:|---|
| `bench4fec.c.o` | 4 / 4,956 | none |
| `iq4ptr.c.o` | 15 / 3,048 | none |
| `iq4hist.c.o` | 3 / 604 | none |
| `pvspi.c.o` | 6 / 3,640 | none |
| `dvbs2_8psk.c.o` | 5 / 4,636 | none |
| `dual_dma.c.o` | 26 / 5,492 | six setup sections only |
| `dvbs2.c.o` | 25 / 8,744 | BCH selector/dispatcher, frontend begin/step, new t=8 wrapper |

`dual_dma.c` adds a 16-byte `fifo_targets[2][2]` field at the end of its state and A2's
four-writer routing branch. All pre-existing fields retain offsets. Its changed sections
are `start_output`, `dual_dma_start`, `dual_dma_start_div256`, `dual_dma_start_fifo`,
`dual_dma_start_fifo_div256`, and `dual_dma_start_axis_fifo`. With the two-writer
axis-FIFO selector used by B2, no router runs, no extra DMA transfer occurs, and the
configuration still calls the same `axis_fifo_dma` helpers. Runtime fault/stop/capture
sections are unchanged. A2 changes source layout and setup phase, not the live two-writer
DMA graph.

### DMA claim order and scheduling

On a fresh idle image (the experiment requires free PIO1), SDK lowest-free claims yield
the same channel numbers in both revisions:

| Channel | Owner | `CTRL.HIGH_PRIORITY` |
|---:|---|---:|
| 0 | PV-SPI RX | 0 |
| 1 | on-chip PV-SPI emulator TX | 0 |
| 2 | PV-SPI emulator read-address reset | 0 |
| 3 | I code-ring input | 1 |
| 4 | I credit reload | 1 |
| 5 | Q code-ring input | 1 |
| 6 | Q credit reload | 1 |
| 7 | I RX pointer control | 1 |
| 8 | Q RX pointer control | 1 |
| 9 | I two-word sample row | 1 |
| 10 | Q two-word sample row | 1 |
| 11 | sparse capture | 0 |
| 12–15 | free | — |

The code order is PV-SPI first, pointer prepare next (input/credit per axis), then
dual-DMA control-I/control-Q/data-I/data-Q/capture. Claim order did not change between
the compared revisions. The existing engine `schedule=3` would instead claim control-I
7/data-I 8/control-Q 9/data-Q 10/capture 11. That is a sensitivity probe, **not** a
restoration of B2's numbering.

`bench4fec` forces `schedule=0`: both control and sample channels high priority. Existing
engine schedules 1 and 2 respectively make only samples or only controls low priority;
schedule 3 retains both high and interleaves claims. Input/credit channels remain high in
all these cases; PV/capture remain low. Cheaply exposing this existing selector through
bench4fec provides a same-flash experiment. Checked local RP2350 datasheet,
`CH0_CTRL_TRIG.HIGH_PRIORITY`, printed pp1128–1129: this changes DMA issue scheduling,
not crossbar manager priority. BUS_PRIORITY mask 13 is identical in both original runs
and should remain fixed during these probes.

### PIO/SMs, instructions and IRQs

Both revisions allocate exactly the same resources for this path:

| Block | SMs in lowest-free claim order | Program words |
|---|---|---:|
| PIO0 | SM0 I writer, SM1 Q writer | 2 + 2 = 4 |
| PIO1 | SM0 PV RX, SM1 CS/READY, SM2 emulator, SM3 capture | 4 + 5 + 10 + 3 = 22 |
| PIO2 | SM0 I pointer, SM1 Q pointer, SM2 GPIO28 DDR clock | 10 + 2 = 12 |
| Total | 9 / 12 SMs | all blocks below 32 words |

Writers retain the paired side-set programs, shared 14-bit table rows, joined TX FIFOs,
initial OSR pulls and PIO1 cross-block atomic start of writer/DDR SM masks. There is no
change in program instructions or clock parity for this path. PIO1 IRQ0 exclusively
services PV-SPI CS; the handler is installed on core 0 before the encoder starts on core
1. Neither revision changes its IRQ priority. All pointer/output DMA IRQ enables are
cleared on every supported IRQ line; `IRQ_QUIET` raw status is polled for bus/null-trigger
faults. No new active DMA interrupt is installed by A2. USB stdio and SDK timer background
service remain present in both builds (USB task interval default 1 ms).

For bandwidth accounting, a 5/6 frame carries 6,720 GCS payload bytes in 22,194 symbols.
PV messages carry 1,316 payload bytes in 333 wire words; RX and emulator TX each transfer
333 words/message, and the emulator reset adds one transfer per 16 messages. The steady
transport average is `(6720 / (1316 * 22194)) * (666 + 1/16)` =
**0.1532472836 DMA transfers and FASTPERI accesses per symbol**, or 1.225978269 M/s at
8 Msym/s. This excludes CPU IRQ/register setup and polling. Credit reloads add
`2 / (16 * 512)` = **0.000244140625** transfers/accesses per symbol. For this command's
**pins32** capture, the sampler changes its IN instruction to 32 bits: one DMA transfer
is one full observation, every seventeenth 16-bit bus slot. Capture therefore adds
`8/17` = **0.4705882353** transfers/accesses per symbol while active, 3.764705882 M/s at
8 Msym/s, or 2.941176% of SYS128. The halfword-pair rate of the legacy capture API does
not apply to this path.

### Arena addresses

Every large live arena and the four-level credits/live context stay fixed:

| Live allocation | Both images: address / bytes |
|---|---|
| four-level table (`ring_buf`) | `0x20000000` / 131,072 |
| I code ring | `0x20020000` / 16,384 |
| Q code ring | `0x20024000` / 16,384 |
| PV-SPI slots (`link_ring`) | `0x20028000` / 32,768 |
| PV emulator (`cfg.tq = tab_q`) | `0x20040000` / 32,768 arena |
| 8PSK mapper/Gold (`cfg.ti = tab_i`) | `0x20048000` / 32,768 arena |
| BBFRAME (`bb_buf`) | `0x20050000` / 8,108 |
| sparse capture (`cap_buf` prefix) | `0x20051fac` / 16,384 borrowed |
| signed-plane groups (`cap_buf + 4096`) | `0x20055fac` / 32,768 borrowed |
| FEC frame (`fe`) | `0x2006a090` / 8,108 |
| BCH table | `0x20071f38` / 32,768 |
| iq4ptr credits | `0x2007eec0` / 64 |
| bench4fec live context | `0x2007ef00` / 224 |

`iqquadptr`'s unrelated default credits remain `0x20039f40` / 64 in both; they are idle
during this command. `dual_dma` state remains at `0x2003e5f8`, growing 560 → 576 bytes.
`iq4ptr` state moves `0x2003e828` → `0x2003e838` (144 bytes).
PV-SPI statistics move `0x2003f928` → `0x2003f938` (56 bytes).
The `cfg` pointer structure moves `0x20039cd0` → `0x20039cf8` (20 bytes), but its target
arenas above do not move.

| Region | B2 bytes | Merge bytes | Consequence |
|---|---:|---:|---|
| `.data`, start `0x20030110` | 40,448 | 40,488 | +40 B hot code/veneers |
| `.bss`, start `0x20039f40` | 23,208 | 23,224 | +16 B router state |
| minimum heap | 1,024 | 1,024 | 536 → 520 B remaining after heap |
| `.sram_hi`, start `0x20040000` | 258,016 | 258,016 | ends `0x2007efe0` |
| `.scratch_x` | 2,032 | 2,032 | 16 B before core-1 stack |
| `.scratch_y` | 1,744 | 1,952 | +208 B t=8 BCH copy |
| core-0 stack | 2,048 at `0x20081800` | 4,096 at `0x2007f000` | merged gap above `.sram_hi`: 32 B |
| core-1 stack | 2,048 at `0x20080800` | same | unchanged |

Physical stack section allocation includes linker padding; the addresses above are the
runtime `__StackBottom/__StackTop` values. A B3 allocation in SRAM4–7 must preserve the
tiny 32-byte `.sram_hi`/stack gap, or reuse owned arenas.

### Core-1 hot-code addresses

| Function | B2 | Merge | Bytes B2 → merge |
|---|---|---|---:|
| `ldpc_matrix` | `0x20035e88` | same | 1,916 → same |
| `dvbs2_bch` | `0x20036604` | same | 1,032 → 1,064 |
| `dvbs2_ldpc` | `0x20036a0c` | `0x20036a2c` | 716 → same |
| `dvbs2_frame_begin_into` | `0x2003707c` | `0x2003709c` | 296 → same |
| `dvbs2_frame_ldpc_into` | `0x200371a4` | `0x200371c4` | 140 → same |
| `dvbs2_8psk_frame` | `0x20037a80` | `0x20037aa0` | 664 → same |
| `dvbs2_bch160_stream` | `0x20037d18` | `0x20037d38` | 248 → same |
| `pv_gcs_bbframe` | `0x20038394` | `0x200383b4` | 428 → same |
| `cs_isr` | `0x20038540` | `0x20038560` | 684 → same |
| `pvspi_next_packet` | `0x200387ec` | `0x2003880c` | 380 → same |
| SCRATCH_X transpose/map code | `0x20080498` / `0x200805cc` | same | unchanged |

`dvbs2_bch` gains a t=8 selector test; this command fixes kernel 2 and normal 5/6 uses
degree 160, so the new t=8 assembly leaf is never entered. The existing ARM stream leaf
is byte-identical. The wrapper's compiler-generated dispatch/register setup changes;
it can change a frame's traffic phase without being a per-symbol compute bottleneck.
Do not extrapolate a whole-frame cost from source branch counts alone.

The checked datasheet §2.2.3, printed p31, stripes SRAM0–3 and SRAM4–7 on address bits
3:2. A uniform **+0x20** hot-code relocation and **+0x10** BSS relocation preserve those
bits, so the comparison does **not** support a wholesale switch of hot code or arenas to
different SRAM banks. Individual altered instructions and the moved stack still change
traffic timing. The stack-placement A/B already shows the stack change is not necessary
for a stall.

Flash placement changes more substantially:

| Function | B2 | Merge |
|---|---|---|
| `iq4hist_pack_group` | `0x1001d698` | `0x1001de50` |
| bench4fec `encode_frame` | `0x1001ebf4` | `0x1001f3ac` |
| bench4fec `encoder_task` | `0x1001ed68` | `0x1001f520` |
| `bench4fec` | `0x1001edc8` | `0x1001f580` |

The +0x7b8 movement changes flash cache placement/alignment and branch addresses even
though the unrelocated executable section bytes above match. It is a plausible phase
perturbation, not evidence of a measured cache bottleneck. The core-1 encoder wait/task
and core-0 conversion/supervision still run from XIP.

## Ranked hypotheses and discriminating device experiments

1. **Rare short-row DMA handoff latency under coincident competing traffic.** This is
   the best-supported failure mechanism: a 2-word row must recur every 16 SYS cycles per
   axis; only output TXSTALL fires, with substantial input runway and valid transport.
   The merge may change the phase of CPU/USB/SPI traffic enough to expose a tail event.
   At SYS128, keep cpw2, mask13, 60 s, same capture and ingress. Compare existing schedule
   0, 1, 2 and 3, then alternating writers. Schedule sensitivity identifies internal DMA
   issue/claim-order margin; an alternating-only improvement supports deadline runway.
   It does not prove an arbitration maximum or remove the need for a longer soak.

2. **Changed XIP location or C2 BCH dispatch changes the competing CPU traffic phase.**
   The shared arenas are fixed and relocated SRAM instructions preserve bank phase;
   XIP addresses differ, and `dvbs2_bch` changes. Build two temporary counterfactuals from
   the merge: restore only `firmware/src/dvbs2.c` from B2 for one; restore only
   `firmware/src/dual_dma.c` from B2 for the other. Both are valid for bench4fec kernel 2;
   retain the new stack location and independent runner. Archive maps, run each at
   `bench4fec 2 60000 0 13` after a fresh reboot, and repeat A/B/A/B several times. A
   reproducible recovery by one revert narrows the perturbation to C2 code placement or
   A2 setup/flash placement. The restore changes several linked addresses, so it narrows
   the cause rather than proving a single cache set or instruction. Follow with deliberate
   16-byte/32-byte XIP section padding if that narrowing is observed.

3. **Capture/USB background activity creates the rare tail rather than a constant
   average deficit.** The recorded 60 s failures occur 10–21 s **after** the mid-run sparse
   capture, so capture start alone is not the trigger. Compare cpw2/mask13 with capture
   enabled versus a lab build that suppresses only the capture launch; also move capture
   timing from halfway to a quarter without altering its stride/count. If failures track
   launch they implicate capture; if they persist far from it they do not. A lab USB
   quiet/background-service variant can similarly distinguish USB interference, but must
   restore USB service for final output and keep the watchdog/ownership supervisor.

4. **The apparent image regression is an undersampled stochastic difference.** B2's
   6×60 s clean is evidence of those six runs, not a zero-failure guarantee. Randomize
   flashing order and repeat both original images with fresh boots, identical host
   capture/USB conditions and at least several 60 s runs each before attributing a
   pass/fail to one source change. A common nonzero failure rate weakens an image-specific
   claim. Preserve every failed record.

Eliminated as an explicit changed setting: B2 channel numbering, output HIGH_PRIORITY,
PIO/SM assignments, DMA IRQ enables, pointer program, table/ring/credit addresses, and
SPI setup. A schedule-3 success is a useful new schedule, not evidence that A2 had changed
B2 numbering. The core-0 stack move is also not a necessary cause, because merged
SCRATCH_Y runs stall. The DWT wrap at 33.55 s changes accounting counter values only:
stage deltas use unsigned subtraction and streaming deadlines/credits use timer/word
counters, with no wrap-sensitive near-40-second threshold found on this path.

For the coordinator's counterfactual build experiment, use `git archive 0b89b34` into two
fresh temporary directories, then (from the real worktree) use `git show
2081cb7:firmware/src/dvbs2.c > /tmp/<variant>/firmware/src/dvbs2.c` or the analogous
`dual_dma.c` command. Configure with explicit `-DCORE0_STACK_SRAM_HI=ON` and
`-DIQ4HIST_EXPERIMENT=ON`, build using `make build BUILD=...`, and record a clear custom
version identity. Do not copy these counterfactuals into the B3 production worktree.

All of these cpw2 ratios repeat at SYS256 after the coordinating clock branch supplies
its clock/VREG/QMI/SPI support. SYS256 is a separate device qualification and does not
turn the unchanged SYS128 cpw1 one-symbol design into a feasible shared-port budget.
