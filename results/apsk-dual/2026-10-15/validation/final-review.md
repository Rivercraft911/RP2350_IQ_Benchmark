# Final APSK review

Read-only review on 15 October 2026. No shared build, USB access, or repository edits.

## Finding resolved

The previously reported P2 empty-selection runner bug is fixed. `selected_profiles` computes the compatible profile list, `main()` rejects an empty list before creating output or connecting USB, and the execution loop consumes exactly that validated list. Self-tests cover incompatible selections and the complete mixed selection. No material correctness finding remains open from this review.

The original bug was reproduced through the actual `main()` using an in-memory fake Board: incompatible selections emitted only info/clock commands yet wrote success. Recorded device campaigns used valid combinations and were unaffected.

## Firmware and ownership

No material firmware bug found.

- The scaled pair path builds all five history keys in ordinary units, shifts those keys and mapper state by three, and emits exactly 1,024 symbols using the scaled table. Its compile-time even-count assertion prevents the existing unscaled odd-tail fallback from being reached. Ordinary unscaled pair APIs retain arbitrary chunk support.
- Packed offsets remain unsigned 16-bit values, at most 39,296. UXTAH and shifted ADD address the unchanged tables correctly. C and assembly prefixes use offset-format pilot history. Native/ISA checks cover all five ratios, histories, partitions, all 4,913 row offsets, ABI preservation, and scaled pair emission.
- Staged mapping reuses a key slot only after its consumer advances `shaped`. Alternating producers own disjoint even/odd output slots. Both wait for DMA completion before output-slot reuse. Publication barriers and worker join prevent unfinished-buffer reuse. Deadline failures remain explicit underrun/idle records.
- Source histories, mapper tables, keys, output blocks, and capture storage occupy disjoint bounded arena regions. Wrap history is preserved.
- Generated scratch files exactly match the original assembly after the intended symbol and section substitutions. The short C prefix and mapper remain in cached Flash. Both ELFs retain two 2 KiB stacks. Scratch margins are 16 bytes in X and 544 bytes in Y, including linked veneers. Available low-RAM heap space is 1,764 bytes in iqbench and 1,220 bytes in pvflight, including the reserved 1 KiB minimum.

## Recorded evidence

Replayed the current host validator against both final 32APSK 60-second soaks, both final-image 16APSK regressions, and the two positive A/B/A records: all six pass. The Flash B control has the expected underrun at 477 microseconds with zero ownership errors. This is a controlled deadline failure, not a passing stream. All current firmware source hashes still match the build-4 manifest.

The measurements establish runtime label mapping, shaping, and digital DMA/PIO output. They do not establish integrated APSK FEC/SPI throughput or AFE/RF performance. No payload-throughput claim is implied by the component result.

## Main results README review

The measured rates, work fractions, table sizes, offset instruction counts, FEC planning arithmetic, memory margins, build provenance, and build-4 UF2 SHA agree with the archived records. Scope is stated correctly: digital mapper/shaper/output components, with complete FEC/SPI and AFE/RF qualification excluded. The final-image 16APSK regression is identified separately from its earlier 60-second soak.

All three minor precision edits are applied: shaping leaf, native block independence, and 720 reordered block cases. The A/B/A comparison JSON now exists, records the raw result SHA and assertions, and every results README Markdown target resolves. Root additionally reports three real CLI invalid-selection checks rejected before directory creation or USB, and the full runner self-test passes 126 metadata profiles and six capture profiles. No outstanding finding remains.
