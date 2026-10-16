# Why the one-symbol-row engines stall: the DMA row loop takes 10–18 SYS cycles

**Result.** Each output chain is a serial loop: control DMA reads a row pointer, writes it to
the sample channel's READ_ADDR_TRIG, the sample channel copies the row to a PIO FIFO and
chains back. Measured on the Pico with sinks that never back-pressure, one loop takes
**10.0 SYS cycles** (1-word row, SRAM pointer), 2 more per extra row word, 2 more when the
pointer comes from a PIO RX FIFO, and 1–4 more when four chains contend. Every stall
observed in rounds 1–3 matches loop latency against the per-chain row deadline.

Pico Plus 2, RP2350B A2, SYS128, USB only. Images `dc6ef389bf` (`sweep.jsonl`) and
`30011d605b` (`pio-pointers.jsonl`), branch `rd/stall-dmaloop`, command `dmaloop`
(`firmware/src/dmaloop.c`), runner `host/dmaloop_sweep.py`. Each record runs the chains
200 us to settle, then counts iterations over 4 M cycles with interrupts off: control-channel
READ_ADDR deltas sampled every 1000 cycles (SRAM pointers) or a PIO sink's word counter
(PIO pointers). Three repetitions each; spread is shown where it exceeds 0.05 cycles.

## Loop period, SYS cycles per row per chain (bus priority 13, core 1 idle)

| Chains running | 8-bit row (1 word), SRAM pointer | 8-bit, PIO pointer | 14-bit row (2 words), SRAM pointer | 14-bit, PIO pointer |
|---:|---:|---:|---:|---:|
| 1 | 10.0 | 12.0 | 12.0 | 14.0 |
| 2 | 10.0 | 12.0 | 13.0 | 15.0 |
| 4 | 11.0 | 14.0 | 16.0 | 18.0 |

- Writing rows to an SRAM word instead of a PIO FIFO saves 2 cycles (1 chain, 1 word: 8.0).
- Priority mask 0 gives the same numbers except four 14-bit chains: 16.0–17.0 (SRAM) and
  18.0–19.0 (PIO), varying between runs. A core-1 `memcpy` hammer at mask 0 adds up to
  0.8 cycles (2.0 for four PIO-pointer 14-bit chains); at mask 13 it adds nothing.
- A paced low-priority background DMA channel (SRAM -> PIO FIFO or SRAM -> SRAM, up to
  SYS/2 = 64 M transfers/s) did **not** change four 8-bit SRAM chains (11.01 at every rate);
  for four 14-bit chains the 16/17 split persisted at every load rate.

Interpretation (inference, not a datasheet figure): within one chain nothing overlaps, so the
loop is a chain of dependent bus operations costing about two cycles each. More chains overlap
with each other until they contend; four chains of 2-word rows contend most.

## Deadline per chain versus observed outcome

A symbol lasts 8 SYS cycles at 16 Msym/s (cpw1) and 16 at 8 Msym/s. With one chain per axis a
chain must deliver one row per symbol; with two chains per axis one row per two symbols.

| Engine | Rate | Chains | Row / pointer | Loop | Deadline | Margin | Observed |
|---|---:|---:|---|---:|---:|---:|---|
| B4 `bench4live8` | 16 Msym/s | 2 | 8-bit / PIO | 12.0 | 8 | **−4** | TX stall after ~20 us |
| older two-writer one-symbol rows | 16 Msym/s | 2 | 14-bit / SRAM | 13.0 | 8 | **−5** | stalled in every record |
| B4 `bench4live8` | 8 Msym/s | 2 | 8-bit / PIO | 12.0 | 16 | +4 | clean 3 s |
| B2 `bench4fec` (live FEC, PV-SPI) | 8 Msym/s | 2 | 14-bit / PIO | 15.0 | 16 | **+1** | stall every 40–50 s on the merged image |
| B5 `bench4quad` | 16 Msym/s | 4 | 14-bit / SRAM | 16.0 (16–17 at mask 0) | 16 | **0** | 5/6 60 s clean; stall during capture |
| B5 `bench4quad8` | 16 Msym/s | 4 | 8-bit / SRAM | 11.0 | 16 | **+5** | 12/12 60 s clean |
| B6 plan (four PIO producers) | 16 Msym/s | 4 | 8-bit / PIO | 14.0 | 16 | +2 | prediction: marginal |

The writers' FIFOs (about 68 cycles of runway for 14-bit rows, 128–144 for 8-bit) absorb short
bursts. A design with zero or one cycle of margin survives until a burst of extra traffic
(capture DMA, PV-SPI, core-0 conversion, core-1 FEC at mask 0) outlasts that runway; a
negative margin drains it in microseconds.

## Limits

The loop numbers are best cases: sinks never back-pressure, PIO pointers are constant (one
SRAM bank), the hammer is a `memcpy`, not the FEC's access pattern. Window-to-window spread is
not reported because sequential register reads skew the sample instants. Real engines only add
traffic. The B6 line is a prediction from these numbers, not a measurement.
