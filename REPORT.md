# C2: normal 9/10 QPSK through live SPI/FEC

Implemented, natively verified, and ready for device measurements. Normal **9/10**
(code 10) and **8/9** (code 9) now work through the complete `dualstream` pipeline
and the bounded `dualptr` frontend. `bchkernel 3` adds a four-word t=8 BCH kernel.
No attached Pico, flashing tool, or device serial port was accessed. There are
**no new M33 performance measurements or 16-Msym/s qualification results** here.
Existing result evidence was preserved; Git metadata was not written.

At 16 Msym/s with normal frames and pilots, the requested capacity calculation is
correct: **27.936782645 Mb/s** for 9/10 versus **25.844600685 Mb/s** for 5/6,
an **8.095238%** increase. These are BB data-field capacities, before application
overhead. The four-writer descriptor engine is the primary qualification path;
the existing two-writer pointer engine remains a diagnostic path because A1
measured live-traffic TX stalls at 16 Msym/s.

## Changes and ownership

- `dualstream` accepts normal indices **5, 6, 8, 9, 10**. Its arguments and defaults
  are otherwise unchanged. `dualptr cpw ms source fault [code]` defaults to code 8
  and accepts **5, 8, 9, 10**. Code 6 is deliberately rejected by `dualptr`: t=12
  still has no bounded frontend. Sources 5/6 select TS/GCS; device live commands
  continue to enable pilots.
- Degree-128 frontends now use bounded steps for nonbaseline BCH selectors.
  Selectors 1/2 reuse the existing five-word recurrence with a zero fifth word.
  Selector 3 specializes degree 128 to four words; other degrees retain the
  selector-2 behavior. Baseline selector 0 remains the default.
- On M33, selector 3 plus `bchbank 1` uses a **208-byte scratch-Y** continuation
  leaf. Bank 0 retains the five-word striped-SRAM leaf, making it a placement
  control rather than a four-word comparison. Whole-frame selector 3 uses a
  separate **208-byte XIP** leaf and an XIP orchestration helper. This avoids
  putting new core-1 full-frame work into core 0's scratch-Y instruction bank,
  the contention failure seen in C1. No table or permanent data arena was added.
- Frontend BB copying, slot release, FE publication, backend ownership, continuous
  mapper carry, input-ring credits, DMA lifecycle and writer layout are unchanged.
  `dualstream` now reports `ldpc_pack` and `bch_bank`; `dualptr` reports its actual
  code, Kbch and BCH selector.
- Host runners accept both new codes and explicit BCH/packer/bank choices.
  References validate code index, rate and BB size, and reference windows can
  construct either pilot setting. `dual_trial.py gcs` now treats a failed cpw=1
  target as a campaign failure, rather than allowing an exploratory ceiling
  probe to exit successfully. Existing archived evidence is not rewritten.

Default selector values, startup profile and existing wire formats are retained.
The changed dispatch is not claimed cycle-neutral: the 5/6 control must be
remeasured on this image. New code paths are selected explicitly.

## Frame, ingress and buffer audit

Normal QPSK uses 64,800 LDPC bits, 32,400 data symbols, a 90-symbol PLHEADER and
22 pilot blocks of 36 symbols: **33,282 symbols/frame** with pilots, **32,490**
without. All pilot-enabled normal rates therefore have the same **2.080125 ms**
frame period, **480.740340 frames/s**, and **266,256 cycles/core/frame** at SYS128
and 16 Msym/s. Capacity is `(Kbch - 80) * 16e6 / 33282`.

| Normal code | Index | BCH t / parity bits | Kbch / Kldpc bits | BB bytes / words | BB capacity, Mb/s |
|---|---:|---:|---:|---:|---:|
| 5/6 | 8 | 10 / 160 | 53840 / 54000 | 6730 / 1683 | 25.844600685 |
| 8/9 | 9 | 8 / 128 | 57472 / 57600 | 7184 / 1796 | 27.590649600 |
| 9/10 | 10 | 8 / 128 | 58192 / 58320 | 7274 / 1819 | 27.936782645 |

8/9 has word-aligned Kbch and Kldpc. Both are **16 bits into their final word**
for 9/10. Its two unused BB storage bytes are cleared; its BCH tail consumes two
bytes, and LDPC parity starts at bit offset 16. LDPC q is 20/18 and parity is
7200/6480 bits for 8/9 and 9/10. Both qualify for the existing q<=64 packer.
8/9 uses the aligned destination path; 9/10 uses the unaligned streaming append.
The packer itself needed no change. Both pack settings are compared natively.
The 9/10 DFL of 58,112 bits fits the 16-bit BBHEADER field.

Existing FE slots remain **2027 words / 8108 bytes**. The 8192-word symbol ring
has the same extent and partial-word carry because all normal QPSK rates have
the same LDPC length. Pilots advance the stream's partial fill by two symbols
per frame; no frame-boundary padding is inserted.

The borrowed Q table has 8192 words. The SPI emulator occupies 5328 words
(16 messages x 333 words = 21,312 bytes). One maximum BB slot needs 1819 words:
`5328 + 1819 = 7147`, leaving **1045 words**. Two large BB slots would not fit;
the existing allocation correctly uses `bb_buf` for the second slot. Core 0
copies a BB slot into its owned FE slot before releasing that BB slot. The SPI
receive queue occupies `24 * 333 = 7992` words of its separate 8192-word arena,
leaving 800 bytes; this now has a static assertion. The emulator loops its
16-message / 112-packet pattern, so duration does not require more storage.
Twelve initial messages provide 15,792 payload bytes, enough for two 9/10 data
fields totaling 14,528 bytes. Their ideal startup time is 4.093 ms, below the
10 ms prefill timeout; actual READY latency remains a measurement.

The actual on-chip master has 10,656 bits/message at four SYS cycles/bit and
1030 other cycles (1024 explicit gap cycles plus six control cycles). Its
ideal period is **43,654 cycles / 341.046875 us**, so the payload upper bound is
`1316 * 8 * 128e6 / 43654 = 30.869656847 Mb/s`. At 9/10:

- Required payload arrival is **2653.569780 messages/s**, or one per 376.850840 us.
- Fixed source work uses **90.499%** of that average period; **35.803965 us/message**
  remain for READY waits and other delays. SCK-only occupancy is 88.364%.
- Twenty-four receive payload slots represent about **9.0444 ms** of demand.
  This average buffer/time budget does not prove worst-case scheduling.
- An external one-lane sender with a 10 us gap needs approximately **29.047 MHz**
  SCK. The previously demonstrated external 25 MHz case caps at **24.1335 Mb/s**.
  On-chip synchronous ingress does not qualify that faster external interface.

The AFE limit stays binding: N4 at 16 Msym/s means **64 MS/s/axis**, 98.4615% of
65 MSPS; the ideal sample-rate ceiling is 16.25 Msym/s. GPIO28 is 64 MHz at cpw=1
and 32 MHz at cpw=2. Nominal alpha=0.20 width is 19.2 MHz. Checked local AFE7071
SLOS789C electrical table, printed p.5, specifies 65 MSPS/axis and 130 MSPS input;
printed p.27 describes I/rising, Q/falling single-differential DDR and FIFO bypass.
Physical interface and RF qualification remain outside these arithmetic results.

## Cycles, bandwidth and placement

The supplied measured code-10 stage baseline is approximately **166k BB+BCH +
100.4k LDPC + 71k map = 337.4k cycles/frame** with kernel 2, packer 1 and packed
mapper. That is **126.7% of one core**, or 63.4% of two perfectly balanced cores,
before ingress, descriptors and supervision. The existing split is not perfectly
balanced; these totals alone cannot establish a live rate.

Instruction counting, using the timing rules in [rp2350-notes](docs/rp2350-notes.md),
gives **46 instructions/word** for the four-word leaf versus **54** for the
five-word leaf. Charging the input LDR use, three dependent complex shifts and
the taken branch gives approximately **51 versus 59 cycles/word**, before
arbitration, cache misses and function overhead. Multiregister prologue/epilogue
timings are not assumed exact. For 1818 complete input words at 9/10:

| Estimated quantity | Four-word kernel 3 | Five-word sliced kernel 2 |
|---|---:|---:|
| Leaf cycles/frame | 92,718 | 107,262 |
| 128-word leaf slice at SYS128 | 51.0 us | 59.0 us |
| 96-word pointer-path leaf slice | 38.25 us | 44.25 us |
| Table loads/frame | 29,088 | 36,360 |
| Table-read bandwidth at target | 55.935 MB/s | 69.919 MB/s |

The **14,544-cycle/frame** difference is about **5.46 core-0 percentage points**
at the target, and removes about **14.0 MB/s** of table reads. Whole-frame kernel 3
BB+BCH is estimated at **104–111k cycles**, including roughly 11–18k for the
surrounding scramble/copy/tail work. Total isolated FEC would be **275–282k**,
about **16–18% below** the supplied 337k baseline. This is the frontier model's
approximate benefit, not a device result.

There is an essential A/B distinction: standalone `dvbs2 10` kernel 2 retains
the six-word generic full-frame BCH, whereas the newly enabled live kernel-2
frontend already uses the five-word continuation. Thus **a large stage A/B gain
must not be reported as an equally large live A/B gain**. Also, standalone
kernel 3 executes from cached XIP, while live kernel 3/bank 1 executes from Y.

For planning only, scaling the measured 5/6 scratch-Y frontend fraction (45.2%)
by input length gives about **48.9%** for the new five-word t=8 slices. Adding
the prior descriptor/supervision remainder (83.1%-45.2%=37.9%) gives about
**86.8% core 0**, falling toward **81–82%** with the four-word kernel. A separate
stage/ingress estimate gives core 1 about **79% GCS / 91–92% TS**:
`[(100422+71000)*1.021 + 58112*(0.61 or 1.18)] / 266256`.
The contention factor and ingress cycles/bit are inherited from the
[frontier calibration](docs/frontier/frontier.md); they are assumptions for this
image. BCH slicing overhead, IRQs and changed arbitration can move the outcome.
The initial whole-BB copy is still one call, roughly 80–156 us estimated;
pointer poll gaps and maximum slice cycles must be checked on hardware.

Sample DMA still transfers **64 M 32-bit words/s**, reading 256 MB/s of LUT data
and writing 256 MB/s to PIO. Descriptor control DMA adds **8 M transfers/s**.
SPI RX and emulation at the demanded arrival rate add approximately **1.767 M
transfers/s**, plus negligible emulator-rewind transfers. This is approximately
**73.77 M DMA transfers/s / 57.6%** of the ideal 128 M/s budget. A pins32 capture
temporarily adds `128e6/17 = 7.529 M transfers/s`, bringing that simple bound to
about 63.5%. These are transfer counts, not guaranteed arbitration bandwidth or
proof of DMA handoff deadlines. Pointer output additionally needs its packed
source/credit traffic. A1 already demonstrates why average bandwidth is insufficient.

Exact post-link free bytes, including the existing reserved 1 KiB heap and both
2 KiB stacks, from GCC Arm 14.2 / SDK 2.2.0 builds:

| Image | SRAM0–3 after heap | SRAM4–7 | Scratch X | Scratch Y |
|---|---:|---:|---:|---:|
| Original default iqbench / pvflight | 680 / 136 | 4464 | 16 | 304 |
| C2 default iqbench / pvflight | **680 / 136** | **4464** | **16** | **96** |
| C2 B1 ON iqbench / pvflight | **584 / 40** | **4464** | **16** | **96** |

In the default iqbench map, old Y BCH160 remains at `0x20081530`; the new leaf is
`0x20081620..0x200816ef`. Y code ends at `0x200817a0`, before its stack at
`0x20081800`. `ldpc_matrix`, `bch_tab`, FE storage and the input/sample ring bases
retain their baseline addresses. Some lower-RAM encoder functions moved by 32–36
bytes because of dispatch changes; this is another reason to repeat the control.
The high bank is NOLOAD data and is not used for executable code. Both ordinary
and B1-aligned images link; no stacks or heap reservations were reduced.

## Native verification

`make build` and `make build BUILD=firmware/build-b1` passed for iqbench and
pvflight without compiler warnings. The latter was configured with
`-DIQ4HIST_EXPERIMENT=ON`. Full **`make check` passed (exit 0, 74 Python command
invocations)**, including the new assembly model. `git diff --check` and relative
Markdown-link validation also passed.
Native tests do not access USB and their runtime is not M33 timing evidence.

- All **42 existing code/pilot configurations**, selectors 0–3 and LDPC pack
  settings 0/1 compare against bit-serial C and independent Python PLFRAMEs.
  Frontend tests also cover both banks, chunks 1/7/128/511, zero-work and completed
  calls, two interleaved FE slots, varied frames, rejection and canaries.
- Live queue tests cover **36 cases / 504 frames**: all five dualstream codes,
  TS/GCS, both pilot settings and extra high-rate selector/packer controls.
  Initial BB CRCs, every emitted word, final partial symbols and masked BB tail
  bytes match independent Python. The actual production queue leaves are compiled.
- Pointer scheduling tests cover **18 cases**, codes 8/9/10, TS/GCS, both pilot
  settings and freeze controls. Sixteen frames cross four ring lengths with skewed
  readers. Frozen credits stop at exactly 14,336 authorized words. This models
  event ordering, not simultaneous bus timing.
- New `host/test_bch128_asm.py` expands the **actual assembly macros** and checks
  **4120 cases / 906346 executed model instructions** against bit-serial polynomial
  division. Both placements, every byte-table index, full-word spans, arbitrary
  prior remainders, uneven continuation slices, exact memory bounds, callee-saved
  registers and the 40-byte stack frame pass. This is ISA semantics, not silicon.
- Runner tests reject wrong code/size/selector metadata, bad data with valid serial
  CRC, clock errors and credit failures. The new strict 16-Msym/s campaign behavior
  has a mocked runner test. Focused reference and pointer runner suites pass.

## Coordinating engineer: build and stage A/B/A

Only the coordinating engineer should perform the following device steps. From
this worktree, the ordinary build directory is `firmware/build`; the UF2 is
`firmware/build/iqbench.uf2` (absolute prefix is this worktree's root). Use your
normal hardware loading procedure. Runners do not build or flash. Start with
external SPI GPIOs unwired, as these tests enable the on-chip master.

```sh
make build
make check
export C2_FW_GIT=$(sed -n 's/^#define GIT_REV "\(.*\)"/\1/p' firmware/build/git_rev.h)
```

For the B1-aligned image instead, configure with the SDK/toolchain exported by the
Makefile, build `firmware/build-b1` with `IQ4HIST_EXPERIMENT=ON`, load its iqbench
UF2, and substitute that build path everywhere. Do not mix the two images' runs.
The current ordinary build identifies as `dfb34c58ed-dirty`; rebuilding after
coordinator commits changes the identity and hashes. The runners pin actual build
identity and preserve firmware/source hashes in each fresh output directory.

This stage script uses the existing Python CRC checker, saves fresh JSON, and
issues **`dvbs2 10 1 20`** for kernel 2, then 3, then 2. It disables the shared
optimization log so this experiment has its own evidence files:

```sh
python3 - <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, 'host')
import iqbench as iq
iq.LOGGING = False
out = Path('results/c2-qpsk-910/stage-aba')
out.mkdir(parents=True, exist_ok=False)
b = iq.Board()
try:
    for command in ('clock 128000', 'mapkernel 1', 'ldpcpack 1', 'bchbank 1'):
        r, _ = b.cmd(command)
        assert 'error' not in r, r
    for iteration, kernel in enumerate((2, 3, 2)):
        b.cmd(f'bchkernel {kernel}')
        r = iq.run_dvbs2(b, 10, 1, 20, None)
        assert r['crc_ok'] and r['git'] == os.environ['C2_FW_GIT'], r
        assert (r['index'], r['pilots'], r['reps'], r['syms'], r['clk_hz'],
                r['bch_kernel'], r['map_kernel'], r['ldpc_pack']) == (
                    10, 1, 20, 33282, 128000000, kernel, 1, 1), r
        (out / f'{iteration}-kernel{kernel}.json').write_text(json.dumps(r, indent=2)+'\n')
finally:
    b.s.close()
assert not iq.FAILURES, iq.FAILURES
PY
```

Expected: `crc_ok=true`, identical PLFRAME CRC across all three rows, 33,282
symbols, kernel-2 stages near the supplied 166k/100k/71k prior observations.
Record `cyc_bch`, `cyc_frame` and `prof.f_bb_bch/f_ldpc/f_map`. Kernel 3 should
reduce BB+BCH while preserving LDPC/map and CRC; the 16–18% whole-frame saving is
an estimate to test, not a correctness exemption. Repeat code 9 and both pilot
settings with `python3 host/iqbench.py dvbs2 9 10 --pilots 0 --reps 20` and then
`--pilots 1`, after selecting kernel 3; this command also checks Python CRCs.

## Coordinating engineer: full live qualification

Run short 3 s versions first (change `--ms` and use fresh output directories),
then these **60 s** controls and target. Each command runs TS and GCS separately.
The target device lines are `dualstream 1 60000 2 5 4096 0 0 0 4 10` and the same
with source 6. The runners also run before/after legacy checks and one-second
waveform, starvation and inverted-clock controls.

```sh
# Previously measured profile: repeat on this image to detect placement regressions.
python3 host/dual_trial.py gcs --clocks 128000 --ms 60000 \
  --writers 4 --quad-profile 1 --code-index 8 --map-kernel 1 \
  --bch-kernel 2 --ldpc-pack 1 --bch-bank 1 --cpws 1 --sources 5 6 \
  --dac-clock ddr --build firmware/build --expected-git "$C2_FW_GIT" \
  --out results/c2-qpsk-910/control-56-60s

# Same-rate, bounded five-word BCH control for the new code.
python3 host/dual_trial.py gcs --clocks 128000 --ms 60000 \
  --writers 4 --quad-profile 1 --code-index 10 --map-kernel 1 \
  --bch-kernel 2 --ldpc-pack 1 --bch-bank 1 --cpws 1 --sources 5 6 \
  --dac-clock ddr --build firmware/build --expected-git "$C2_FW_GIT" \
  --out results/c2-qpsk-910/control-910-k2-60s

# Target: four-word t=8 BCH in core 0's bank, all ingress and FEC active.
python3 host/dual_trial.py gcs --clocks 128000 --ms 60000 \
  --writers 4 --quad-profile 1 --code-index 10 --map-kernel 1 \
  --bch-kernel 3 --ldpc-pack 1 --bch-bank 1 --cpws 1 --sources 5 6 \
  --dac-clock ddr --build firmware/build --expected-git "$C2_FW_GIT" \
  --out results/c2-qpsk-910/target-k3-60s
```

8/9 uses the target command with `--code-index 9` and a fresh `--out`. A slower
control uses `--cpws 2` (8 Msym/s). A placement control uses code 10/kernel 3
with `--bch-bank 0`; this intentionally selects the five-word striped leaf and
therefore changes both implementation and placement. A pure bank control uses
kernel 2 with banks 0/1. Always retain failures and use a fresh directory to repeat.

Primary pass criteria: both source rows have `classification=clean`, empty
`faults`, full requested elapsed duration, expected `git` and configuration,
zero output/ownership/DMA/SPI errors or null insertion, advancing runtime SPI/BB/FEC
counters, matching accepted-message CRC chain and first-four BB CRCs, and **all
4096 pins32 observations** matching the independent waveform at one fixed offset.
GPIO28 must equal IQ_FLAG on every observation, with both phases present. Negative
controls must classify `detected` for their expected fault and keep unrelated checks
clean. The campaign exits nonzero on an unexpected target failure. CPU fractions
and maximum slice/poll/stage costs are outcomes to record; low load cannot excuse
wrong samples or missing input.

Optional pointer investigation, after an 8-Msym/s positive control:

```sh
python3 host/dualptr_trial.py --ms 3000 --clocks 128000 --cpws 2 \
  --sources 5 6 --faults 0 1 --clock-modes 1 2 --code-index 10 \
  --bch-kernel 3 --bch-bank 1 --control --build firmware/build \
  --expected-git "$C2_FW_GIT" --out results/c2-qpsk-910/ptr-8-control

python3 host/dualptr_trial.py --ms 3000 --clocks 128000 --cpws 1 \
  --sources 5 6 --faults 0 --clock-modes 1 --code-index 10 \
  --bch-kernel 3 --bch-bank 1 --control --build firmware/build \
  --expected-git "$C2_FW_GIT" --out results/c2-qpsk-910/ptr-16-probe
```

Equivalent target line: `dualptr 1 3000 6 0 10`, after the selectors above.
The runner enforces poll gap <=256 us, conservative grant runway (>=1360 words
at SYS128), no issue beyond grants, source/clock/waveform identity and resource
restoration. A clean positive pointer row is `digital_live_clean`; its descriptor
control is `cpu_descriptor_control_clean`. A1 predicts this 16-Msym/s probe may
still fail with writer TXSTALL. Only extend a passing short probe to `--ms 60000`
with another fresh output directory. Kernel 3 does not repair its two-writer handoff.

## Risks, dead ends and next evidence

- A separate lower-SRAM four-word loop was rejected: the B1 ON pvflight image has
  only 40 bytes free. A new table is unnecessary. Executable placement in NOLOAD
  high SRAM was not attempted. The 208-byte Y continuation plus XIP full-frame
  copy fits without shifting core-1 work into Y.
- Unbounded t=8 BCH was unsuitable for supervision; simply adding code choices
  would have left a scheduling defect. Supporting t=12 in `dualptr` is deferred
  rather than silently falling back to an unbounded call.
- The approximately 18% isolated-work reduction does not apply directly to the
  new sliced live control. Native timings and ISA models do not establish M33
  cycles, cache behavior, contention, IRQ latency or thermal margin.
- The TS backend estimate is close enough to prior limits that input scheduling
  can still bind. READY waits, minimum ring lead, maximum frame/slice costs and
  queue counters need inspection even if average load looks comfortable.
- The current pointer engine has a measured live handoff failure at 16 Msym/s;
  four pointer-fed writers remain separate work. Sparse captures verify a finite
  set of samples; they are not a recording of every output word over 60 seconds.
- Faster external ingress, AFE pin timing, loaded clock edges, DAC reconstruction
  images, spectrum/EVM and receiver decode remain unqualified. N2 modes are not
  used in this experiment.

## Intended small logical commits

The coordinating engineer should create these commits; none was created here.

1. `firmware/src/dvbs2.c`, `dvbs2.h`, `dvbs2_asm.S`;
   `host/native/dvbs2_test.c`, `dvbs2_buffers_test.c`;
   `host/test_dvbs2_buffers.py`, `host/test_bch128_asm.py`, `Makefile` —
   **Add opt-in t=8 BCH stream kernel and bounded high-rate frontend.**
2. `firmware/src/bench_dual.c`, `bench.h`, `main.c`;
   `host/test_dual_codes.py`, `host/test_dualptr_native.py` —
   **Enable and verify normal 8/9 and 9/10 in live QPSK pipelines.**
3. `host/dual_spi.py`, `dual_trial.py`, `dualptr_trial.py`;
   `host/test_dual_spi.py`, `host/test_dualptr_trial.py` —
   **Qualify high-rate QPSK runner configuration and independent references.**
4. `REPORT.md` — **Document C2 validation, resource budgets and device A/B commands.**
