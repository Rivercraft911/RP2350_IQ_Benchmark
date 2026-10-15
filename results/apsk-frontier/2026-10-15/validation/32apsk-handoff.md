# Exact32APSK component and shared native-mode frontier

2026-10-15. New component sources and native proofs; root owns firmware integration, silicon timing, DMA/PIO capture and final qualification. No device/RF timing is invented here.

## What changed

`iq32native8` extends exact coordinate-chunk shaping to native32APSK. Use a17-level real-axis alphabet (zero plus eight signed magnitudes), L8/N2 and two output symbols per group. Its nine chronological coordinates split into three3-coordinate partial FIRs. Each table has17³=4913 rows ×4 real samples/8bytes =39,304bytes. Three native tables need117,912bytes and fit the existing128KiB ring arena. The hot loop avoids row/sample reversal; mathematical edge symmetry could reduce this to78,608bytes, but that lower-memory consumer is not implemented or timed.

All five standard32APSK ring-ratio pairs are accepted. `rate_index=2` is5/6, gamma1=2.64, gamma2=4.64, R3=1. Coordinates are Q30, product sums Q8, and each of the three partial FIRs is rounded independently. This is exact constellation geometry subject to explicit fixed-point rounding, not an approximate constellation. The pulse coefficients are the previously frozen L8/N2 pulse with SHA256 `16936cb50f5de62e68b10367624b868afea4ad49f8dfc4481526de4712fda5ae`; the32APSK chunk quantizer has not independently inherited a prior binary-table PSD certificate.

Native32APSK, native8PSK and QPSK share this fixed17-level alphabet: native8PSK requires only0,±1,±1/sqrt2, already present. This enables a common component pulse profile without extra tables or an orientation jump. A new mixed-mode native proof covers360 transitions, odd symbol boundaries, all five fixed-gamma profiles, all Gold quarter-turns, and actual continuous filter history. It uses geometric symbols, not a complete DVB-S2 interleaver/PL mapper or an RF receiver. The existing faster globallypi/8-rotated8PSK engine remains a different profile.

## The larger optimization

`iq32keys` moves rolling radix17 key construction upstream. One32-bit key word per complex symbol contains two13-bit row keys, I/Q in16-bit lanes. For a two-symbol output group starting atn, the three chunk keys are the triples ending atn−5,n−2,n+1. Thus the assembly loop loads only three source words instead of constructing three paired keys. Its actual source has33 hot-loop instructions/two symbols against62 in the native-coordinate baseline. These instruction counts are not silicon cycle measurements.

The tradeoff is explicit: key input uses4bytes/symbol instead of2. The current standalone converter consumes real CPU and is timed separately by root. A future mapper can construct keys directly, but no mapper integration or free conversion claim is made. A32KiB key arena holds8192symbols, too small to retain an entire normal32APSK PLFRAME; incremental publication or a larger arena is needed for a full system. The continuous component benchmark instead uses a declared cyclic8192-symbol fixture and encodes it once before runtime timing.

A companion agent independently implemented the no-growth dense-pair representation (`iq32pairkey`): two native coordinate codes become pair=first+17last plus a redundant last code in the remaining halfword bits. It keeps2bytes/symbol and removes part of the address work. Its future direct mapper can emit a pair from two point-table loads and one addition; a code table plus a17code+(code<<9) table cost128 additional bytes, or512 with all four Gold turns expanded. Root measures these alternatives rather than inferring the winner from instruction counts.

## Numeric and memory safety

Exhaustive actual table rows across all5 gamma pairs and all17³ coordinate triples pass an independent FIR model. Every partial and every packed-add stage has a conservative axis bound≤7301LSB, inside signed14-bit range. The coordinate-independent bound also covers QPSK/pilot insertions and every history, even combinations that do not form valid complex constellation symbols. Per-chunk rounding contributes≤1.5LSB; Q8 product rounding adds≤8/512LSB. The worst tested error against independently evaluated trigonometric coordinates and Q8 FIR is1.48835LSB. This excludes the original pulse-design quantization/error and analog chain.

The paired radix arithmetic cannot carry between16-bit lanes: intermediate/final key≤4912. The native hot leaf requires valid coordinate codes0..16;17..31 are invalid. Ignored input padding bits are independently tested. Zero-block calls do not dereference input/output/table pointers.

M33 GCC14.2, -O3/-Werror: native C1764Btext, native ASM292B; key C524B, key ASM180B; data/BSS0. Native builder176B stack; native C64B, key encode12B, key-history helper48B, key C72B; both ASM frames48B. The32out wrapper is400B, so builder+wrapper uses576B before its caller; root measured main336B, making this construction chain912B before interrupts. The original1272B builder was replaced after the root found the2KiB stack margin unsafe; products now round identically on demand without a1KiB scaled stack array. All actual compilation objects/dependencies were isolated in/tmp.

## Contracts for root

Native API: `iq32native8_build(tables,rate)`, `iq32native8_fill(raw,blocks,seed)`, `iq32native8_kernel_c/asm(out,raw,blocks,prev4,tables)`. Each raw input word packs two5-bit coordinate codes per axis (Ibits0..9,Qbits16..25); blocks count two-symbol groups; output is4 RAW_PAIRS words/group. Default history is repeated unit-pilot code6.

Rolling-key API: `iq32keys_encode(out2blocks,raw,blocks,prev_word)`, `iq32keys_history(out5,prev4raw)`, `iq32keys_kernel_c/asm(out,keys,blocks,prev5keys,tables)`. NULL history remains unit-pilot. The ASM prefix establishes contiguous history through at most3 blocks; key input then advances2words/group. The native-coordinate ASM prefix needs at most4 blocks. Both preserve AAPCS registers and use48-byte frames.

Default8192-block, seed0x1234abcd fixture: raw inputCRC1106835987; tables old/middle/futureCRCs3255586384/916828675/1817262751; RAW_PAIRS outputCRC2048936984; masked PACKED outputCRC1960087962; rolling inputCRC189197925 with default unit-pilot prehistory. Root component timings should divide by2symbols/group and include input-conversion timing as a separate metric.

Continuous32out API: `bench_32out(cpw,ms,kernel)`; kernel0=keys_C,1=keys_ASM; cpw4/6/8;128/150MHz,100..60000ms, core1idle. Raw4096-word source occupies cfg.ti, encoded8192keys in in_buf, three tables in ring_buf, four2048-word DMA output blocks plus4096-word capture in cap_buf. The raw geometric fixture is NOT a DVB bit-label mapping. Cyclic rawCRC2979756864, rolling inputCRC2091885109, cyclic outputRAWCRC415574042, periodic GPIO-halfwordCRC2015101740. Key conversion runs once during setup and is explicitly excluded from runtime shaper/producer fractions. IRQs remain enabled during the output run. Failed shape calls are counted separately so negative-control work accounting is honest.

`host/iq32out_trial.py` never builds/flashes. It validates all8192 contiguous captured halfwords against one bounded offset tied to the source cursor, checks cyclic-tail history and every configuration/CRC/fault field, and supports expected-deadline-fault controls. The actual C block helper was compiled and checked for106 blocks,434176 GPIO halfwords,12source wraps, arena guards, and uint32 sequence wrap. Runner selftest includes missing/duplicate/truncated transport, CRC errors, sample corruption, wrong bounded offset, reset history, missing faults and incorrect timing/configuration/accounting controls.

## Native evidence

- `host/test_iq32native8.py`:3,919,779 checks, all5gamma profiles, every table row, custom/default history, partitions and invalid-build canaries.
- `host/test_iq32native8_asm.py`:180actual-source ISA/AAPCS cases,336555 instructions.
- `host/test_iq32keys.py`:190native/ISA cases,123200 instructions, arbitrary encoder/shaper partitions and continuous history.
- `host/test_iq32mix.py`:10mixed-mode streams,360mode boundaries, continuous pulse and partial groups; no completeDVBframes.
- `host/test_iq32out.py`:106actualhelper calls/434176GPIO halfwords; `host/iq32out_trial.py --self-test`:12capture cases and8metadata profiles.

Native result JSONs are `/tmp/yapogee-iq32native8-tests.json`, `...-asm-tests.json`, `/tmp/yapogee-iq32keys-tests.json`, `/tmp/yapogee-iq32mix-tests.json`, `/tmp/yapogee-iq32out-tests.json`, `/tmp/yapogee-iq32out-native-tests.json`.

## Normalization source and limits

[ETSI EN302307-1 V1.4.1](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf), §5.4.4/Table10, explicitly permits R3=1 as an alternative to unit average data energy; §5.5.2 defines unit-radius header coordinates. At5/6 this selected data constellation has average energy0.6272016201, so unit pilots/header are2.02593dB above average data energy. The choice must remain metadata, not silently changed to Edata=1. Switching gamma or the amplitude policy changes table values; fixed tables do not make arbitrary code-rate/profile switching free. These components do not provide full FEC/PL mapping, analog filtering/image suppression, RF power linearity, receiver lock or an operational satellite modem.
