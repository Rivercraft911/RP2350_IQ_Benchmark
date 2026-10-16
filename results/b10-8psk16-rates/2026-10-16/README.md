# B10 on the Pico: complete on-chip 8PSK 5/6 at 16 Msym/s (38.76 Mb/s)

Pico Plus 2, RP2350B A2, SYS128, USB only, experiment image `977d68856a` (`rd/b10-8psk16-rates`
on C9 + main `b33d5f7`; `flash-977d68856a.txt`). `b9 cpw ms invert prio slow options code
sram_hot`: B9's complete transmitter (memory GCS -> two-core FEC -> four PIO producers -> DAC bus)
now admits normal 5/6 (code 8) and 3/4 (code 6) and C9 masks; hot GCS and C5 scheduler code is
copied to SRAM (`sram_hot 1`, default) and the supervisor checks the service timer before each
slice. All runs at priority 13, cpw 1 (16 Msym/s). Every positive run: full duration, exact
independent ring CRC and 4096 captured words, normal clock phase, resources restored.

**Result:** 5/6 ran 60 s clean with masks 4603 and 6395 (43,256 frames, 38.76 Mb/s payload),
with little margin: core 0 94–95% busy, worst frame 330–332k of 355k cycles. 2/3 ran 60 s
clean with the service gap down to 90 µs. 3/4 (mask 12795) passed 3 s but ran dry after 46 s.

## The C9 regression was flash cache placement

Same image, 2/3, mask 251, hot code in flash vs SRAM, alternating, 3 s each:

| `sram_hot` | Runs clean | GCS per frame, core 0 / 1 | LDPC per frame | Core 0 / 1 busy |
|---:|---|---:|---:|---:|
| 0 (flash) | 1 of 3 (two SOURCE_EMPTY) | 25.7k / 26.0k | 91.4k / 102.1k | 94.0 / 88.7% |
| 1 (SRAM) | 3 of 3 | **19.6k / 20.2k** | 89.6k / 100.5k | 91.0 / 86.5% |

Main's B9 image measured GCS 23.1k / 22.9k and LDPC 88.5k / 98.4k. The SRAM copy recovers GCS
and more; LDPC is within 1–2k of main. B10's map comparison found the C9 merge had moved the
GCS body 0x958 bytes in flash, into a two-way XIP cache set shared with B9's core-1 loop, the
timer IRQ and USB code (docs/rd/b10-8psk16-rates.md); maps show the conflict opportunity, the
A/B above shows its cost.

## Rates (3 s, then 60 s)

| Code | Mask | 3 s | 60 s | Core 0 / 1 busy | Supervision | Worst frame, core 0 / 1 | Max gap (gaps > 96 µs) |
|---|---:|---|---|---:|---:|---:|---:|
| 2/3 | 251 | clean | **clean, 43,257 frames** | 91.0 / 86.5% | 12.6% | 298k / 312k | 90 µs (0) |
| 2/3 | 6395 | clean | | 87.5 / 83.1% | 10.5% | 288k / 302k | 110 µs (462) |
| 5/6 | 251 | SOURCE_EMPTY | | 95.2 / 92.2% | | 327k / 355k | |
| 5/6 | 4603 | clean | **clean, 43,256 frames** | 93.6 / 92.2% | 10.5% | 319k / 332k | 110 µs (1563) |
| 5/6 | 6395 | clean | **clean, 43,256 frames** | 95.2 / 91.5% | 10.3% | 320k / 330k | 113 µs (1169) |
| 3/4 | 251 | SOURCE_EMPTY | | 94.0 / 90.0% | | 342k / 365k | |
| 3/4 | 4603 | SOURCE_EMPTY | | 97.3 / 96.3% | | 333k / 345k | |
| 3/4 | 12795 | clean | SOURCE_EMPTY at 33,358 frames (46 s) | 94.6 / 93.2% | 10.1% | 323k / 335k | 105 µs (210) |

Payload per frame (EN 302 307-1 Table 5a Kbch minus the 80-bit BBHEADER): 2/3 42,960 bits
(30.97 Mb/s), 5/6 53,760 (38.76 Mb/s), 3/4 48,328 (34.84 Mb/s). The C9 masks' larger BCH
slices bring service gaps back above 96 µs (still under the 128-µs gate).

Controls at 5/6, mask 6395, 3 s: a 5000-µs slow worker gives `source_underrun_detected`; an
inverted DDR clock gives `clock_inversion_detected`. `stackcheck` afterwards: code intact,
core 0 deepest 2224 of 4096 B.

One 60 s run per positive row; 5/6 runs at 92–95% of both cores, so treat it as a
demonstrated rate, not a qualified one with margin.

## Merged with main (N5, wide ingress): regressed again (`bd796ac66e`, not merged into main)

On the image that adds main's N5 and wide-ingress code, N5 and `pvwide` pass, but the FEC
transmitter is layout-sensitive again: 2/3 mask 251 ran dry after 8,740 frames and 5/6 mask 6395
after 3,376 frames in 60 s runs (3 s runs passed), with supervision 14.3% and core-1 GCS 22.7–27.7k
per frame (`merged-*`). Moving only GCS and the C5 scheduler to SRAM does not make the 16 Msym/s
FEC independent of flash layout; the remaining flash-resident hot code (job stepping, mapper
dispatch, supervision) still shares the XIP cache with USB and timer code. The 5/6 result stands
for image `977d68856a` (tag `archive/b10-8psk16-rates`).
