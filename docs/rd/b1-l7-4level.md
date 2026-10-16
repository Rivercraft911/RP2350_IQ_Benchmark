Task B1 implements an opt-in, hardware-addressed, four-level L7/N4 shaper and a cyclic 8PSK experiment for SYS128 / 8 Msym/s. The common alphabet and 128 KiB table work in exhaustive native tests. The actual rounded table passes the ideal held-DAC ensemble mask model. Sustained output, CPU supervision cost and AFE performance have **not** been measured here; the attached Pico was never accessed. Live SPI/FEC integration remains a proposed next experiment.

The implementation is in [iq4hist.c](../../firmware/src/iq4hist.c), [iq4ptr.c](../../firmware/src/iq4ptr.c), [bench4live.c](../../firmware/src/bench4live.c) and [iq4hist_trial.py](../../host/iq4hist_trial.py). `IQ4HIST_EXPERIMENT=ON` includes the new command and moves the borrowed output ring to a PIO-addressable alignment. The option defaults OFF. Existing source credits, default output formats and autonomous startup remain unchanged in the ordinary build. Compiling the ordinary `dual_dma.c` against its original version produced identical bytes in all 32 executable sections, totaling 13,420 bytes. The new shared-row writer path is compiled only with the experiment option.

Git delivery is blocked by the execution sandbox: `git add` cannot create `/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.git/worktrees/codex-b1-l7-4level/index.lock` (`Operation not permitted`), despite that Git directory being listed as a writable root. No commit or push succeeded. The implementation and this report remain in worktree branch `rd/b1-l7-4level`, based on `8014ed33b9`; this is an incomplete part of the requested delivery, not a committed hardware qualification.

The mathematical reduction follows the existing globally rotated mapper. Let C = cos(π/8), S = sin(π/8), A = (C+S)/2 and B = (C−S)/2. The existing `iq8_input_t` coordinate is A·sign(a)+B·sign(b). Code `a | b<<1` therefore selects `[+C, −S, +S, −C]`. Uniform 8PSK data uses all four coordinate levels. Rotated QPSK, π/2-BPSK PLHEADER symbols, pilots and Gold rotations by multiples of π/2 preserve that alphabet. This was checked against [reference/dvbs2](../../reference/dvbs2/README.md), the existing C 8PSK/QPSK conversion paths and their independent Python PLFRAME references, including normal/short frames and pilots. A global constellation rotation does not change distances or ideal bandwidth.

Each table key has the newest coordinate code in bits 1:0 and successively older codes above it. Each row contains four chronological, unflagged, 14-bit two's-complement samples in two uint32 words. I and Q use the same table; the writers generate IQ_FLAG separately. The memory arithmetic is exact: 4^7 × 4 × 2 = 131,072 bytes. `iq4hist_pack_group()` converts existing signed-plane groups to sixteen chronological two-bit codes per axis using SWAR spreading; it does not reset filter history.

The generator uses the exact 28 Q8 taps of `qpsk_l7_n4_hold_m010_far2` from the frozen [selected pulse manifest](../../results/pulse-design/2026-10-15/selected.json). Their little-endian coefficient SHA256 is `d23d0e76d95169efb8b96466f8db7f81e0b5e3158849f594a419641586ca4796`. Direct nearest Q30 C/S levels are `[992008094, −410903207, 410903207, −992008094]`; the FIR accumulates in int64 and rounds once with floor((sum+2^37)/2^38). This has different rounding from the existing two-plane shaper and is confined to the opt-in experiment.

Native verification uses [test_iq4hist.py](../../host/test_iq4hist.py), its [C bridge](../../host/native/iq4hist_test.c), [test_iq4ptr.py](../../host/test_iq4ptr.py) and [test_iq4hist_trial.py](../../host/test_iq4hist_trial.py). The table test checks every one of 16,384 rows / 65,536 samples against an independent scalar integer FIR, buffer guards, invalid capacities, frozen taps and PRBS packing. Table CRC32 is **0x19bf3c1d** (431963165). Maximum axis magnitude is 6,745 LSB, below 8,192; headroom relative to 8,191 is approximately 1.69 dB. Maximum error relative to the real C/S FIR using the frozen taps is **0.4999421852 LSB**. A conservative bound is 0.5 + max_phase(sum(abs(taps_q8))/256) / 2^31 = **0.50000339944 LSB**. This bound excludes the original pulse-design coefficient quantization and the physical DAC.

Four alternating normal/short QPSK/8PSK reference frames check 69,328 emitted symbols through the existing C signed-plane mappers/converter and continuous shaping. Frame boundary positions are 22,194, 55,476, 60,966 and 69,336 symbols; partial group fills are 2, 4, 6 and 8. The final eight symbols remain unpublished, as required by the existing complete-group stream API. Multiple arbitrary publication partitions produce identical scalar FIR output. This is native boundary evidence, not live VCM firmware integration.

The optional `--spectrum` test audits **this four-level rounded table**, rather than borrowing the older binary-table certificate. Exact overlapping-history covariance gives the iid-coordinate ensemble PSD; an independent orthogonal sign-product decomposition checks that PSD and includes nonlinear rounding terms in receiver error. The amplitude derivative bound certifies between-grid slack over more than 1,048,000 frequencies, including exact mask knots.

| Model of actual rounded table | Upper margin | Lower margin | Far reserve above 40 dB | Ideal-RRC ISI + distortion |
|---|---:|---:|---:|---:|
| Digital, without hold | −1.035085 dB: fail | +0.250000 dB | −1.035085 dB | 1.224266% |
| Ideal per-axis DAC hold | +0.099513 dB: pass | +0.216889 dB | +2.092444 dB | 1.469870% |

Here f_n = f/(R_s/2). Upper comparisons cover [0,6], lower comparisons [0,1.11], and far rejection [1.7,6]. The hold multiplies amplitude by sinc(f_n/8), corresponding to a zero-order hold at four samples/symbol (32 MS/s at the target rate). The held continuous certificate has positive normalized-amplitude upper/lower slacks of approximately 0.000160667 / 0.022245826. Receiver integration at 16,384 and 65,536 points agrees within about 2.4×10^-9 percentage points and reproduces the frozen linear-pulse ISI within 10^-7 percentage points. These are model results for iid uniform coordinate histories with α=0.20 and the repository's Annex A dB-interpolation convention. They do not certify deterministic headers/pilots/coded payload spectra, all reconstruction images, or the actual AFE magnitude/phase/hold response. **This pulse requires the held model; it fails the digital-only mask.** The [original independent pulse audit](../../results/pulse-design/2026-10-15/independent-audit.json) and [design notes](../../results/waveforms/2026-10-15/pulse-design.md) provide the underlying model definitions.

The PIO producer uses two pin-free SMs in PIO2. Each consumes a packed planar source word with right-shifting OUT2, retains a rolling 14-bit key and emits `base | key<<3`. Y holds base>>17, so the table must be aligned to 128 KiB. The ten-word program takes **145 non-stalled PIO cycles per sixteen symbols**, or 9.0625 cycles/symbol. At 8 Msym/s, each SM has sixteen SYS cycles/symbol and uses 56.64% of its instruction budget. This is an exact instruction count, not a measured throughput or CPU load. Its ideal ceiling is 128 MHz / 9.0625 = **14.1241 Msym/s** before arbitration.

Two ENDLESS DMA input channels wrap immutable 4,096-word axis rings. Two RX-control/sample-DMA pairs feed two PIO0 writers; one PIO1 sampler records GPIO0..31 every seventeenth bus word. A third PIO2 SM generates GPIO28 DDR clock using the existing atomic output/clock start and stop. The experiment needs **seven DMA channels**, six SMs in total and 19 PIO instruction words across the three blocks. There are no CPU descriptors or per-sample arithmetic while the cyclic stream runs. Core 0 polls every 128 µs, with a 256 µs experiment ceiling; core 1 has no assigned work. Spin waiting is excluded from the reported supervision fraction. Input cursors count issued reads including prefetch, not physical output. At the unpaced PIO ceiling a 4,096-word source wrap takes about 4.64 ms, comfortably longer than the polling ceiling. Final cursor snapshots are taken after successful abort and before releasing DMA ownership; native lifecycle tests cover delayed retirement and abort failure.

The first-principles bandwidth budget distinguishes DMA transfers from accesses to the shared peripheral port:

| Work per symbol, both axes | DMA transfers | FASTPERI accesses | At 8 Msym/s |
|---|---:|---:|---|
| Four sample words, SRAM → writer FIFOs | 4 | 4 | 128 MB/s SRAM reads and 128 MB/s FIFO writes |
| Two pointers, RX FIFOs → DMA trigger registers | 2 | 4 | 64 MB/s FIFO reads and 64 MB/s register writes |
| Two packed input words per sixteen symbols | 0.125 | 0.125 | 4 MB/s SRAM reads and 4 MB/s FIFO writes |
| Total, excluding capture/USB/SPI/polls | **6.125** | **8.125** | **49 M DMA transfers/s; 65 M FASTPERI accesses/s** |

The RP2350 local datasheet was checked: §2.1 printed p.24 lists PIO0/1/2, USB and DMA control registers behind one shared fast-peripheral port; §2.1.1 printed p.25 limits a zero-wait subordinate to one access/system clock. Thus the target consumes at least **50.78125%** of that port's 128 Maccess/s ideal budget. Capture temporarily adds 64 Mbus-word/s / 17 = 3.7647 M FIFO reads/s, or 2.94 percentage points, and takes approximately 1.088 ms for 4,096 observations. Arbitration, DMA chain handoff and FIFO runway still need device testing; averages alone cannot prove stall-free operation.

At 16 Msym/s the same design needs **130 M FASTPERI accesses/s**, already beyond 128 MHz before other traffic, and the PIO program needs more than the available eight cycles/symbol. Removing a PIO instruction cannot fix the shared-port ceiling. The existing successful QPSK pointer engine amortizes one pointer over four symbols; this table's alphabet expansion forces one pointer per symbol. Grouping two outputs would require 4^8 × 8 × 2 = 1 MiB; grouping four would require 4^10 × 16 × 2 = 32 MiB. Such full-history tables do not fit SRAM. More than 16 Msym/s N4 also conflicts with the adopted integer SYS128 bus timing; the AFE limit independently gives R_s ≤ 65/4 = 16.25 Msym/s. No 16-Msym/s B1 claim is made.

The final linker map proves the table fits alongside the existing complete firmware by **reusing** the 128 KiB output ring. It does not add a second table allocation:

| B1 memory allocation | Address / bytes |
|---|---|
| Shared LUT, alias of `ring_buf` | 0x20000000–0x2001ffff, 131,072 B, SRAM0–3 |
| I source, first half of `in_buf` | 0x20020000–0x20023fff, 16,384 B |
| Q source, second half of `in_buf` | 0x20024000–0x20027fff, 16,384 B |
| Existing `link_ring` | 0x20028000–0x2002ffff, 32,768 B |
| Lower-RAM data / BSS (`iqbench`) | starts 0x20030110 / 0x20039f40; 40,448 / 23,160 B |
| Reserved minimum heap (`iqbench`) | 0x2003f9b8–0x2003fdb7, 1,024 B; **584 B** beyond it |
| Existing `.sram_hi` | 0x20040000, 257,680 B; **4,464 B** free |
| Scratch X / Y, including their 2 KiB stacks | **16 / 304 B** free |

The experiment also links `pvflight`, but that target has only **40 B** beyond its reserved minimum heap. Use **iqbench.uf2** for this experiment. Lower-RAM code/data and the DMA table still share four striped banks; scratch placement is unchanged. Moving the ring first avoids wasting 128 KiB on alignment, but changes the experiment image's addresses and bus traffic. No existing FEC placement or load measurement can establish the new image's performance. The NOLOAD ring is filled before any experiment consumer starts; ordinary transmission rebuilds its own output ring before use. No new allocation is needed for the 16 KiB capture, which borrows existing `cap_buf`.

The separate plane-to-code conversion disassembles to 43 Cortex-M33 instructions (four LDRH, two STR, eighteen ORR, sixteen AND, two LSL and BX), with no stack. Using the timing rules in [rp2350-notes.md](../../docs/rp2350-notes.md), an SRAM-resident version is estimated at **45–60 cycles per sixteen symbols**, or 2.8–3.75 cycles/symbol, before contention: 17.6–23.4% of one core at 8 Msym/s. The current function runs from flash only during cyclic-source preparation. Live integration should fuse the packing with mapping or put that hot work in SRAM. Host test durations are not M33 timing evidence.

Build and reproduce natively from this worktree (no USB access):

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-b1 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON
make build BUILD=firmware/build-b1
python3 host/test_iq4hist.py --spectrum
python3 host/test_iq4ptr.py
python3 host/test_iq4hist_trial.py
make check
```

Both default and B1 builds completed for iqbench/pvflight without compiler warnings. The complete `make check` suite passed with exit status 0, including all three new native tests; the separate `--spectrum` audit also passed. Markdown links and `git diff --check` passed. The standalone spectrum test prints the CRC, exhaustive error bound, frame-boundary checks and held-mask certificate described above. The PIO test checks all 16,384 keys at four SRAM bases, 12,288 seeds, 72 streams, FIFO delays, five cyclic laps, 18 DMA ring cases, 16,384 writer/clock slots and 38 abort lifecycle cases. Eight runner tests reject corrupted data/flag/spare/clock samples, incomplete/reordered transport, bad source identity, wrong clock/build metadata, nonfinite accounting and incomplete cleanup.

For the coordinating engineer only: load `firmware/build-b1/iqbench.uf2` using your normal flashing procedure. The locally built image identifies itself as `8014ed33b9-dirty`; UF2 SHA256 is `d5d7c20861728d2aa95c1a627a64f246a62477ef5c38cb60f52208471d1437fb`, ELF SHA256 `ae7cc374c0b81c497b621df429b65a6853392f9230ab61cfc5e819aa9e61fe57`. Rebuilding after a commit changes the embedded revision and hashes. Pin the actual build's revision, and use fresh output directories:

```sh
# Slower control: 4 Msym/s N4, 16 MHz GPIO28 DDR clock.
python3 host/iq4hist_trial.py --ms 3000 --cpws 4 --inverts 0 1 \
  --build firmware/build-b1 --expected-git 8014ed33b9-dirty \
  --out results/b1-four-level/control-local

# Target and inverted-clock negative control: 8 Msym/s N4, 32 MHz DDR.
python3 host/iq4hist_trial.py --ms 60000 --cpws 2 --inverts 0 1 \
  --build firmware/build-b1 --expected-git 8014ed33b9-dirty \
  --out results/b1-four-level/target-local
```

The equivalent device lines are `clock 128000`, `bench4live 4 3000 0`, `bench4live 4 3000 1`, `bench4live 2 60000 0` and `bench4live 2 60000 1`. The runner verifies the opt-in CMake flag before opening the device and archives build configuration, linker script, firmware identity, source hashes, binaries and captures. Positive pass criteria are `classification=digital_live_clean`, `faults=[]`, zero pointer/output/stall errors, expected source issue rate, unchanged source CRC, correct table CRC, complete elapsed duration, all **4,096 bus observations** matching the independent cyclic scalar FIR at one fixed stride-17 offset, and zero GPIO28/IQ_FLAG mismatches. Inverted runs must keep the same correct data but show **4,096/4,096** clock-parity mismatches and `classification=clock_inversion_detected`, with no other faults. Both controls must exit successfully only when their expected condition is detected. A short passing capture is not proof of every transmitted sample; 60 seconds is not a temperature or power soak. If the target stalls while cpw4 passes, retain both records and investigate short-row DMA handoff/arbitration rather than relaxing validation.

Live FEC can reuse the existing staged encoder with a different output service. At 8 Msym/s a normal pilot-enabled 8PSK 5/6 frame has 22,194 symbols and lasts 2.77425 ms, giving 355,104 cycles/core. Previously measured approximately 114k BB+BCH, 111k LDPC and 48k packed 8PSK mapping cycles total approximately 273k cycles/frame before ingress, packing and supervision. That arithmetic is a planning estimate for this image, not a new encoder measurement. BB data capacity would be (53,840−80)×8e6/22,194 = **19.378 Mb/s** in nominal 1.2×8 = **9.6 MHz**; this excludes application overhead and does not establish RF bandwidth.

The next integration should replace the cyclic ENDLESS source with bounded paired-axis publication credits and guarded reuse; never overwrite either axis while its input DMA can still read it. Convert only complete sixteen-symbol groups, publish I and Q together with release ordering, preserve the existing mapper's partial group across frames, and retain six coordinate symbols of PIO history across headers, pilots, mode changes and ring wraps. Each PLFRAME still resets its own Gold sequence and signals its MODCOD/VCM MATYPE. Stop/restart must rebuild from known history. Add device captures spanning both QPSK→8PSK and 8PSK→QPSK boundaries, including the following 90-symbol header, and compare against the same continuous reference. Native boundary tests here establish the representation but not that producer/consumer ownership protocol.

The existing SPI receiver must move from PIO2 to PIO1 for that integration: its three SMs plus the capture SM fit all four PIO1 SMs and 22 instruction words. PIO0 retains two writers; PIO2 retains two pointers plus clock. Seven present DMA channels plus three SPI channels and two future credit channels would use 12/16. Existing `link_ring` can hold the SPI slots; `cfg.ti` can hold 5,536 B of normal 8PSK Gold state plus 16,384 B of VCM scratch, and `cfg.tq` can hold the 21,312 B on-chip SPI emulator, as in the existing live experiment. Existing capture space leaves about 40 KiB for staging/context when no CPU sample ring is needed. Put new live state in those explicitly owned arenas rather than consuming the few remaining lower-RAM bytes. Assigning BB/BCH and conversion to core 0 and LDPC/mapping/ingress to core 1 is worth measuring; do not infer schedulability from the standalone stage sums.

Rejected approaches and remaining risks are explicit. A second static 128 KiB LUT cannot fit the current nearly full firmware; borrowing and aligning the existing ring is essential. Reusing the old one-bit pointer program would generate the wrong four-level history. A 16-Msym/s one-symbol-row path fails both the present PIO cadence and the stronger shared-port bound; extra writers do not remove those accesses. A digital-only claim for this hold-designed pulse is false. The cyclic producer intentionally has no live ownership/credits, so it must not be repurposed for mutable input. B1 output cleanup inherits the existing output backend's unbounded DMA-abort wait; the new pointer cleanup has a 1 ms timeout and retains claims on failure. Fault stop also inherits the board-level clock-permission limitation described in [dac-clock.md](../../docs/dac-clock.md). Physical pad timing, SN65 delay, AFE latching, actual hold response, jitter, spectrum/EVM, reconstruction images and receiver acquisition remain unmeasured. The approximately 0.10 dB modeled mask reserve is tight and is not a substitute for those measurements.
