# C5 on the Pico: LDPC 36–41% cheaper; 8PSK 2/3 FEC now fits the 16 Msym/s budget

Pico Plus 2, RP2350B A2, SYS128, image `1f569ab27a` (`rd/c5-ldpc`, experiment build; the only
later source change removes a `.note.GNU-stack` directive that made ld warn, no code change).
`c3pair code 40 layout who options`, all in `pair.jsonl`; mask 27 = C4 without scratch-Y BCH,
59 = 27 + C5 (bit 32). Output-ring CRCs are identical with and without C5 for every code.

Cycles per frame, thousands, average of 40 frames:

| Code | Mode | Mask 27: LDPC / frame | Mask 59: LDPC / frame |
|---|---|---:|---:|
| 2/3 | one core (who 0) | 143.8 / 362.0 | **84.5 / 294.9** |
| 2/3 | both cores together (who 2) | 157.7 / 386.5 | **89.3 / 310.8** |
| 5/6 | one core | 136.7 / 377.9 | **87.4 / 322.7** |
| 5/6 | both cores together | | **93.1 / 342.7** |
| 3/4 | one core | | 99.6 / 359.1 (BCH is the flash C t=12 at mask 59) |

LDPC sub-steps, 2/3 one core, mask 27 -> 59 (thousands): clear 10.3 -> 2.2, rotate-XOR groups
85.0 -> 57.6, row accumulation 10.1 -> 4.5, apply 11.0 -> 0 (fused), transpose/pack 27.0 -> 19.9.
Repeated A/B rows at the end of the sweep reproduce within 0.1k. `stackcheck` after the sweep:
code intact, core-0 deepest stack 1360 B of 4096.

Against the 355,104-cycle budget per core per assigned frame at 16 Msym/s, 2/3 now fits even with
both cores in the same stage at once (310.8k, 12% margin before supervision); 5/6 fits narrowly
(342.7k). Design and native evidence: [docs/rd/c5-ldpc.md](../../../docs/rd/c5-ldpc.md).
