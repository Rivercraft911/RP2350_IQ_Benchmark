# Future common native 8PSK/16APSK coordinate profile

2026-10-15 mathematical sanity check. **Not implemented, timed, receiver-tested or RF-qualified.** This is a memory-oriented future option after the maintained single-modulation components, not a replacement for their current performance evidence. Reproduce the small alphabet/storage/symmetry check with `python3 results/waveforms/2026-10-15/native8-16-union.py` from the repository root; its saved output is `native8-16-union.json` alongside this report. The existing core models and GNU crosscheck were not changed.

## Clarification of the historical notebook

The generic coordinate-chunk **mechanism** can serve different finite alphabets after tables and coordinate mappings are rebuilt. The fixed native 16APSK `R2=1` tables cannot directly shape native 8PSK: their eight axis values are `±a, ±b, ±c, ±d`, where `a=sin(15°)`, `b=1/√2`, `c=cos(15°)` and `d=1/(γ√2)`. Native 8PSK additionally requires `0, ±1`, which those tables omit. No simultaneous shared fixed alphabet or live 8PSK↔16APSK transition was demonstrated by the historical proposal. The maintained two-plane 8PSK profile is globally rotated by π/8, which introduces another orientation distinction.

## A shared alphabet that does contain both modes

At one fixed 16APSK radius ratio γ, use native orientation throughout and the union

`U = {0, ±1, ±a, ±b, ±c, ±d}`.

There are exactly 11 levels for each of the six specified 16APSK γ values. The sanity script checks all six separately, with `R2=1` for 16APSK and radius one for 8PSK. Native unit PLHEADER/pilot coordinates `±b` already belong to the union. Gold quarter-turns only exchange/negate axes and preserve it. The union's existence concerns real-axis representation; it does not replace the specified constellation labels, interleaver, PL scrambling or MODCOD/TYPE generation.

For L9, two samples/symbol and four output symbols/group, each axis uses 12 chronological input coordinates and returns eight real samples. Split the history into four three-coordinate chunks. A dense radix-11 key has `11³=1331` rows, each containing eight signed 16-bit samples (16 bytes).

| Storage | Bytes | KiB |
|---|---:|---:|
| One three-coordinate table | 21,296 | 20.796875 |
| Four independent chunk tables | 85,184 | 83.1875 |
| Two symmetry-shared tables | 42,592 | 41.59375 |
| One 12-bit packed-key → radix-11 map | 8,192 | 8 |
| Two tables plus that map | 50,784 | 49.59375 |

Encode each coordinate as a nibble 0..10. A three-coordinate packed key therefore has 12 bits; only 1331 of its 4096 entries are valid. Invalid nibbles 11..15 need rejection/sentinel handling. Its dense key is `x+11y+121z`. One 4096-entry `uint16` conversion map suffices for all four chunks and both axes. The stream representation would need four bits/coordinate and new history/key extraction; the current three-bit 16APSK representation cannot simply be retained.

If `A[r,k]` maps chronological history coordinate k to output sample r, symmetric 18-tap FIR coefficients imply `A[r,k]=A[7-r,11-k]`. Thus chunks 0↔3 share one table and chunks 1↔2 share another, provided the three input digits and eight output samples are reversed on the mirrored lookup. The script independently checks all 96 matrix elements. A deterministic identical coefficient/product/table rounding rule preserves this identity. Reversing the three packed nibbles before the same conversion map needs no second map. If a separate dense-key reversal lookup is preferred, 1331 `uint16` entries add 2662 bytes. These are storage alternatives, not instruction/cycle estimates.

## Work and numeric costs

This design performs **eight axis-row lookups per four-symbol group**, versus six in the current three-chunk 16APSK design. Each row has four 32-bit words, so row reads rise from 24 to 32 words/group; four-part summation also needs more packed additions. Conversion, key extraction and mirrored-sample reversal add work. Memory is attractive, but no speed improvement or viable full-pipeline symbol rate follows from the table size.

The underlying FIR decomposition is exact before quantization. Rounding four independent chunk contributions to integer output samples introduces a conservative ≤2-LSB axis error relative to their unrounded sum, compared with ≤1.5 LSB for three chunks. If each coordinate-times-Q8-coefficient product is also rounded before accumulation, at most nine nonzero products contribute to a sample: add `9/(2·256)=0.017578125` LSB relative to the unrounded Q8 FIR. This excludes the original tap-design/quantization error. A different quantization rule needs its own bound.

The union contains `±1`, exceeding the largest native 16APSK coordinate `cos(15°)`. Recompute final and intermediate headroom instead of carrying over a 16APSK-only proof. With the archive's illustrative symmetric L9 pre-emphasis taps, the maximum phase L1 sum is 7300.234375 LSB; the conservative four-chunk/product-rounding bound is 7302.251953125 LSB, below 8191. This is a conditional mathematical bound for those taps and scaling, not qualification of a future implementation or of all possible tap profiles. Signed packed intermediates and 14-bit output range still need verification in actual code.

## Transition requirements and priority

A prospective common profile must start and remain in one native constellation orientation, use the same SPS, pulse taps, scale, outer-radius policy and fixed γ, and preserve coordinate/filter history and partial groups across PLFRAMEs. Whole-frame header/pilot/data mapping and correct MODCOD/TYPE remain necessary. The existing globally π/8-rotated two-plane stream cannot enter this native profile by changing only the next payload's mapping. A clean stop/drain/restart is the conservative profile-change boundary unless a separate coherent transition is proved.

Changing 16APSK code rate normally changes the required γ. The 11-level count applies to one γ at a time; different γ values require table/profile rebuilding or a larger union whose size and timing must be analyzed anew. It does not make arbitrary 16APSK code-rate switching free. Unit-average-data-energy APSK normalization likewise changes this alphabet; it must be modeled explicitly.

Rank this as a **future memory-oriented common native 8/16 option**: mathematically viable storage/symmetry, with more per-group work and new mapper/history machinery. A native scalar full-frame reference, exact rounding/headroom and cross-boundary checks, component timing under real DMA, and whole-pipeline/receiver validation would precede any live common-profile claim. There is no demonstrated native APSK transition here.
