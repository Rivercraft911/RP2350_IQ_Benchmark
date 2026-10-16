# C6 on the Pico: 8PSK packed-code mapper 32% cheaper

Pico Plus 2, RP2350B A2, SYS128, image `c0d4c4dca3` (`rd/c6-map8`, experiment build).
`c3pair code 40 0 who options`, all in `pair.jsonl`; mask 27 = C4 without scratch-Y BCH,
59 = 27 + C6 (bit 32 on this branch). Output-ring CRCs are identical with and without C6.

| Code | Mode | Map, mask 27 -> 59 | Frame, mask 27 -> 59 |
|---|---|---:|---:|
| 2/3 | one core | 99.6k -> **67.7k** | 359.6k -> 326.7k |
| 2/3 | both cores together | 105.9k -> 72.7k | 382.9k -> 348.5k |
| 5/6 | one core | 99.6k -> 67.8k | 376.0k -> 343.1k |
| 3/4 | one core | 99.7k -> 67.7k | 414.8k -> 381.8k |

The repeated A/B rows reproduce within 0.1k. `stackcheck`: code intact, core-0 deepest stack
1376 B of 4096. Design and native evidence: [docs/rd/c6-map8.md](../../../docs/rd/c6-map8.md).
