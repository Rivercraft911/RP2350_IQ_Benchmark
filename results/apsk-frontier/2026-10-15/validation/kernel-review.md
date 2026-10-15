# APSK kernel handoff, 2026-10-15

All changes are new component files; existing waveform tables, quantizers and
public pipeline behavior were not changed by this agent. No USB access, shared
build, flashing, commit or release was performed by this agent.

Exception after root explicitly transferred ownership: the32APSK builder was
changed to avoid a large temporary stack table. Details appear at the end.

## Exact 16APSK schedules

`firmware/src/iq16fast.h` / `iq16fast_asm.S` expose
`iq16fast_sparse_asm` and `iq16fast_pair_asm`, with the existing native8 signature,
136 KiB table layout, input/history contract and RAW_PAIRS output CRCs.

The L8 old3 partial cannot contribute to output symbol three: its ages are10,9,8,
all outside0..7. Omitting that word and its addition is exact for every valid
table and all six supported radius ratios. Register allocation keeps the middle
row's final word alive, so no compensating MOV is introduced. The pair schedule
unrolls two input words per branch; the existing C prefix handles at most three
words, making the remaining count even.

Both schedules use a48-byte assembler frame, no new data RAM and cached flash.
Arm GCC14.2 target compilation with `-Wall -Wextra -Werror` passes. Symbol sizes
are252 bytes sparse and438 bytes pair (692-byte object including alignment).

`host/test_iq16fast.py` passes372 independent FIR/actual-assembler/AAPCS cases,
executing5,958,096 modeled instructions. It covers all six ratios, all4096 axis
keys, ignored input padding, short/odd/even blocks and both null/custom history.
Modeled instruction counts for256 input words are13230 baseline,12722 sparse and
12215 pair. Those counts are not cycle predictions. Parent root owns Pico timing.

## Optional planar16APSK

`iq16fast_planar.h` / `iq16fast_planar_asm.S` expose
`iq16fast_planar_asm`. The output group is explicitly
I01,I23,I45,I67,Q01,Q23,Q45,Q67, suitable for a separate dual-axis consumer.
Tables and all sample values remain exactly the same as native8. Two prefix
blocks at most are transposed in place; then one STM writes each axis row.
The register schedule directly produces consecutive r0..r3 without a MOV.

This284-byte cached-flash function has no extra data RAM and a48-byte frame.
`host/test_iq16fast_planar.py` passes133 guarded FIR/AAPCS cases across six ratios,
with2,961,564 model steps. Its no-writeback STM instructions are modeled by the
equivalent ordered scalar stores. Arm GCC target assembly passes Werror.
The parent measured it slower than the paired RAW_PAIRS schedule, so it should
remain a recorded exploration rather than be presented as the fastest kernel.

## Equal-density32APSK pair input

`iq32pairkey.h` / `iq32pairkey.c` / `iq32pairkey_asm.S` add an alternate input
representation to the shared117,912-byte native333 tables and unchanged output.
Each axis halfword stores pair=c0+17*c1 in bits0..8 and last=c1 in bits9..13.
There are still two complex symbols per32-bit input word (2 bytes/symbol).

For consecutive input words a,b,c,d,e, both I/Q lanes are calculated together:

    old = last(a) +17*pair(b)
    middle = pair(c) +289*(pair(d)-17*last(d))
    future = last(d) +17*pair(e)

`pair(d)-17*last(d)` equals first(d), is nonnegative and at most16. Every final
key is at most4912, and no intermediate low-lane carry or borrow can contaminate
the upper Q lane. The assembler retains last(d) through the middle calculation
and reuses it for future, removing10 instructions per two-symbol hot loop:
52 instructions instead of the baseline62. A BIC with encodable immediate
0xfe00fe00 extracts the9-bit lanes; direct AND0x01ff01ff is not encodable.

The public converter supports exact in-place conversion. Four non-null history
words must also be converted; a null history directly creates the encoded unit
pilot0x0c6c0c6c. The assembler uses at most four C prefix blocks, then accesses
only contiguous encoded history. C converter stack4 bytes, C leaf56 bytes,
assembler48-byte frame, C text352 bytes and assembler256 bytes; zero new BSS.
Arm GCC target C/assembly Werror passes.

`host/test_iq32pairkey.py` passes705 native partition and actual-source ISA/AAPCS
cases,98,260 exhaustive paired-lane algebra checks and8,702,655 modeled
instructions over all five radius-ratio options. Independent FIR references
cover every partial, history, padding, short/tail and actual32-point fixture.
Default5/6 fixture: native inputCRC1106835987, encoded inputCRC3117192176,
RAW outputCRC2048936984, packed outputCRC1960087962. Both output CRCs are identical
to the baseline32APSK component.

Standalone conversion is real work. The root component benchmark times it and
history conversion separately and reports conversion+shape as well as shape.
A future mapper may emit this representation directly: precompute coordinate
word C for each constellation label, tableFirst=C and tableLast=17*C+(C<<9).
The pair word is tableFirst[label0]+tableLast[label1]. This is two table loads
and one add, comparable to raw coordinates' two loads and shift/OR. All four
Gold rotations can be folded into the table index. This algebra does not claim
that a complete DVB bit-label/interleaver/PL mapper has been implemented.

## Independent reviews

The sibling32APSK baseline was read for coordinate/radius construction, integer
range, table indexing, history ages and assembler register lifetimes. No material
defect found. It correctly preserves the R3=1 alphabet, including pilot code6;
the native packed radix17 arithmetic cannot carry between lanes because each
intermediate is bounded by4912.

The rolling-triple sibling format's five-key history is also correct. Keeping
encoded indices3..7 of the eight history coordinates supplies keys ending at
symbols−5..−1; all depend only on available history, so the encoder's initial
default-pilot assumption has been discarded. Its three-block prefix advances
six symbols before the ASM reads five words back.

The root `bench32.c` preparation and timing were reviewed read-only. The native
input fixture remains untouched, histories match each representation, all modes
CRC the same waveform, and converted data in cfg.ti do not overlap the shared
three tables. Summed conversion+shape includes the actual prepare/helper/history
overhead and timing-read overhead, so it is slightly conservative. No material
comparison flaw found. Complete modem, RF receiver, AFE and spectral-image
qualification remain outside these component proofs.

## Material stack correction

Read-only Ninja target command recipes were re-executed with `-fstack-usage`,
redirecting all objects/dependencies to `/tmp/yapogee-apsk-stack-audit/`.
The actual SDK Release flags report main336 bytes (including inlined dispatch),
bench32native8 488, bench32out400, and the original builder1272. Thus the original
component call chain requires2096 bytes before IRQ entry, exceeding the configured
2 KiB stack; output setup leaves just40 bytes. Correct CRCs from those images do
not establish stack safety. The root is retaining them as pre-fix candidates.

With explicit root authorization, `iq32native8.c` now recomputes each rounded
coordinate/tap product while building a partial row instead of keeping17*16
scaled products on the stack. It uses the identical signed64-bit multiply,
rounding offset and shift before summation. This is setup work, with no hot-leaf
change and no extra persistent RAM.

The corrected builder uses176 bytes under the same SDK flags, saving1096 bytes.
The simple component chain is1000 bytes and output setup912 bytes, leaving about
1 KiB for nested setup/USB work instead of exceeding the stack. This is a static
frame audit, not a claim of measuring all possible interrupt high-water marks.
The IRQ code inspected includes USB worker32 bytes, tud_task_ext80, DCD IRQ40,
buffer handling56 and event handler48, plus hardware exception frames.

All147,390 table words across all five radius-ratio choices were compared against
the original builder and are identical. Every original table CRC is preserved.
`host/test_iq32native8.py` then passes3,919,779 independent component checks,
with unchanged full output CRCs and floating-point rounding-error bounds. The
corrected actual source equals the isolated176-byte-stack candidate byte for byte
and is frozen for the root's new hardware image.
