# B8 on the Pico: complete two-core 8PSK transmitter runs out of input within 5 ms

Pico Plus 2, RP2350B A2, SYS128, image `428e89bd08` (`rd/b8-lean8`, experiment build;
`flash.txt`). `host/b8_lean8_trial.py`, `b8lean8 code ms cpw invert prio options starve`.

`default-args-1..5/`: a coordinator scripting error (zsh did not split the argument string) ran the
runner five times with its defaults instead of the intended A/B, 60 s, inverted, starvation and
16 Msym/s sets: 8PSK 5/6 (code 8), 8 Msym/s (cpw 2), options 27, priorities 13 and 0, about 3 s
requested. All ten runs failed the same way.

| Field (all ten runs) | Value |
|---|---|
| Elapsed before the stop | 5.09–5.13 ms; 4 frames published |
| Reason | live fault: writer TX stalls on both axes, input null trigger (SOURCE_EMPTY), credit runway reached zero, PV-SPI did not consume runtime traffic |
| Core 0, first frame | busy 102%; stages GCS 162k, BCH 112k, LDPC 161k, map 114k cycles |
| Core 1, first frame | busy 65–66%; BCH 119–123k, LDPC 169–172k, map 107k; GCS 0 |

Reading: core 0 builds the GCS BBFRAMEs for both workers on top of its own FEC frames, and the
split is unbalanced. With the output DMA and PV-SPI running, every FEC stage costs 15–30% more
than in the quiet `c3pair` measurements (results/ldpc-banks, results/c4-fec-speed). Next: rerun
after the combined C5/C6/C7 FEC (about 110k fewer cycles per frame) and with ingress shared or
moved off core 0.
