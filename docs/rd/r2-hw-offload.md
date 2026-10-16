# R2: hardware offload after pulse shaping

The strongest remaining hardware opportunities are **autonomous DMA grant reloads**
and **Hazard3 bit packing**. Neither is a demonstrated complete transmitter solution.
Four independent DMA reloaders remove CPU work at grant boundaries, but B6 can fit
them only with sparse capture disabled. Hazard3 has the right `pack`/`zip`
instructions for the packed-code mapper; running them needs a RISC-V toolchain and
another firmware image. This work supplies a finite DMA experiment and an isolated
mapper experiment, with native verification. No Pico or serial port was
accessed. No flashing or clock/VREG/flash configuration changes were performed;
Git metadata and existing measurement evidence were left unchanged.

This is the R2 delivery report, replacing `REPORT.md`, on `rd/r2-hw-offload`, based
on `rd/c3-8psk-fec16`. B6 was inspected with read-only `git show`; its live output
engine has **not** been copied into this branch. Existing commands retain their
defaults. The new `r2grant` command is restricted to `IQ4HIST_EXPERIMENT=ON`
`iqbench`; the Hazard3 experiment is a separate CMake project.

## Workload and units

The supplied device evidence is the starting point, not a new R2 measurement:
[C3 results](../../results/c3-8psk-fec16/2026-10-15/README.md) give 402–495k
cycles per assigned frame, versus 355,104 cycles available per core at SYS128,
16 Msym/s with alternate frames. B6's measured runtime source is PRBS generation,
not FEC. Its dedicated core-1 rearm loop conflicts with C3's two-core FEC model.

Let `S` be SYS in Hz and `d=cpw`. The adopted N4 output gives
`Rs=S/(8d)`. A normal 8PSK PLFRAME with pilots has 21,600 data symbols, 90 header
symbols and 14×36 pilots = **22,194 symbols**. At SYS128/d1 it lasts
**1,387.125 us**, or 177,552 SYS cycles. Each alternate-frame worker gets
`2*22194*8d = 355104d` cycles. These cycle ratios hold at SYS256/d2; no R2
code implements that clock configuration.

| Normal rate | Kbch / Kldpc bits | BB payload at 16 Msym/s | BB words/frame, rounded up | Reentrant LDPC matrix |
|---|---:|---:|---:|---:|
| 2/3 | 43,040 / 43,200 | 30.970533 Mb/s | 1,345 | 60×12×4 = 2,880 B |
| 3/4 | 48,408 / 48,600 | 34.840407 Mb/s | 1,513 | 45×12×4 = 2,160 B |
| 5/6 | 53,840 / 54,000 | 38.756421 Mb/s | 1,683 | 30×12×4 = 1,440 B |

Payload rate is `(Kbch-80)*Rs/22194`, including PL overhead. A packed axis word
holds 16 symbols. Each of B6's four input readers consumes the **whole** axis
stream, including the other parity's history: `Rs/16` words/s per reader.
Its 512-word grant therefore lasts `8192/Rs` seconds: **512 us** at 16 Msym/s,
1,024 us at 8 Msym/s. Four reloads per grant interval mean 7,812.5 reloads/s.

A DMA transfer below means one read/write pair. SRAM→PIO or SRAM→DMA CSR uses
**one** shared FASTPERI access; PIO RX→DMA trigger uses **two**. SRAM→SRAM uses
none. The local RP2350 datasheet, build 2025-07-29, §2.1 p24 and §2.1.1 p25
were checked: PIO0/1/2, USB, DMA CSRs, XIP DMA FIFOs and HSTX FIFO share one port,
with an ideal upper bound of one zero-wait access per SYS cycle. Sixteen channels
do not multiply the DMA's one-read/one-write-per-cycle ceiling (§12.6 p1095).

## Survey

Survey matrix and checked-source ledger follow below. A hardware operation that
cannot implement a stage has no useful stage throughput; a high raw interface
rate does not change that. All cycle budgets are estimates unless explicitly
identified as prior device measurements.

The B6 baseline already occupies the following resources, including reserved
sparse capture. Its old three-SM PV-SPI emulator cannot simply be added.

| B6 service | SMs | DMA channels | Instruction words |
|---|---:|---:|---|
| PIO row producers / code inputs | 4 | 4 input +4 RX control | PIO0: 13 |
| Four sample writers | 4 | 4 sample | PIO2: 8 |
| DDR clock / capture | 1 +1 | 0 +1 | PIO1: 14 total |
| Total / free | 10 /2 | 13 /3 | Free SMs both in PIO1 |

Per symbol, two sample words +two RX pointers +four inputs/16 symbols cost
**4.25 DMA transfers, 6.25 port accesses**, or 53.125% DMA /78.125% port at d1.
Stride17 capture adds `8/17` transfers/accesses per symbol: totals
59.007353% /84.007353%. Source/table SRAM reads are 9 B/symbol =144 MB/s at
16 Msym/s. The existing core-1 polling schedule requests up to
`4/64+2/(32*64)` port accesses/SYS cycle, another **6.347656 percentage points**
before rearming, core0 and USB. Actual contention can lower its polling rate.
Removing rapid polling and the core reservation matters much more than removing
the four infrequent rearm writes.

## Checked hardware facts and reusable resource costs

| ID | Checked block facts and first-principles resource cost |
|---|---|
| P | PIO has 12 SMs, four/block, shared 32-word instruction RAM/block; one instruction/SM cycle, 32-bit shifts, X/Y and OSR/ISR, unjoined 4-word FIFOs each direction (§11.1 pp876–878, §11.2.3 pp881–884). Instruction set has no native AND/XOR/add/CRC/GF multiply: MOV offers identity/NOT/bit-reverse (§11.4.10 pp898–899). A memory transform uses at least one input and one output DMA/channel/port access per 32-bit word, plus its PIO instructions, unless output goes directly to pins. Each added stage therefore needs at least 1 SM, 2 DMA, 2 port accesses/word and code/state; emulating missing logic needs an explicit instruction schedule, not an assumed accelerator. |
| D | DMA performs copies, byte swap, fixed-stride increment/decrement/double-increment, power-of-two wrap, chained controls; no arbitrary two-input ALU or arbitrary bit transpose (§12.6 pp1095–1098). One 32-bit RAM->RAM copy costs 1 transfer,4B read+4B write,0 FASTPERI payload accesses; RAM->PIO/trigger costs 1, PIO->trigger2. Gather/scatter often needs one extra control DMA and 1–4 descriptor words/segment (§12.6.3.1–3 pp1099–1100). Normal finite transfer completes after last write, then chains; triggers while BUSY are ignored; null trigger stops and can raise IRQ_QUIET interrupt. DREQ cannot have duplicate DMA subscribers (§12.6.4.2 pp1101–1102). |
| S | There is one passive DMA sniffer, observing one selected channel; it consumes no extra DMA channel if watching a transfer already needed, and adds no payload traffic. It accepts 32 bits/SYS cycle, implements fixed CRC32(IEEE), CRC16(CCITT), 32-bit addition and **one-bit** even parity. It neither transforms the transferred data nor supplies programmable polynomials (§12.6.8.2 p1107, SNIFF_CTRL table p1136). Seed/result/config accesses use FASTPERI; contention and single shared state still matter. |
| I | Each core has 2 interpolators, each 2 lanes; CPU SIO accesses are single-cycle, results ready next cycle; operations are right shift, mask, sign extension, addition, optional blend/clamp, cross input/result (§3.1.10 pp44–46). SIO has per-core CPU-only ports (§2.1 p25): DMA does not drive interpolators. Thus 0 SM/PIO/DMA/FASTPERI/SRAM for their state, but CPU instructions are still needed. No XOR/GF operation. E1 means do not depend on OVERF. |
| R | Hazard3 is an alternative architecture for each of the **same two sockets**, not two extra CPUs (§3.9 pp335–336). Zbkb zip/unzip interleave/deinterleave the two 16-bit halves (§3.8.1.18 pp271–272); zip/unzip/brev8/pack are 1 cycle, Zba shNadd and other listed bit ops 1 cycle (§3.8.7.6 pp299–300). Integer ALU 1 cycle, load 1 or 2 with dependency, store 1; these assume no bus stalls (§3.8.7.1 pp296–297). `EXTENSION_ZBC=0`, so no CLMUL (§3.8.8 p302). Mixed Arm+RISC-V is supported but needs two program images (§3.9.2 p336). Kernel changes need 0 SM/DMA/port accesses; normal SRAM traffic and a core's cycles remain. |
| H | HSTX is **output-only**, 8 pins, fixed GPIO12..19, up to 2 bits/pin/HSTX cycle; FIFO 8×32 on FASTPERI (§12.11 pp1202–1203). Rotation+static bit selection can emit zipped/unzipped records, but there is no internal return of transformed records to SRAM (§12.11.3 pp1204–1205). Expander repeats RAW or TMDS data; its sole actual encoder is TMDS; command consumes a cycle with no expander data output (§12.11.5 pp1206–1208). One DMA stream, 1 port access/32-bit FIFO word, 0 SM normally; PIO-coupled mode consumes PIO output signals and only works with clk_hstx sourced directly from SYS, adding 1 cycle to PIO-pad delay (§12.11.6 p1208). Existing GPIO6..13 OUT8 bus/GPIO14 flag/GPIO28 clock cannot be remapped to HSTX without a different board/pin design. |
| A | Atomic XOR/SET/CLR aliases operate on **peripheral registers, not SRAM**; native aliases cost same cycles as normal write; interposed UART/SPI/I2C add 2 cycles (§2.1.3 p26). They replace a control-register read-modify-write with 1 store and avoid lost updates. No SM/channel/state SRAM cost. DMA may target ordinary DMA/PIO peripheral aliases, but not CPU-only SIO. This is not a RAM XOR engine. |
| C | SIO mailboxes are 4×32 each direction; doorbells 8 flags each direction (events coalesce), spinlocks 32 (§3.1.4–6 pp41–43). Per-core SIO access avoids FASTPERI. Ownership still needs release/acquire barriers, valid cursor logic and bounded callbacks. A2 E2 aliases writes above SIO+0x180 to spinlock release; SDK workaround uses SRAM atomics (p42); do not build a custom lock protocol around that hazard. |
| Q | QMI supports serial/dual/quad transactions with memory command/address/dummy overhead; transfer chaining improves sequential throughput, not random accesses (§12.14.2–2.1 pp1228–1231). Quad data's ideal rate is 4 bits/SCK; an example quad 32-bit read uses 22 overhead+8 data SCKs. Flash/PSRAM share the interface. It is storage/stream transport, no CRC/GF/bit mapper. Direct FIFO adds FASTPERI; XIP memory reads instead use XIP subordinate ports. No new memory/clock setup is proposed here. |
| U | USB supports 12 Mb/s Full Speed only, 4 KiB DPRAM /3840 B usable buffering (§12.7.1 pp1141–1142): insufficient even for 30.9705 Mb/s minimum requested BB input. USB protocol hardware does not supply DVB framing. Hardware SPI two PL022 controllers (§12.3 p1046), slave<=clk_peri/12, master<=clk_peri/2 (§12.3.4.4 p1050). At clk_peri=128 MHz that gives 10.667/64 Mb/s before gaps: slave inadequate, master potentially useful if external sender can slave and pins/protocol change. RX typically 1 DMA, TX dummy 1 if needed; 0 SM. APB reads>=3 SYS cycles, writes>=4 (§2.1.4 p26);16-bit SPI frames therefore require>=2 word-DMA transfers/32 data bits with>=6 APB read cycles, separate from FASTPERI. UART/I2C are slower framing transports, not FEC engines. |
| X | SHA hardware's fixed SHA256 takes>=121 cycles/64 B (16 APB writes, 64 cycles +57 digest), about 67.70 MB/s at SYS128 (§12.13.2 p1222). It cannot change its compression function into DVB CRC/BCH/LDPC. TRNG produces random entropy, not a seeded/restartable DVB LFSR (§12.12 pp1212–1213); RCP's fixed nonlinear 24-bit PRNG is explicitly unsuitable as a general PRNG (§3.6.3.6 pp117–118), let alone Gold/BB sequence. M33 FPU is ordinary single-precision (§3.6.4 p123); DCP is floating add/subtract/multiply/divide/sqrt over a CPU coprocessor interface (§3.6.2 p104): neither is carry-less polynomial arithmetic or an autonomous memory transform. GPIO coprocessor accesses GPIO (§3.6.1 p101), not arbitrary SRAM XOR. SIO TMDS is fixed DVI coding, 1 read/write per cycle, not DVB (§3.1.9 p44). These incompatible engines get 0 bytes/second of correct DVB work offloaded, 0 resources reserved. |
| T | DMA has 4 pacing timers, max 1 request/SYS cycle (§12.6.8.1 p1107); they pace data but cannot test publication or combine four independent completions. System timers/alarms and SIO MTIME can schedule service, not perform it (§12.8.1 pp1182–1184, §3.1.8 p43). PWM counts/generates edges, including 16-bit counters and level/edge modes (§12.5.2.5 p1082), no data-dependent GF logic. A clock or timer trigger alone is no ownership proof. 0 SM/DMA for CPU interrupt scheduling; a paced transfer still consumes its DMA/channel/port budget. |

## Complete stage/block applicability matrix

**Use** means that the block implements the named portion. **Assist** leaves the algorithm on a CPU while saving address, packing, transport or control work. **Reject** means that the native primitive is incompatible, or that the proposed graph exceeds the available resources. Every cell inherits the concrete resource budget in its ID row above. A rejected pair reserves zero resources and offloads zero correct stage operations; emulating it on a CPU is not hardware offload.

| Block | Ingress / framing | BB scrambler | CRC8 | BCH | LDPC | Bit interleave | PL map / Gold / pilots | Publication / credits |
|---|---|---|---|---|---|---|---|---|
| PIO (P) | Use wire receive, byte packing, counts; dynamic TS/GCS parsing remains CPU. New 1–2 SM design may fit; existing 3 SM does not. | Reject: no native XOR/LFSR feedback. | Reject: no native programmable CRC/XOR; 32 instruction words do not hold a 256-entry CRC table. | Reject: multiword GF/XOR absent. | Reject: shifts possible, row XOR absent. | Assist: shift/serialize columns, but a 32-symbol memory round trip needs 3 input + 3 output transfers, 6 port accesses, at least 2 DMA + 1 SM. | Assist constant replay; native Boolean mapping/Gold XOR absent. Existing table shaper only replaces pulse shaping. | Assist IRQ wait/barrier states. New counted-grant protocol must include prefetch/history, not merely pause pin output. |
| DMA (D) | Use FIFO capture/gather of fixed header/data/null segments; 1 payload transfer/word plus descriptors. Dynamic headers/SYNCD remain CPU. | Reject: no payload XOR with stored mask. | Reject: no programmable CRC; sniffer discussed separately. | Reject: table fetch cannot perform feedback XOR. | Assist copy/zero/gather; no XOR/variable rotation. Four-word control blocks per edge add 1920/2400/2160 words/frame. | Assist byte/word gather; normal columns are already contiguous 675-word streams. Bitwise byte-DMA requires 64800 transfers/frame before descriptors. | Use scatter/gather constants/data; reject nonlinear Boolean map and Gold XOR. | Use finite grants, chaining, null stop. Four independent reload partners exceed 16 channels with capture. ENDLESS/self-trigger does not validate publication. |
| Sniffer (S) | Use matching CRC32 during existing ingress transfer; one shared state, seed/result cost per packet. | Reject: passive, data unchanged. | Reject: fixed CRC32/16 polynomials differ from 0xD5. | Reject: 16/32-bit fixed state differs from 128/160/192-bit remainder. | Reject: EVEN gives scalar parity, not 12-word XOR. | Reject: data unchanged. | Reject: cannot emit transformed data/Gold. | Assist integrity diagnostics only; checksum is not a four-reader completion barrier. |
| Interpolators (I) | Assist ring/address masks; parser remains CPU. | Reject: shift/mask/add cannot XOR. | Assist table address, but XOR index and table load remain CPU; SIO overhead may lose. | Assist four-lane table address construction: estimated 3–8 cycles/word saving below. | Assist edge/index construction; CPU still rotates/XORs 12-word rows. | Assist shifts/masks/address walk, not a perfect shuffle. | Assist table index; table load and Boolean map remain CPU. | Assist modulo address only; cannot autonomously observe/compare all publication/completion totals. |
| Hazard3 (R) | Assist pack/rev8/bit operations; parser still runs on CPU. | Assist one-cycle XOR; existing precomputed masks still used. | Assist extract/address operations; no CRC8 instruction. | Assist shNadd/extract/XOR; CLMUL absent. | Assist rotates/XOR/bit operations; same CPU and SRAM responsibilities. | Use zip/unzip packing, one cycle each; column loads and MSB reversal remain. | Use zip/spread instruction reductions; strongest compute prototype. Mixed image integration is separate. | Assist CPU grant/AMO ownership logic; architecture switch adds no processor socket. |
| HSTX (H) | Reject: output-only. | Reject: no XOR or SRAM return. | Reject: fixed TMDS is not CRC8. | Reject: no configurable GF operation. | Reject: no configurable GF operation. | Assist zipped external pins; internal RAM transform requires loopback receiver, DMA and pins. | Assist RAW_REPEAT constants; TMDS is not DVB/Gold, eight pins do not replace adopted AFE bus. | Reject: clock generation cannot validate ownership. |
| Atomic aliases (A) | Assist control flags, one store; no payload parser. | Reject: SRAM has no XOR alias. | Reject: no CRC state machine. | Reject: no wide configurable GF accumulator. | Reject: no in-place RAM XOR; scratch-register workaround needs extra passes/port traffic. | Reject: not a permutation. | Reject: control-register XOR is not an SRAM mapper. | Use atomic PIO start/disable, IRQ/control updates; concurrency primitive, not automatic credits. |
| SIO sync (C) | Assist packet ownership notification. | Reject: no algorithm. | Reject: no algorithm. | Reject: no algorithm. | Reject: no algorithm. | Reject: no algorithm. | Reject: no algorithm. | Use release/acquire cursors + doorbell/mailbox notification. Coalescing doorbells require counters, not one flag per grant. |
| QMI / PSRAM / XIP (Q) | Use memory source/spooling; no live transform. | Assist stored mask; already precomputed in SRAM. | Assist stored table only. | Assist stored tables only; random-read overhead may be worse. | Assist storage only; still CPU read/modify/write. | Assist sequential column reads; no Boolean transform. | Assist constant sequences/replay; new payload still requires FEC/map. | Assist descriptor storage; no publication proof. |
| USB / SPI / UART / I2C (U) | Use transport offload; USB12M / SPI-slave10.667M insufficient. SPI-master64M potentially useful with changed source/pins. | Reject: no DVB scrambler. | Reject: wire CRC is not DVB CRC8. | Reject: no BCH engine. | Reject: no LDPC engine. | Reject: serial packing is not three-column permutation. | Reject: no DVB mapper/Gold engine. | Assist external READY/CS only; finite DMA ownership still required. |
| SHA / TRNG / RCP / FPU / DCP / GPIOC / TMDS (X) | Assist SHA integrity or TRNG seed only, not DVB framing. | Reject: wrong polynomial/state or nonlinear random sequence. | Reject: wrong algorithm. | Reject: no GF(2) carryless polynomial engine. | Reject: no GF(2) matrix engine. | Reject: no permutation to system RAM. | Reject: DVI/random/floating arithmetic does not produce DVB symbols. | Assist GPIOC control pin only; no autonomous credits. |
| Timers / PWM / DMA pacing (T) | Assist timeout/chip-select timing, not payload parse. | Reject: no content logic. | Reject: no content logic. | Reject: no content logic. | Reject: no content logic. | Reject: no content logic. | Assist clocks/periodic constant replay only. | Assist bounded service wakeups; timer rearm can outrun unpublished data, so finite grants remain required. |

## Quantitative explanations for the dead ends

- A PIO memory transform is not free because it uses no pins. For a hypothetical raw 64800-bit transpose of the three interleaver planes (not the 88776-bit packed-axis mapped frame), 675 groups/frame × (3 input + 3 output words) = 4050 transfers and port accesses/frame, 2.920 M/s or 2.281% of the SYS128 port. That excludes PIO instructions, mapping, buffering, descriptors and ownership. C3 already loads the three contiguous columns directly and fuses mapping, so an additional materialized permutation adds work. PIO cannot simply XOR those inputs into the Boolean 8PSK map. Its four RX scratch words/SM and 32 shared instruction words are not a 256-entry byte lookup table. SRAM lookup through a PIO-generated pointer adds two accesses for RX-to-trigger and one for SRAM-to-FIFO.
- DMA minimum access is eight bits. Even granting a preexpanded one-byte-per-bit source, 64800 bit moves/frame require 46.715 M transfers/s, 36.50% of SYS128 DMA capacity, before descriptors. B6 already consumes 53.125%, or 59.007% with capture. A four-word descriptor per independent bit move would itself be 186.861 M control words/s, impossible on the port. The ordinary mapper instead reads 2025 source words/frame, with no materialized interleaver.
- Sniffer CRC8 mismatch is structural. Firmware uses full degree-eight polynomial 0x1D5; hardware IEEE 0x104C11DB7 and CCITT 0x11021 have remainders 0xCE and 0x78 modulo 0x1D5. A full-stream hardware CRC cannot simply be truncated/projected to the DVB CRC8 for arbitrary input lengths. Per-byte assistance with extra seed/read/transform could be devised, but would compete with the existing single-byte table loop. BBHEADER CRC processes only nine bytes/frame, about 721 calls/s. Even an illustrative 50 cycles/header is 36k cycles/s, 0.028% of one SYS128 core. TS packet CRC8 over 187 bytes occurs about 20.6–25.8k packets/s at these payload rates and matters more; it still has the wrong polynomial for the sniffer.
- EVEN returns one population-parity bit over all observed bits. LDPC needs independent bitwise parities and wide row XOR, BCH needs a feedback polynomial remainder. A naive scalar parity reduction with seed and result read for each of the 2/3 code's 21600 parity bits would spend 31.144 M FASTPERI accesses/s, 24.33% of the port, before input gathering or configuration. B6 has only 21.875% spare without capture. Continuous parity-prefix tricks still require expanded/gathered bits and result retrieval; they do not implement its 12-word row updates.
- SRAM does not have atomic aliases. A hypothetical safe peripheral scratch register could accumulate N words with one seed, N XOR-alias writes and one result read, N+2 FASTPERI accesses. This does not rotate source words or write results back to RAM automatically. A RAM-to-register seed, source-to-XOR, register-to-RAM row update requires at least three transfers/word: 36 transfers and 36 port accesses per 12-word edge, before address controls and necessary shifts. CPU XOR is one nominal instruction and uses SRAM, avoiding that port. Using active DMA or PIO registers as scratch accumulators would corrupt the output engine.
- HSTX can format 16 pin bits/HSTX cycle, but has no return from its bit crossbar to system SRAM. Loopback needs receiver SM/DMA, pins, input timing and both FIFO write/read traffic. GPIO12..19 already overlap the adopted AFE bus/flag and PV-SPI. RAW_REPEAT is useful for constant output, not FEC-dependent labels or Gold XOR.
- Ingress requires 30.970533 / 38.756421 / 34.840407 Mb/s BB payload for 2/3 / 5/6 / 3/4 at 16M. Existing 32 MHz PV-SPI capacity 30.8697 Mb/s falls short of all three. USB12M and hardware SPI slave at clk_peri128/12 fail before protocol overhead. SPI master at clk_peri/2 gives an ideal 64 Mb/s, a credible SM-saving route if the external source can act as slave. GPIO18 SCK, GPIO20 RX, GPIO17 CS, GPIO19 TX would differ from the present PIO signal assignment. A PIO quad master with one four-bit IN per two instructions has an instruction ceiling of 256 Mb/s at SYS128, not an electrical timing guarantee. An external-clock WAIT/IN/WAIT slave has at least three instructions/nibble before timing margin; four to six cycles/nibble would give 85–128 Mb/s aggregate, enough in principle, but needs a new external source/protocol, pins and timing proof. No new transport is implemented here.

## Narrow CPU assists to keep after the primary prototypes

### Interpolator BCH addressing (design only)

Each 32-bit recurrence makes four byte indices into four table planes, with eight-word rows. Required address is plane_base + 32*byte. Four lanes across two interpolators can build these addresses:

- interp0 ACCUM0=x. Lanes0/1 use the same input via CROSS_INPUT, right shifts 19/11, masks 5..12, bases for planes 3/2.
- interp1 ACCUM0=x and ACCUM1=x<<5. Lane0 uses shift 3/mask 5..12/base plane1; lane1 shift 0/mask 5..12/base plane0.
- Feed three SIO stores + one LSL, then read four PEEK results. Avoid POP, which would feed results back into accumulators.

This is eight instructions versus roughly eleven existing byte-extract/scaled-address/plane-offset instructions in the 160-bit assembly, with different load-use dependencies. A plausible saving of 3–8 cycles/word is an **unmeasured instruction estimate**: about 4.0–10.8k cycles/frame at 2/3 and 5.0–13.5k at 5/6, 1.1–3.8% of the 355104-cycle budget. Configure once per bounded slice and restore per-core state. Each core needs its own four lanes; there are no SMs, DMA channels, PIO words, FASTPERI accesses or extra BCH tables. The twenty or twenty-four table loads, remainder XORs and recurrence remain. Table traffic stays at least 80 bytes per 160-bit word. PEEK loads are CPU instructions; dependent result use still needs correct scheduling. Table/SRAM placement can dominate this gain. This is a plausible narrow optimization, not an independent BCH engine.

### Precomputed sequences (mostly already done)

`dvbs2.c` already builds BB scrambling words outside cadence. `dvbs2_8psk_init` already builds Gold planes and PLHEADER. Normal pilots-on Gold storage is 5536 bytes and includes both data and pilot sequence bits. Saving serial LFSR generation again would double-count work already removed.

Prepacking PLHEADER/pilots into axis codes could replace header zip and pilot map calls. Header storage is six groups × eight bytes =48 bytes, equal to the current six two-word header records. Fourteen pilot blocks require three groups each, 336 bytes. Total constant packed storage 384 bytes; approximate incremental storage 336 bytes if the header is reused. Stream carry/shift assembly remains because frame fill cycles through 0,2,...14. Only 594/22194=2.676% of symbols are constant. The 48 independently padded storage groups are not 48 complete groups on air: the whole frame emits 1387 complete continuous groups plus 2 pending symbols. C3 estimated all header/pilot overhead near 4–5k cycles/frame; that is an optimistic saving ceiling before replacement copy/carry work, 1.1–1.4% of the per-core frame budget. No data-FEC saving follows.

Caching complete preencoded frames would invalidate a live-transmitter compute claim. C3's synthetic 256-byte payload has frame-phase periods 128/4/256 for 2/3,5/6,3/4: payload bytes 5370/6720/6041, period=256/gcd(bytes,256). A tempting four-frame 5/6 cache removes FEC only because the test input repeats; arbitrary ingress cannot use it. Do not count replay as FEC offload.

### Completion-IRQ rearm alternative (design only)

This retains B6's 13 channels and frees core1 without four extra controllers. At 512 words/grant =8192 symbols, completion rate is 1953.125/s/input, 7812.5/s for four. An illustrative 150 cycles/event costs 1.172 M cycles/s, 0.916% of one SYS128 core; one shared interrupt can amortize entry when events coalesce. This is not measured handler cost or a latency guarantee.

The seam deadline is the difficulty. An unjoined TX FIFO's four words plus OSR
hold at most 80 symbols, equivalent to 5 us at the nominal 16-Msym/s output
cadence. That is not a guaranteed service window: the producer can consume five
words in roughly 440 unbackpressured SYS cycles, 3.44 us at SYS128. A final DMA
write guarantees that word arrived; it does not guarantee a full FIFO. One word
can be consumed in 88 unbackpressured cycles, about 0.688 us. Interrupt entry,
four-channel work, XIP misses, arbitration and USB must fit actual runway.
C3's 145–198-us main-loop slices do not prove interrupts cannot preempt them,
but interrupt masking can still violate renewal. Measure under output priority
and capture, and retain finite stop at publication exhaustion. Low average ISR
cost is not a schedulability proof.

These rejection verdicts apply to the native primitives and the explicit graphs
above. PIO can emulate Boolean operations through conditional instruction
sequences; the absence of a native XOR is not a computability proof. Such a
proposal must supply its instruction schedule and feedback transport. The BB
stream already needs up to 38.8141 Mb/s including its header, about 3.30 SYS cycles
per bit at SYS128, before input, output and feedback. CRC8's much smaller BBHEADER
work does not justify consuming another scarce SM/DMA path. No uncounted PIO
logic emulation is credited as offload here.

For assist cells, resource costs include CPU accesses: a simple interpolator
feed/peek needs at least two SIO instructions before the ordinary table load;
Hazard3 XOR/rotate and M33 XOR/rotate both have nominal single-cycle arithmetic.
Zba indexed addressing does not automatically beat Arm's scaled LDR addressing.
A stored table or QMI source performs zero arithmetic by itself. Clock, timeout,
notification and control cells carry only control work: one peripheral/SIO
operation per event at best, with the payload-stage throughput unchanged.


## Prototype 1: finite DMA grants

[r2grant.c](../../firmware/src/r2grant.c) builds immutable lists of up to eight
512-word counts followed by a zero sentinel, only for a complete published
prefix. [bench_r2grant.c](../../firmware/src/bench_r2grant.c) gives each of four
independently paced input channels its own one-word reload DMA. Input completion
chains to reload; reload reads the next count and writes the input channel's
`AL1_TRANS_COUNT_TRIG`. Reload retains an incrementing list address and a count
of one. The input retains its source address. A zero trigger stops that reader
and raises its raw IRQ because IRQ_QUIET is enabled; no ISR or CPU rearm runs.
The next reader is never triggered based on another reader's completion.

This uses checked DMA behavior: strict final-write ordering (§12.6.1 p1096),
retained addresses and count reload (§12.6.2.1–2 p1097), finite retirement
(§12.6.2.2.1 p1098), alias map (§12.6.3.1 p1099), disabled self-chain and null
trigger (§12.6.3.2–3 p1100). Every PIO TX and RX DREQ has exactly one DMA
subscriber, as required by §12.6.4.2 pp1101–1102.

The diagnostic substitutes four pin-free echo SMs for B6's producers. Each
executes PULL, MOV ISR/OSR, PUSH, JMP X-- with delay31 on all four instructions:
**128d SYS cycles/word**, exactly B6's average packed-input rate. A fifth
instruction is a terminal self-jump after the programmed word count. This
prevents a healthy EOF from setting TXSTALL. Four RX DMAs capture every word;
four physical streams must preserve both axes' exact independent patterns and
the complete granted length. `gap_us` deliberately delays foreground service;
core1 receives no task. A 6000-us gap spans the full 4096-word d1 burst at
SYS128. This establishes a measurable finite-burst question, not live output
seam latency under B6 traffic.

| Cost | Finite diagnostic | Proposed B6 capture-off integration |
|---|---|---|
| SMs / PIO words | 4 /5, shared in PIO0 | No additional SMs/words |
| DMA | 4 input +4 reload +4 echo =12 | Existing 12 +4 reload =16 |
| Payload transfers and port accesses/symbol | 0.25 TX +0.25 echo =0.5 | B6 4.25 transfers /6.25 accesses |
| Reload increment/symbol, steady interior | 4/(512×16) =1/2048 | Same |
| d1 DMA /port load, steady interior | 6.256104% /6.256104% | 53.131104% /78.131104% |
| CPU per grant while launched | 0 instructions | 0 for a prepublished finite epoch |

The finite diagnostic uses **36 reload transfers** for eight grants plus one
sentinel on each stream: 32 launches plus four terminal stops. The per-symbol
steady number excludes these four finite-stop overheads. Each reload reads 4 B
of SRAM and writes one FASTPERI CSR. Four input streams read 65,536 B and the
four echo streams write 65,536 B for a full burst. Input and echo together add
32 MB/s useful SRAM payload traffic at the nominal 16-Msym/s input cadence.

No new static target SRAM is allocated. Sources borrow 32,768 B of `in_buf`;
four echo regions, guards and count lists borrow **65,744 B** of `ring_buf`
(65,536 +64 +144). Sources are I=`0x20020000`, Q=`0x20024000`; lists occupy
`0x20010040..0x200100cf` after echo/guards. Lists are not placed in scratch code:
`stackcheck` compares scratch against its flash image and would correctly flag
mutable scratch contents as a mismatch. The usual PRBS input is restored after
all owned DMA has retired. Other table commands rebuild their borrowed table.

The experiment ELF leaves **240 B** after the reserved 1 KiB lower-RAM heap,
**32 B** between `.sram_hi` end `0x2007efe0` and stack bottom `0x2007f000`,
16 B between scratch-X code and the core1 stack, and 2,144 B free after scratch-Y
code. These are linker margins, not stack high-water measurements. The new
command's compiled stack frame is 472 B including saved registers, before
callees/printf/IRQs. Its code/planner/source are 3,832/90/40 flash bytes.

The maximum planner path has 38 Thumb instruction slots including nine stores.
Using the M33 timing rules, **50–65 cycles/list, 200–260 for four lists** is an
instruction-based setup estimate excluding XIP misses/contention. Preparation,
verification, printing and polling for the final stop are outside that estimate.
No host-native time is substituted for a target cycle count.

The immutable horizon is **65,536 symbols**: 4.096 ms at SYS128/d1, 8.192 ms at
d2. This protocol cannot append grants to an already consumed sentinel or safely
wrap and overwrite live source slots. It does not replace B6's continuous rearm
worker yet. A rolling version needs per-reader descriptor epochs, retirement
before zero-slot reuse, restart rules, paired-axis release and a proof that no
reader can replay stale slots. Removing capture fits the independent reloaders;
retaining it needs **17 channels**, so is rejected. One shared timed reload loop
has no completion barrier: a trigger can be ignored while a slower channel is
BUSY and lose a grant. No such shortcut is implemented.

The 16-channel capture-off proposal also leaves **zero channels for real ingress
DMA**, including even a single hardware-SPI RX channel. Its resource fit applies
to memory-sourced codes only; a transmitter needs CPU-serviced ingress or a
further reduction in the DMA graph. This resource limit is independent of the
unimplemented rolling publication protocol.

Stop disables SMs, clears EN/chains for every owned channel, then performs a
bounded joint abort before release, following §12.6.8.3 pp1107–1108 and E5.
On abort failure, ownership and borrowed buffers remain retained; reboot before
another buffer-using experiment if `released=false`. That is an actual stop
failure, not an acceptable starvation control.

## Prototype 2: Hazard3 packed-code mapper

[r2_hazard3_bits.h](../../firmware/src/r2_hazard3_bits.h) replaces the packed
mapper's three helpers only when `IQ_R2_HAZARD3` is defined:

| Operation | Explicit Hazard3 instructions | Ideal instruction cycles |
|---|---|---:|
| Interleave low halves of two words | `pack; zip` | 2 |
| Spread low 16 bits into even bit positions | `pack rd,rs,zero; zip` | 2 |
| Reverse 32 bits | `brev8; rev8` | 2 |

`zip` interleaves the lower/upper 16-bit halves of one register; it is not the
three-column DVB interleaver itself. The mapper still reads its three columns,
combines Gold/sign planes, inserts PLHEADER/pilots and carries partial groups
across frames. No intermediate interleaved frame is allocated. The change is
in [dvbs2_8psk.c](../../firmware/src/dvbs2_8psk.c); ordinary Arm builds compile
the original helper bodies. The helpers consume **zero SMs, PIO words, DMA
channels, FASTPERI accesses and additional data SRAM**. CPU instructions remain
necessary; this is an ISA acceleration, not an asynchronous coprocessor.

The checked sources are §3.8.1.18 pp271–272 for the Zbkb semantics,
§3.8.7.6 pp299–300 for one-cycle `pack`/`zip`/`brev8` and p299 for `rev8`.
§3.8.8 p302 explicitly has `EXTENSION_ZBC=0`, so a carryless-multiply BCH
proposal cannot run on this chip. `Zbkb` does not imply `Zbc`.

The Arm GCC14 object gives approximately 57 helper instructions per 32 data
symbols: two 18-instruction zips, two nine-instruction spreads including half
selection, and three RBITs. The candidate source needs 17 ideal single-cycle
instructions including high-half extraction and the three two-instruction
reversals. Thus **40 cycles/32 symbols ×675 words = 27,000 cycles/frame** is a
local instruction-substitution estimate. It is not a compiled RISC-V loop count
or a whole-mapper prediction. RISC-V lacks Arm's fused shifted operands; register
allocation, loads, slices and instruction alignment can change the result.
GCC may already recognize some portable idioms. The timing rules for M33 are in
[rp2350-notes.md](../rp2350-notes.md); Hazard3 load/branch/alignment rules are
§3.8.7.1 pp296–297 and §3.8.7.9–10 pp300–301.

Even optimistically subtracting all 27k from C3's measured 2/3 totals leaves
375–388k, **still above 355,104**. Switching only core 1 leaves core 0's deficit
untouched. Restoring faster LDPC placement or other work is still necessary;
no stand-alone 16-Msym/s CPU-fit claim follows from `zip`.

[firmware/r2_hazard3](../../firmware/r2_hazard3/CMakeLists.txt) compiles the exact
C3 mapper twice, with renamed baseline symbols and the candidate helpers. The
`r2map code frames fault` command compares both on the same core, alternating
their order per frame. It uses an arbitrary deterministic 64,800-bit FECFRAME
input, pilots, Gold0, eight-word slices and continuous 2048-group rings. **It
does not encode FEC** or model external ingress. Both kernels' full physical
rings, pending code words and partial fill must match. `fault=1` flips the last
published candidate I bit after timing. The host independently derives both
expected rings/hashes from the existing scalar Python PL reference.

The mini image borrows no application arenas: its dedicated FE, Gold, four
axis-ring and configuration arrays total **46,492 B** (8,108 +5,536 +32,768 +80).
The built Arm harness has 62,504 B across vector/data/BSS/heap/stack sections,
including a 4 KiB stack and 2 KiB heap. This is a separate diagnostic image,
not 62 KiB newly required by the transmitter. An integrated helper substitution
retains C3's existing rings/Gold and adds no buffers. RISC-V ELF placement and
stack use remain unmeasured.

**Build result:** native emulation and the Arm harness UF2 build pass. This
machine has only Arm GCC14; the SDK RISC-V configure fails for missing
`riscv32-unknown-elf-gcc`/`riscv32-corev-elf-gcc`. Its Apple clang17 also has no
RISC-V backend. Consequently **no RISC-V object or UF2 was built**, and the
inline assembly is source-reviewed rather than assembler-verified here. The
Arm image reports `candidate="emulated-not-hazard3"`; the trial rejects it unless
`--allow-emulated` is supplied. Its cycle numbers cannot be used as Hazard3
performance evidence.

SDK2.2.0 supplies `rp2350-riscv` and `pico_riscv_gcc` toolchain files. The checked
`pico_multicore/multicore.c` builds architecture-specific launch trampolines and
uses RISC-V `gp`/`mtvec` versus Arm VTOR. `multicore_launch_core1` from an Arm
build does not launch a RISC-V C image as a drop-in function. A mixed build needs
separate linked flash/SRAM regions, an RV CRT/global-pointer/stack/vector setup,
reset-time ARCHSEL, an architecture-aware launch sequence and a shared ABI for
queues/barriers. This work implements none of that launch path.

The datasheet §3.9.2 p336 permits mixed sockets and explicitly requires two
program images. §3.9.1 p335 says the ordinary boot marker switches **both** cores
to a single image's ISA automatically; a RISC-V-only UF2 is the smaller first
measurement. No OTP programming is required for that path. R2 never changes
ARCHSEL or OTP. The harness enables `mcycle` by clearing `mcountinhibit.CY`
(Table378 p313 gives its inhibited reset value), then accumulates bounded
32-bit deltas into 64-bit totals.

## Verification and coordinator trials

All native tests use the production C routines, existing Python references or
the actual production command under SDK/register shims. No test opens USB:

- `host/test_r2grant.py`: 51,210 guarded planner admission/capacity cases,
  24,576 independent C/Python source words, 294,912 exact echo words through four
  independently delayed readers, stable null stops, partial-prefix control and
  40 abort offsets. Its event model does not model RP2350 arbitration or rolling
  ring publication.
- `host/test_bench_r2grant.py`: executes the complete command body with the
  real five PIO opcodes and register/resource shims. Five test groups check all
  12 channel configurations, sole DREQ ownership, chains, null-trigger ordering,
  all 16 compact priority masks expanded to hardware bits 0/4/8/12, raw priority
  restoration, SYS128/256 ratios, exact echoes, guards, admission failures,
  missing sentinels, DMA/data faults and bounded failed aborts. This is a driver
  correctness test, not a silicon bus model.
- `host/r2_grant_trial.py --self-test`: rejects 330 corrupted metadata,
  CRC, ownership and stop fixtures across dividers/SYS ratios. Real trials
  require explicit `--port` and fresh `--out`.
- `host/test_r2_hazard3.py`: explicit instruction emulation, 22 normal/short
  configurations, 180 mapped frames plus 36 deferred-publication frames, 4,560
  slice calls, all 16 initial fills, Gold extremes, tiny rings and 65,536
  shuffle/spread inputs. Fifteen additional repeated-stream cases compare the
  actual candidate C mapper with the runner's scalar reference. This does not
  execute the RISC-V assembly instructions.
- `host/r2_hazard3_trial.py --self-test`: independent PL/Gold reference,
  ring wraps/carry, predicted corruption control and rejection of Arm emulation
  as a Hazard3 result. The standalone image measures arbitrary FEC bits, not
  FEC encoding.

Build from this worktree, without device access:

```sh
make build
export PICO_SDK_PATH="$HOME/.pico-sdk/sdk/2.2.0"
export PICO_TOOLCHAIN_PATH="$HOME/.pico-sdk/toolchain/14_2_Rel1"
cmake -S firmware -B firmware/build-r2 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DIQ4HIST_EXPERIMENT=ON \
  -DCORE0_STACK_SRAM_HI=ON
make build BUILD=firmware/build-r2
make check

# Optional Arm correctness harness. Its timing is not Hazard3 evidence.
cmake -S firmware/r2_hazard3 -B firmware/build-r2-hazard3-arm -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DR2_HAZARD3_EMULATE=ON
cmake --build firmware/build-r2-hazard3-arm
```

Default and experiment `iqbench`/`pvflight`, and the Arm mini image, build
without new compiler warnings. The RISC-V dependency failure is recorded above.
The full check status is recorded at the end of this report after completion.
Historical waveform self-tests may report missing gitignored binary images;
those old hashes are not revalidated by this work.

For the coordinator, the grant build directory is `firmware/build-r2` and UF2 is:

```text
/Users/river/Documents/stanford/SSI/RP2350_IQ_Benchmark/.claude/worktrees/codex-r2-hw-offload/firmware/build-r2/iqbench.uf2
```

At delivery its UF2 SHA256 is
`f6cf768b876c124bae87393d7f07bbf10e8ecca6671d86944ff15d7ba2c2474e`;
ELF SHA256 is
`24a65b25df5fce239d38526c46fe96af82477e5756fc9498ecdcc55c27040ba9`.
Rebuilds may change these identities; the runners archive the actual images and
source hashes. A dirty Git revision alone does not identify source content.
The following are **coordinator instructions, never executed here**. Set `PORT`
to the actual CDC path, load the image through the normal procedure, and use
fresh output directories. Use `info` and `stackcheck` before/after the grant
trials; both scratch images must remain intact and neither stack paint exhausted.

```sh
python3 host/iqbench.py --port "$PORT" raw info
python3 host/iqbench.py --port "$PORT" raw stackcheck

# Both rates and priorities; absence of foreground CSR service spans d1 burst.
python3 host/r2_grant_trial.py --port "$PORT" --build firmware/build-r2 \
  --out results/r2-hw-offload/grants-clean --batches 8 --cpws 2 1 \
  --gap-us 6000 --priorities 0 13 --repeats 6

# Mandatory independent-reader starvation control.
python3 host/r2_grant_trial.py --port "$PORT" --build firmware/build-r2 \
  --out results/r2-hw-offload/grants-short-prefix --batches 8 --cpws 1 \
  --gap-us 6000 --priorities 13 --fault 1 --repeats 3

python3 host/iqbench.py --port "$PORT" raw stackcheck
```

Manual equivalents are `r2grant 8 1 6000 0 13` and
`r2grant 8 1 6000 1 13`. A positive needs `errors=txstall_mask=mismatches=0`,
all four issued/captured counts 4096, `null_mask=input_mask` with four input
channels, `guards_ok=released=true`, and `core1_tasks=0`. At SYS128/d1 its
programmed echo duration is 4096 us; elapsed includes at least the 6000-us gap.
I/Q CRC32s are **2165502144 /138477260**, repeated for the second parity.
The runner checks independent Python CRCs and the exact requested metadata.

The negative publishes one fewer batch only to reader3. It must return
`errors=36` (capture TIMEOUT4 plus source SEAM32), exactly one TXSTALL,
issued/captured `[4096,4096,4096,3584]`, all four null stops, correct guards and
successful release. Its final Q CRC is **1181902901**; the other three are
unchanged. No out-of-prefix source read or unrelated fault is allowed. The
runner returns zero only when that expected failure is exposed. Repeating
finite bursts for 60 seconds would still not establish continuous credit
service, so no such qualification label is offered.

For a real Hazard3 build, first provide a separately installed SDK-compatible
RISC-V toolchain directory in `R2_RISCV_TOOLCHAIN`. That dependency is absent
here. Use a separate cache from the Arm harness:

```sh
PICO_TOOLCHAIN_PATH="$R2_RISCV_TOOLCHAIN" \
cmake -S firmware/r2_hazard3 -B firmware/build-r2-hazard3 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DPICO_PLATFORM=rp2350-riscv \
  -DR2_HAZARD3_EMULATE=OFF
cmake --build firmware/build-r2-hazard3
```

The intended UF2 path is
`firmware/build-r2-hazard3/r2map.uf2` under this worktree; **that file does not
exist here**. Inspect its ELF disassembly for actual `pack`, `zip`, `brev8`,
`rev8` instructions before loading. This project intentionally uses SDK startup
clocks and reports `sys_hz`; it makes no SYS128 or waveform-rate claim.

After building/loading that image, the coordinator runs:

```sh
python3 host/r2_hazard3_trial.py --port "$PORT" \
  --build firmware/build-r2-hazard3 --codes 5 8 6 --frames 100 \
  --faults 0 1 --out results/r2-hw-offload/hazard3-map

# Offline expected words/hashes, without opening a device.
python3 host/r2_hazard3_trial.py --reference --codes 5 8 6 --frames 100 --faults 0 1
```

A positive requires `arch=hazard3`, `candidate=zbkb`, `equal=true`, no runner
faults, and every independent ring hash/carry/count match. For 100 frames expect
138,712 complete groups, fill8, pending I/Q `[45934,6452]`. Expected FNV32 hashes:

| Code | I / Q, each both kernels | Negative candidate I hash |
|---|---|---:|
| 5 | 3678802496 /3886983059 | 354039255 |
| 8 | 3243413544 /2917792135 | 1603191839 |
| 6 | 3618826994 /2490076409 | 2687227697 |

The corruption control requires `equal=false`, exactly its predicted changed
candidate I hash, and unchanged Q/pending metadata. It is accepted as a detected
control. `cycles[0]`/`cycles[1]` compare portable/Zbkb on the **same Hazard3
core**. Record maximum slices and cycles/frame; a speedup is an outcome, not a
correctness pass condition. It is not an Arm-versus-Hazard3 full-FEC comparison.
For optional Arm harness correctness, load
`firmware/build-r2-hazard3-arm/r2map.uf2`, change `--build` accordingly and add
`--allow-emulated`; retain its `timing_scope=emulation-only` classification.

## Integration decision and remaining risks

The grant experiment tests a hardware seam, and the mapper experiment tests an
instruction substitution. Neither includes live ingress, BCH/LDPC, B6 output,
concurrent two-core FEC, capture arbitration or a complete on-air deadline.
Preserve failures at each gate. A host-native duration is never an M33 or
Hazard3 cycle measurement.

The present one-lane PV-SPI path caps useful ingress at approximately 30.8697 Mb/s
according to C3's analysis, below even the 2/3 payload requirement above. Faster
ingress and its framing/checksum cost remain necessary independently of CPU fit.
USB Full Speed cannot carry these payloads. Precomputed PRBS payloads do not
establish a live transmitter result.

The older frontier's proposed second chain counted DMA transfers while omitting
the second FASTPERI access of FIFO→trigger transfers. Fourteen-bit one-symbol
rows require more than the port can supply at d1; more chains or higher SYS at
the same `d` cannot fix that ratio. OUT8 B6 has an average budget, but worst-case
handoff latency still requires device evidence. DMA read priority, SRAM placement,
XIP fetches and USB tails can consume any apparent compute margin.

No AFE pad timing, DAC capture, RF spectrum/EVM, receiver acquisition, thermal
margin or long-soak result is added by R2. The small held-model margin of the
existing 8-bit shaping table is unchanged.

Intended small logical commits for the coordinator; none was created:

1. `firmware/src/r2grant.{c,h}`, `firmware/src/bench_r2grant.c`,
   `firmware/src/main.c`, `firmware/CMakeLists.txt`, `host/test_r2grant.py`,
   `host/test_bench_r2grant.py`, `host/r2_grant_trial.py`, `Makefile`:
   **Prototype finite independent DMA grants with exact echo and sentinel tests.**
2. `firmware/src/dvbs2_8psk.c`, `firmware/src/r2_hazard3_bits.h`,
   `firmware/r2_hazard3/{CMakeLists.txt,main.c}`, `host/test_r2_hazard3.py`,
   `host/r2_hazard3_trial.py`, `Makefile`:
   **Prototype opt-in Hazard3 zip mapper comparison and offline references.**
3. `docs/rd/r2-hw-offload.md`:
   **Document hardware-stage survey, budgets, rejected routes and measurement gates.**

Final verification completed here: **`make check` passed all 93 Python
invocations**, including the five added R2 invocations. The latest priority-mask
mapping also passed a separate five-group production-driver rerun. Final default
and experiment builds pass without new warnings. `git diff --check` and all
relative Markdown links pass. No device measurement or RISC-V target build is
substituted for these native/Arm-build results.
