# APSK: measured budgets and the next useful experiments

Read-only analysis of build 4, 2026-10-15. No firmware changes, builds, or USB use in this pass. The 60-second qualification runs were still underway when this note was written; use their final records for qualification claims.

## Measured work near the 150 MHz frontier

Source: `results/apsk-fec/2026-10-15/bulk-frontier-1/runs.jsonl`, SHA256 `3fb0733dc67fdb8ff03ae392ff83bf146249a631f3c458a11fd5285ead49411d`. Selected rows are `16apsk-fec-150000-cpw9` and `32apsk-fec-150000-cpw14`: both 3-second PASS, zero recorded faults and exact captured samples. Normal 5/6, pilots, outer radius 1, continuous frame history, live on-chip PV-SPI source.

All costs below are cycles per transmitted complex symbol, including headers and pilots.

| Work | 16APSK | 32APSK |
|---|---:|---:|
| C1 BB transport, validation, header and byte conversion | 1.74 | 2.09 |
| C1 BB scrambling + BCH | 7.05 | 8.74 |
| C1 LDPC | 7.80 | 9.66 |
| C1 PL interleave, constellation, Gold and native packing | 18.08 | 32.35 |
| **C1 total** | **34.65–34.70** | **52.85–52.94** |
| C0 native-to-offset preparation | 0 | 13.29 |
| C0 shaping | 20.54 | 19.91 |
| C0 publication/control | 0.11 | 0.12 |
| **C0 total** | **20.66** | **33.31** |
| Measured symbol rate, Msym/s | 4.1667 | 2.6786 |
| C1 recorded busy | 96.48% | 94.46% |
| C0 recorded work busy | 57.37% | 59.49% |

C0 means are exact block totals. C1 includes one final partial attempt: 751 runtime attempts / 750 complete frames for 16; 603 / 602 for 32. The interval divides recorded work by attempted versus completed frame-symbol counts. It bounds the small end-of-run denominator ambiguity, rather than pretending each attempted map was complete. Credit waiting is excluded from C1 MAP work. The first few FEC CRC checks are included in the recorded stage costs. This is sufficient for architecture decisions, not a per-instruction model.

The current division is encoder-limited. Cutting C0 preparation alone does not increase throughput while C1 remains saturated. For perspective, BB handling alone is only about 5.0% / 4.0% of C1; it is not the main target.

## Estimated CPU headroom from balancing work

At the same measured costs, perfect balancing gives `Rs_ideal = 2 * 150e6 / (C0 + C1)`:

| Estimate at 150 MHz | 16APSK | 32APSK |
|---|---:|---:|
| Current fixed-stage CPU ceiling, Msym/s | 4.32–4.33 | 2.83–2.84 |
| Perfectly balanced CPU ceiling, Msym/s | **5.42** | **3.48** |
| Gain over the fixed-stage ceiling | about 25% | about 23% |
| Gain over the measured passing rate | about 30% | about 30% |
| PL work to move from C1 to C0, cycles per total symbol | 7.00 | 9.77 |
| Fraction of PL work to move | about 39% | about 30% |

These are engineering estimates, not measured rates or hardware limits. They assume unchanged per-symbol costs, perfect overlap, no added copy/synchronization cost, no extra SRAM contention, and enough buffering for worst-case deadlines. Integer CPW further quantizes rates: 16 CPW8 would be 4.6875 Msym/s, CPW7 5.3571; 32 CPW12 would be 3.125, CPW11 3.4091. A passing average budget alone cannot qualify any of them.

Moving *all* PL mapping to C0 is a bad partition: it would load C0 to about 38.73 / 65.67 cycles per symbol, predicting only 3.87 / 2.28 Msym/s. Cooperation should transfer a bounded share of mapping work while keeping output deadlines first.

## 1. Build random-access PL jobs over two owned FE slots

This is the largest clear architecture opportunity. Once FEC completes, interleaving, constellation lookup and Gold rotation are deterministic functions of frame identity and symbol position. Gold is precomputed and headers/pilots are known. There is no mathematical requirement for one core to emit the entire PLFRAME serially. FIR history is a downstream dependency; retain the existing eight-symbol history and ordered output ownership.

The proposed memory actually fits in the existing 32 KiB `cfg.ti` arena:

| Arena allocation | 16APSK words | 32APSK words |
|---|---:|---:|
| Existing old-coordinate table | 2048 | 0 |
| Maximum Gold arena for this modulation | 1040 | 830 |
| Two FE slots, including current guards: `2 * DVBS2_FE_WORDS` | 4054 | 4054 |
| **Used / 8192 words** | **7142 / 8192** | **4884 / 8192** |
| **Spare bytes** | **4200** | **13232** |

`DVBS2_FE_WORDS = 64800/32 + 2 = 2027`, so each slot is **8108 bytes**, not just the 8100 payload bytes. All offsets are uint32 aligned. For 16, offsets in words can be old=0, Gold=2048, FE0=3088, FE1=5115, end=7142. A 1 KiB native output scratch block would also fit if the chosen job API needs one; it is not included in the table above. Context remains in the existing cap-buffer tail, PV-SPI emulation/key scratch in `cfg.tq`, and the two large shaping tables in `ring_buf`.

Use existing `dvbs2_frame_begin_into(bb, slot)` and `dvbs2_frame_ldpc_into(slot)` to construct FE directly in an available slot. No FE copy is inherently required. If a simpler first prototype copies from the old global FE, count a full 8108-byte copy per frame and measure it; never reuse the source during that copy. Only C1 should run FEC in this proposal: BCH/LDPC code selection and workspaces are global, so two concurrent FEC calls are not safe without further changes. PL readers only need immutable FE and profile data, and can overlap one FEC writer using a different slot.

Required ownership rules:

- FE state must progress FREE -> BUILDING -> READY -> readers finished -> FREE, with publication barriers and explicit frame identity. The next FEC must not overwrite any slot still read by a PL job.
- Each job needs absolute output-symbol/native-word position and a disjoint destination reservation. Publish only the longest contiguous completed prefix to the consumer; completing a later job does not make an earlier gap safe.
- 16 frame length 16686 is 2 modulo 4, so successive frames begin at native fills 0,2,0,2. A word mixing the last two symbols of one frame and the first two of the next needs exactly one owner or a tiny explicit boundary join. Per-frame flush/padding would corrupt the waveform.
- 32 frame length 13338 is even, so its standard two-symbol words align across frames. Prefix/partial APIs still need their documented general behavior.
- C0 must claim only bounded work when enough output lead remains, with worst-case job timing and backpressure tests. Task bookkeeping may cost more than the saved work if jobs are too small.

Acceptance: independent finite FEC->PL->FIR comparison including mixed-frame words, two-slot reuse/stall/stop stress, then identical capture-backed frontiers and 60-second tests. A sensible first target is at least one CPW step: 16 CPW8 (+12.5%) or 32 CPW13 (+7.7%). The ideal ceilings are not targets to advertise before measurement.

## 2. Keep a bounded PL-kernel experiment, with an explicit gain gate

PL still consumes about 52% / 61% of C1 work. To raise the fixed-stage CPU ceiling by 5%, a candidate must save at least **1.65 / 2.52 cycles per transmitted symbol**. That means about **9.1% / 7.8% of MAP cost**, enough to justify one small fixed-profile kernel rather than numerous tiny rewrites.

Candidate scope: expose one data-run leaf with fill and output pointer held in registers; compare a 16 fixed-fill0/2 packer, or a 32 larger register transpose/point-gather batch, against the current exact implementation. Preserve the public stream semantics and use a fallback for unusual prefixes. No 127-term generated Boolean expression or large new RAM table without a measured gain. The current register-pair transpose is already present; do not propose rediscovering it as a new optimization.

Acceptance: native/Arm exactness, stack and code-size audit, matched MAP timing under the real concurrent pipeline, and a capture-backed rate step. Reject it if it only improves a standalone microbenchmark or misses the gain gate. This path is simpler than changing ownership, but its remaining upside is less certain.

## 3. Profile normal5/6 LDPC packing before attempting one structural rewrite

The existing encoder already uses rotated QC groups, wordwise prefix work and a tiled Arm transpose. It also has a specialized `ldpc_pack60` for normal2/3. Normal5/6 has q=30 and still uses the generic transpose plus scattered `or_bits` tail pack. That makes a direct q30 packer the one specific FEC experiment worth measuring before broader changes.

Use the existing LDPC checkpoints to split group XORs, prefix accumulation and final packing. The hard stop criterion is useful: a 5% pipeline improvement from LDPC alone requires saving about **21% / 26% of all LDPC cycles** for 16 / 32. If packing accounts for less than that, even removing it entirely cannot meet both targets. If it does dominate, try a contiguous q30 column packer, preserve guards and exact parity, and compare against the generic path. A 30% reduction in whole-LDPC cost would predict about 7% / 6% improvement in the current encoder ceiling; that is a target hypothesis, not a claim of available speedup.

Do this after the ownership design or as a separate measured branch. Avoid changing BCH/LDPC bit order or the canonical FE representation at the same time as parallel PL work; independent references and clear failure attribution are more valuable than stacking unmeasured changes.

The useful next review should choose one of these experiments, assign its ownership/API and acceptance criterion, then measure. This note does not claim RF quality, receiver interoperability, external-source SPI qualification, or a new sustained throughput result.
