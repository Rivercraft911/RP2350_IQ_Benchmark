# Q1: QPSK on the hardware-shaping engine (`dualptr`)

Branch `worktree-agent-a9e0518b97ffb696d`, from `main` `0296275`. No Pico was touched: nothing
here is a device measurement of new code. Measured numbers are quoted from existing records,
named in each table; every other number is an instruction-count or scaling estimate, and
[q1_budget.py](q1_budget.py) reproduces all of them from those records.

## Findings

1. **Pigeon Vision on `dualptr` needs no firmware change.** `dualptr 2 <ms> 5 0 5` (QPSK 2/3 TS,
   8 Msym/s) was already accepted by the firmware and runner but had never been exercised. Its
   frontend, BB slots and LDPC path were checked line by line and now have native coverage.
   Estimated load is core 0 about 21% and core 1 about 43%, against 78.8% and 66% measured
   for today's `pvflight` path. No device run of code 5 on `dualptr` exists yet; commands are
   in section 5.
2. **Moving `pvflight` onto `dualptr` is not small, so it is designed, not built.** The flight
   profile needs indefinite runs, external SPI start-up, 64-bit publication counters, a
   non-fatal input policy and USB service inside the 256 µs supervision bound. One finding
   makes the move necessary anyway: today's `pvflight` path drives its clock on CLK_IO (GPIO16),
   but the Yapogee board takes DACCLK from GPIO28 through the SN65LVDM31
   ([DAC clock](../dac-clock.md)). Only the `dualptr` engine drives GPIO28 (`dualclock 1`).
3. **The extra TS cost at 16 Msym/s is the per-packet CRC-8** (EN 302 307-1 §5.1.4).
   Measured: TS BB building costs 75.1k cycles/frame against 37.2k for GCS, a gap of 982 cycles per
   188-byte packet (864 at priority mask 0). The byte-serial CRC-8 loop, as inlined in
   `pv_bbframe`, counts to about 858 cycles per packet (18 cycles per 4 bytes). Two opt-in fixes
   are built and natively verified:
   - `pvkernel 1`: a fused adapter, with CRC-8 by four tables, the copy and the byte swap in one
     SRAM leaf. It is estimated to save 7–12 points of core 1 (97.0% → 85–90%).
   - `pvkernel 3`: the same adapter on core 0 in bounded steps, leaving core 1 only LDPC and
     mapping. Estimated core 1 69%, core 0 62–67%.
4. **Frame-parallel FEC would not balance QPSK at 16 Msym/s better than moving ingress.** It is
   estimated at 72–87% on core 0 for GCS (75–90% for fused TS), against 62–67% and 69% for `pvkernel 3`.
   It needs bounded reentrant LDPC on core 0 (+36–43%, measured for the 2/3 code in C4), duplicate
   BCH and map placements, and ordered publication. Not recommended for QPSK.

## 1. QPSK 2/3 TS at 8 Msym/s on `dualptr`

Profile: normal 2/3, pilots, 33,282 symbols per frame, N4, L8 pointer table, two pointer-fed
writers, GPIO28 DDR clock at 32 MHz (cpw 2). BB capacity is (43040 − 80) × 8e6 / 33282 =
**10.326 Mb/s**, and the 9 Mb/s TS uses 87% of it. The input is TS, as `pvflight` uses; GCS is
the control.

### What it needs, checked against the code

| Item | Finding |
|---|---|
| Command | `main.c` accepts `dualptr cpw ms source fault 5`; `trial_code` maps 5 to `normal 2/3`, Kbch 43040. Rejects only 6/7. |
| BCH frontend | t = 10, degree 160: the bounded five-word continuation (`bchkernel 2`; kernel 3 is identical for degree 160), scratch-Y copy with `bchbank 1`. Kbch = 1345 whole words, no tail bytes; 15 slices (14 of 96 words, one of 1). |
| LDPC | Kldpc 43200 = 1350 whole words, q = 60: `dvbs2_ldpc` writes parity in place via `ldpc_pack60` (`ldpcpack 1` is required by `dualptr` but has no effect at q = 60). |
| BB slots | 5328 emulator words + 2 × 1345 = 8018 ≤ 8192 words: both slots fit in the borrowed Q table (5/6 and up use `bb_buf` for slot 1). |
| SPI prefill | 6 BBFRAMEs = 32,220 bytes; the first two (10,740 bytes) come from the 12 prefilled messages (15,792 bytes). The rest arrive during the prefill encodes. |
| DDR clock | `dualclock 1`: GPIO28 at SYS/(2 × cpw) = 32 MHz; the runner checks GPIO28 == IQ_FLAG in all 4096 snapshots. |
| Bus priority | At cpw 2 each writer chain needs an 8-word row every 64 SYS cycles per axis. The measured DMA row loop is 10–18 cycles ([dmaloop](../../results/dmaloop/2026-10-15/README.md)). 5/6 ran 60 s clean at mask 0 ([dualptr](../../results/dualptr/2026-10-15/README.md)); mask 13 was needed only at 16 Msym/s. Run both. |
| Runner and reference | `host/dualptr_trial.py --code-index 5` builds the 2/3 TS/GCS references (`dual_spi.bbframe(rate='2/3')`), checks `kbch`/`s2_code` and the first four BBFRAME CRCs. |

### Budget at 8 Msym/s (estimate)

The frame budget is 532,512 cycles per core. The table scales the measured 5/6 TS `dualptr` run
(A1, mask 0) by the isolated 2/3 : 5/6 stage costs (results/optimizations/2026-10-15).

| | Cycles/frame | Share |
|---|---:|---:|
| Core 0: BB copy + BCH (5/6 live 120.2k × 91.9/113.9) | 97.0k | 18.2% |
| Core 0: pointer supervision (measured, independent of code) | — | 3.2% |
| **Core 0 total** | | **21.4%** |
| Core 1: LDPC + map (isolated 178.4k × measured live factor 1.014) | 181.0k | 34.0% |
| Core 1: TS BB build (measured 9.27 cycles/byte × 5380 bytes) | 49.8k | 9.4% |
| **Core 1 total** (GCS ≈ 38.8%; with `pvkernel 1` ≈ 40–42%) | | **43.3%** |
| `pvflight` today, `pvtx` 2/3 8 Msym/s (measured, results/dual-dma/2026-10-15/regression-2) | | 78.8% / 66% |

Cross-check: the estimated 49.8k-cycle TS build matches the 49,813 `bb_cyc_per_frame` measured
for the same adapter in `pvtx`.

### Native verification added

- `host/test_dualptr_native.py`: code 5 now runs through the real prefill, BB-queue and
  frontend leaves, the split backend and the finite-credit model. TS and GCS, pilots on and
  off, and the grant freeze all give exact words against independent Python.
- `host/test_dualptr_trial.py`: code-5 fixtures at cpw 2 through the real references. Positive,
  inverted-clock and starvation controls classify correctly. A 5/6 capture relabelled as 2/3 is
  rejected. The runner sends `dualprio 13`, `dualptr 2 60000 5 0 5` and the matching
  `dualstream` control.

## 2. Moving `pvflight` onto `dualptr` (design; not implemented)

What changes between `pvtx` (`tx.c`, `iqout.c`) and `dualptr` (`bench_dual.c`, `dual_dma.c`):

| Aspect | `pvflight` today | `dualptr` today | Needed for flight |
|---|---|---|---|
| Shaping | CPU, L12 `lut_asm_p`, core 0 78.8% | PIO/DMA L8 table (`qpsk_l8_n4_both_m010_far2`) | L8; a new pulse, see re-qualification |
| DAC clock | CLK_IO on GPIO16 | GPIO28 DDR (`dualclock 1`) or HSTX GPIO16 | GPIO28, as the Yapogee board requires |
| SPI | PIO2, slots in `in_buf`, external master | PIO1, slots in the I table, on-chip emulator only | external master on PIO1 |
| Start-up | encode nulls, start output, then arm READY | wait for 12 emulator messages, then prefill | nulls first, READY last, as `pvtx` does |
| Duration | until `stop` (`ms = 0`) | 100–60,000 ms; 32-bit µs deadline (wraps at 71.6 min) | until `stop`; 64-bit time |
| Publication count | — | `encoder.total` (uint32) passed to `iqquadptr_publish(uint64)`, which treats a decrease as an ownership error and halts (`iqquadptr.c`): fails after 2^32 words = 2.39 h at 8 Msym/s | 64-bit accumulator of 32-bit deltas |
| Input errors | counted; nulls fill gaps | any lost/bad message sets `input_failed` and stops | count and continue (TS nulls); policy decision |
| Output faults | ring underruns counted | stop on any pointer/output/credit/poll fault | stop and restart policy: a requirements decision |
| USB service | `flight_service` once per ms on core 0 | none during the run | inside the 128 µs poll loop; the measured max gap is already 235–249 µs of the 256 µs limit at both 8 and 16 Msym/s, because it is set by the longest work item (the BB copy), not by load |
| Status | `tx_status_json` | final JSON only | status from pointer/encoder/PV-SPI counters |

Proposed structure: split `bench_dual_pointer_writers` into setup, run loop and teardown with a
small config (emulator or external SPI, ms or until stop, capture on or off, fault policy). Add
a CMake option, off by default, that makes `pvflight`'s `flight_start` call the until-stop
variant with the stored profile (2/3, pilots, cpw 2, L8, `dualclock 1`, a chosen priority mask).
`PVTX` stays the default.

Native work: a long-run counter model starting near 2^32 words, null-only start-up then late
SPI arrival, message loss with continued output, and the stop path. This is several hundred
lines across firmware and tests, with start-up ordering and supervision timing that only the
board can settle. That is why it was not built here.

**What would have to be re-qualified**, in order:
1. Pulse: L8 replaces L12. The L8 table passes the Annex A mask in the digital and held-DAC
   models only ([selected.json](../../results/pulse-design/2026-10-15/selected.json):
   "no Pico or RF validation"). Spectrum and EVM after the AFE, and receiver decode, are open.
2. Clock path: GPIO28 through the SN65LVDM31 at 32 MHz, setup/hold at the AFE, and the SN65
   disable-before-clock-stop rule, for which no board firmware exists yet ([DAC clock](../dac-clock.md)).
3. Bus timing: the data bus is unchanged (D0–D13 on GPIO0–13, IQ_FLAG on GPIO14 in both engines,
   `config.h`), but I and Q are now latched on the rising and falling edges of GPIO28 instead of
   being clocked by CLK_IO on GPIO16.
4. PV-SPI with the external CM5 on PIO1: READY semantics, 120 s continuity, disconnect and
   reconnect ([autonomous startup](../autonomous-startup.md) lists these as still unmeasured).
5. Supervision timing with USB status traffic: max poll gap, and stack high-water (`stackcheck`).
6. Multi-hour soak past the 32-bit counter and timer wraps.
7. Bus priority mask for flight (0 measured clean for 5/6 at 8 Msym/s; 13 adds output margin).

## 3. TS input at 16 Msym/s

### Where core 1's extra 14 points go

Measured, 9/10, 16 Msym/s, `dualptr`, mask 13 ([round 2](../../results/round2/2026-10-15/README.md),
`q1-ptr2-910k3-p13-60s`; 28,851 frames in 60 s = 480.85 frames/s):

| | Core 1 | BB build (`ts_busy`) | Cycles/frame |
|---|---:|---:|---:|
| GCS | 82.8% | 13.96% | 37.2k |
| TS | 97.0% | 28.21% | 75.1k |
| Difference | 14.2 points | | **37.9k = 982 per packet = 5.25 per CRC byte** |

The same difference at mask 0 (`q3`, CPU descriptors) is 864 cycles per packet. The only per-packet work that
TS does and GCS does not is `next_packet`'s `pv_crc8(p + 1, 187)`, writing the result into the
next packet's sync byte. Disassembled (GCC 14.2, `-O3`), the loop inlined in `pv_bbframe` is
14 instructions plus the taken branch per 4 bytes: 4 data loads, 4 dependent table loads,
4 EORs and loop control. Two load-use stalls make it **18 cycles per 4 bytes, about 858 per
packet** (timing rules: RP2350 §3.7.4.9, pp.137–138), close to the 864 measured at mask 0. The
standalone `pv_crc8` schedules without stalls (16 cycles per 4 bytes, about 800 per call). Two
idle `pvbench` probes of it on one older image read 803 and 1006 cycles
(results/dual-dma/2026-10-15/development-probes; the second probe's conditions are not
recorded). The rest of the mask-13 gap is contention: the code
and the 256-byte `crc8_t` sit in striped SRAM0-3 (0x2003f1e0), where the shaping DMA reads
256 MB/s, and under mask 13 core 1 has the lowest bus priority. Both TS and GCS also pay an
unaligned byte copy and a separate per-word swap pass, the latter in XIP flash
(`build_spi_frame` at 0x100198c4).

Hardware CRC is not available: the DMA sniffer computes only CRC-32, CRC-16-CCITT, XOR
reduction and a sum (RP2350 §12.6, SNIFF_CTRL.CALC, p.1136), and polynomial 0xD5 is none of them.

### What was built (opt-in; defaults unchanged)

- `pv_bbframe_words` (`pvproto.c`): writes exactly the words that `pv_bbframe`, tail zeroing and
  the swap pass produce, with the same `pv_stats` updates, nulls and in-place sync bytes. Each
  word-aligned source word is read once:
  - CRC-8 by slicing by four: from state c, bytes b0..b3 give
    T3[c ^ b0] ^ T2[b1] ^ T1[b2] ^ T0[b3], since the byte step c → T0[c ^ b] is linear.
  - The sync byte is seeded with the previous CRC, so every byte takes one uniform update and
    unaligned frame edges need no special case.
  - REV, then a funnel shift into the frame's byte phase (the phase is constant within a frame
    because 188 ≡ 0 mod 4).
- Begin/step form (`pv_bbframe_words_begin/step`): at most N packet chunks per call, for a
  caller with a polling deadline.
- `pvproto_asm.S`: the per-word leaf, position-independent (no literal pool, no external branch).
  It is 332 bytes and 19 instructions per word (17 at phase 0), costing 21 cycles per word (19)
  with one load-use stall and the taken branch. `pv_words_prepare` copies it into a caller arena.
- `dualptr ... [code] [pvkernel]`, TS only:
  - 1 = fused adapter on core 1, leaf copied into the unused side of the 64 KiB pointer-table
    arena in `ring_buf`. That is striped SRAM0-3, where core 1's other hot code already runs.
  - 2 = the same with the portable C leaf from flash: a placement control and an independent
    on-device correctness check of the Arm leaf.
  - 3 = fused adapter on core 0 (`ingress_step`, four packet chunks per step between polls).
    Core 0 then produces and consumes the BB slots; core 1 runs LDPC and mapping only. The
    FE-slot handoff is untouched.
  - The JSON adds `pv_kernel`, `pv_leaf_base`, `ingress_core`, `core0_ingress_busy`,
    `ingress_steps` and `ingress_max_step_cyc`. `core0_work_busy` includes the ingress share.
- `pvbench reps [kernel]`: idle, interrupt-masked cycles of the old adapter plus swap pass
  against the fused one, for 2/3 and 9/10, plus a 16-frame equality check.
- `host/dualptr_trial.py --pv-kernel {0..3}`: sends the argument only when nonzero, so default
  lines are unchanged. It fails closed on the reported kernel, the leaf placement (not in the
  pointer table or input ring) and, for kernel 3, proof that core 0 built the runtime frames
  and core 1 none (`ts_busy` 0).

Why the leaf goes in SRAM and not XIP: core 0's `dualptr` supervision loop runs from XIP and,
under mask 13, has priority over core 1 at the shared XIP port. Kernel 2 measures that choice.

Memory: no static storage and no SRAM code were added. The context (1,056 bytes) uses the head of
`cap_buf`, which `dualptr` never reads (capture and FE1 follow `IN_WORDS`). The opt-in flag
uses a padding byte of the existing `encoder` struct, whose size stays 0xd8. RAM sections and
RAM symbols of `iqbench`, the experiment `iqbench` and `pvflight` are unchanged apart from the
order of linker veneers and the stack-size artefact in section 6.

### Native verification

- `host/test_pv_words.py`, built with ASan and UBSan:
  - Against the existing C path: 384 configurations and 14,208 frames, covering all 21 normal
    and short QPSK Kbch, three data lengths that end frames on packet boundaries, four
    source alignments and four missing-packet patterns. Two thirds of the frames are built in
    steps of 0–7 chunks, and an extra step after completion must have no effect.
  - The leaf is held to its aligned-word contract: 98.2% of bytes pass through it.
  - Against the independent Python TS reference: 2/3 for 100 frames (frame 93 ends on a packet
    boundary), and 5/6, 8/9 and 9/10 for 24 frames each.
  - It also computes the expected device `pvbench` `words_crc`: **[656557369, 408789036]**.
- `host/test_pv_words_asm.py`: an instruction-semantics model executes the actual
  `pvproto_asm.S` source:
  - 1,056 cases across all four phases, counts 1–64, arbitrary CRC/accumulator states, exact
    memory bounds and AAPCS registers, against a bitwise CRC-8 and byte-funnel reference.
  - All 95 source instructions execute and equal the 95 instructions the firmware toolchain
    assembles, and the copied section has no relocations or literal-pool loads.
  - This is ISA semantics, not silicon timing.
- `host/test_dualptr_native.py`: every TS case also runs through `pvkernel` 1 and 3, using the real
  `build_spi_frame`, prefill, `ingress_step`, frontend and backend leaves. All 48 cases give exact
  words and the independent first-four BBFRAME CRCs, and no ingress step exceeds four packets.
- Negative controls run during development by mutating the source:
  - Framer: the CRC table order, the CRC seed, the tail word, the alignment test, leaf use and
    the packet-boundary carry.
  - Leaf: table base, shift, extract width, phase dispatch, state store offset and saved
    registers.
  - `ingress_step`: wrong slot, third outstanding slot, missing publish, ignored step result.
  - Every behaviour-changing mutation failed its test. The one survivor, a redundant end-of-frame
    reset, was removed.
- `make check` passes: 101 invocations, 99 before this branch plus the two new tests.

### Expected effect at 16 Msym/s, 9/10 TS, mask 13 (estimates)

The fused path counts to about 1,090 cycles per full packet (47 × 21 + about 100 of framing and
call overhead) plus a 43-cycle per-message share and about 500 cycles per frame: **44.3k
cycles/frame without contention**. The same counting for today's TS build gives 60.7k, against
69.1k measured at mask 0 (1.14×) and 75.1k at mask 13 (1.24×), so the table spans 1.0–1.3×.

| `pvkernel` | Core 0 | Core 1 | Basis |
|---|---:|---:|---|
| 0 (today) | 45.6% | 97.0% | measured, round 2 |
| 1 | 45.6% | **85.4–90.4%** | estimate |
| 3 | **62.3–67.3%** | **68.8%** | estimate; core 1 = measured LDPC+map share |
| GCS reference | 45.6% | 82.8% | measured |

When idle, the fused leaf is not faster than the old CRC plus copy (21 cycles per word against
about 18 + 6.5). The gain comes from dropping the swap pass, moving the table off the DMA-shared
bank, and cutting memory accesses from about 13 to 6 per word. `pvbench` will show the idle
part. Expected for 2/3: about 30–33k cycles fused against about 41k for the old path
(`pv_bbframe` measured 31,986 and 39,936 in the two older probes, plus about 9.4k estimated for
the swap).

## 4. Frame-parallel FEC for QPSK at 16 Msym/s (estimate)

Each core would encode alternate frames: 532,512 cycles per assigned frame. Each core needs its
own BB/FE slots, a reentrant LDPC workspace (q × 12 words: 864 bytes at 9/10, 2,880 at 2/3), an
input turn (the TS adapter state is serial) and ordered publication of the 2-symbol carry
between frames, as C3 did for 8PSK. Core 0 must keep its 256 µs supervision bound, so its LDPC
and mapping must be the bounded C4 forms.

Stage inputs (9/10):

- Live core-0 copy + BCH: 113.2k.
- Core-1 BCH on the five-word striped leaf: +14.5k (C2 estimate), since scratch Y is core 0's.
- Bounded LDPC on core 0: 136.5–143.7k, scaled from the C4 measurement for 2/3 (146.6–154.3k
  against 107.8k serial).
- Private-workspace serial LDPC on core 1: 101.8k.
- Map: core 1 71.6k; core 0 77.7–91.8k (sliced, striped placement).
- Publication 2k; supervision 16.6k per assigned frame on core 0.
- The two-core factor, 1.08–1.15 (ldpc-banks), is applied as an upper bound.

| Input | Core 0 | Core 1 |
|---|---:|---:|
| GCS | 72.0–87.4% | 63.9–73.5% |
| TS, today's adapter | 79.1–95.5% | 71.0–81.7% |
| TS, fused adapter | 74.5–90.3% | 66.5–76.5% |

Against `pvkernel 3` (62–67% / 69%), frame-parallel is worse on the busier core and more work
in total: 136–161% of one core summed over both cores for GCS, against 128% measured today. The current split already uses both cores,
and its imbalance comes from ingress being on core 1, which `pvkernel 3` addresses directly.
What frame-parallel would take is listed above; I recommend not building it for QPSK. The AFE's
65 MSPS limit at 4 SPS caps QPSK at 16.25 Msym/s, so there is no higher QPSK rate for it to
enable.

## 5. Device commands for the coordinator

Build the experiment image (as round 2 did), check, and pin the identity:

```sh
make build
cmake -S firmware -B firmware/build-exp -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DIQ4HIST_EXPERIMENT=ON -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-exp
make check
Q1_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build-exp/git_rev.h)
OUT=results/q1-qpsk/2026-10-16
```

Flash `firmware/build-exp/iqbench.uf2`, then run the steps in order, each into a fresh
directory. The runners neither build nor flash, and they exit nonzero on any unexpected
failure.

**A. Isolated fused adapter (no output path; about 1 s each).**

```sh
mkdir -p $OUT/pvbench
python3 host/iqbench.py raw 'clock 128000'
python3 host/iqbench.py raw 'pvbench 64 1' > $OUT/pvbench/kernel1.json
python3 host/iqbench.py raw 'pvbench 64 2' > $OUT/pvbench/kernel2.json
python3 host/iqbench.py raw 'pvbench 64 1' > $OUT/pvbench/kernel1-repeat.json
```

Pass: `git` = `$Q1_FW_GIT`, `words_ready` 1, `words_match` 1,
`words_crc` = [656557369, 408789036] for both kernels. Record `swap_cyc` and `words_cyc`
(2/3, 9/10). A mismatch with an otherwise passing native suite means the silicon leaf differs
from its model: stop and keep the record.

**B. Pigeon Vision profile, 2/3 at 8 Msym/s (item 1).**

```sh
# 3 s screen: TS and GCS, correct and inverted clock, grant starvation, same-rate control
python3 host/dualptr_trial.py --ms 3000 --clocks 128000 --cpws 2 --sources 5 6 \
  --faults 0 1 --clock-modes 1 2 --code-index 5 --priority 0 --control \
  --expected-git "$Q1_FW_GIT" --build firmware/build-exp --out $OUT/pv23-3s
# 60 s at mask 0 (as A1 measured 5/6) and at mask 13
python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 2 --sources 5 6 \
  --faults 0 --clock-modes 1 --code-index 5 --priority 0 --control \
  --expected-git "$Q1_FW_GIT" --build firmware/build-exp --out $OUT/pv23-60s-p0
python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 2 --sources 5 6 \
  --faults 0 --clock-modes 1 --code-index 5 --priority 13 \
  --expected-git "$Q1_FW_GIT" --build firmware/build-exp --out $OUT/pv23-60s-p13
```

Pass:
- Positive rows classify `digital_live_clean`: full duration, zero output/pointer/input faults,
  all 4096 snapshots match the independent 2/3 reference, GPIO28 == IQ_FLAG everywhere, poll gap
  ≤ 256 µs, credit runway ≥ 1360 words, no read beyond grants, and the first four BBFRAME CRCs
  and SPI CRC chain match.
- Controls classify `cpu_descriptor_control_clean`.
- Negative controls classify `inverted_clock_detected` and `credit_starvation_detected`.
- Record `core0_work_busy` and `core1_work_busy`; expected about 21% and 43% (TS), about 39% (GCS).

**C. TS at 16 Msym/s, 9/10, adapters in A/B/A order (item 2).**

These are the same conditions as round 2 `q1`. Screen each kernel for 3 s with `--ms 3000` and a
fresh `--out` first, then run 60 s:

```sh
for step in 1:0 2:1 3:3 4:2 5:0; do n=${step%%:*}; k=${step##*:}
  python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 1 --sources 5 \
    --faults 0 --clock-modes 1 --code-index 10 --bch-kernel 3 --bch-bank 1 --priority 13 \
    --pv-kernel $k --expected-git "$Q1_FW_GIT" --build firmware/build-exp \
    --out $OUT/ts16-$n-k$k || break
done
# Negative controls on the new paths (3 s): inverted clock and grant starvation must be detected
for k in 1 3; do
  python3 host/dualptr_trial.py --ms 3000 --clocks 128000 --cpws 1 --sources 5 \
    --faults 0 1 --clock-modes 1 2 --code-index 10 --bch-kernel 3 --bch-bank 1 --priority 13 \
    --pv-kernel $k --expected-git "$Q1_FW_GIT" --build firmware/build-exp --out $OUT/ts16-neg-k$k
done
# GCS reference on this image (expected near 82.8% core 1)
python3 host/dualptr_trial.py --ms 60000 --clocks 128000 --cpws 1 --sources 6 \
  --faults 0 --clock-modes 1 --code-index 10 --bch-kernel 3 --bch-bank 1 --priority 13 \
  --expected-git "$Q1_FW_GIT" --build firmware/build-exp --out $OUT/gcs16-ref
```

Pass: every positive row classifies `digital_live_clean` with the criteria above. Both `k0`
rows must reproduce round 2 (core 1 near 97.0%). If they do not, the image changed something
and the comparison is void. Then record:

| Field | Expected (estimate) |
|---|---|
| Core 1 with `k1` | 85–90% |
| Core 1 with `k3` | about 69% |
| Core 0 with `k3` | 62–67% |
| `ingress_max_step_cyc` with `k3` | under about 6k cycles |
| `max_poll_gap_us` | at most 256 µs (the runner enforces it) |

`k2` measures the flash C leaf against `k1`.

The idea fails if:
- `k1` core 1 stays at 95% or more: placement or contention is worse than modelled; compare `k2`.
- `k3` raises the poll gap or credit faults: reduce `PTR_INGRESS_PACKETS`.
- Any waveform mismatch appears with a clean native suite: keep the evidence.

**D. Afterwards:** `python3 host/iqbench.py raw stackcheck > $OUT/stackcheck.json`. Both scratch
code images must match flash and neither paint may be exhausted. The new core-0 call chain is
`ingress_step` → `pv_bbframe_words_step` → `pvspi_next_packet` → `pv_accept` → `pv_crc32`,
plus the leaf's 40-byte frame.

## 6. Risks, dead ends and unverified points

- **Pre-existing build defect.** `firmware/CMakeLists.txt` reads `CORE0_STACK_SRAM_HI` inside the
  target loop before the `option()` that defines it. A fresh `make build` configure gets a 2 KiB
  core-0 stack; any reconfigure gets 4 KiB. The default image therefore depends on configure
  history. Observed here when adding a source file. Fix: move the `option()` above the loop.
  Left for the coordinator, because it changes the default image.
- All load figures for the new code are estimates. The fused leaf's advantage depends on bus
  contention more than on its instruction count.
- `pv_bbframe` and `pv_bbframe_words` keep different mid-packet CRC state: never mix them in
  one stream. `dualptr` selects one per run, before prefill.
- The context and leaf borrow `cap_buf`'s head and the free side of `ring_buf`. That is safe for
  `dualptr` as written, but any future use of those regions in the same command would collide.
- `pvkernel 3` adds work to core 0, whose max poll gap was already 245–249 µs of 256 at
  16 Msym/s. The longest slice is still the BB copy (about 16k cycles), not an ingress step,
  but this is a margin to watch.
- GCS gains nothing from this branch. A word-output GCS copy (no CRC) would remove its swap
  pass too; not built.
- Dead ends:
  - DMA-sniffer CRC: no CRC-8 mode.
  - New SRAM code or tables: 8 bytes are free in `pvflight`'s lower SRAM, 80 in the experiment
    image, and 24 in its SRAM4-7.
  - Leaf in XIP: contends with core 0's supervision fetches.
  - 64 KiB two-byte tables: no 64 KiB free in the default image.
  - Interpolator extraction: no fewer cycles than UBFX.
  - A DSP `UXTAB` single-base variant: same instruction count, frees registers for unrolling,
    perhaps 2 cycles per word; not worth the extra model surface yet.
- Not established: anything about RF, the AFE, external SPI or flight start-up.

## Files

- `firmware/src/pvproto.c`, `pvproto.h`, `pvproto_asm.S` (new): fused adapter, step form and
  leaf.
- `firmware/src/bench_dual.c`: `pvkernel`, `ingress_step` and result fields.
- `firmware/src/main.c`, `bench.h`, `bench.c` (`pvbench`).
- `firmware/CMakeLists.txt`: adds the `.S` file.
- `host/dualptr_trial.py`: `--pv-kernel`.
- Tests: `host/test_pv_words.py`, `host/test_pv_words_asm.py` (both new),
  `host/test_dualptr_native.py`, `host/test_dualptr_trial.py`; `Makefile` runs the new ones.
- `docs/rd/q1_budget.py`: every number in sections 1, 3 and 4.
