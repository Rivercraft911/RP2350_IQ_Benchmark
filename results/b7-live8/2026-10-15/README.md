# Complete on-chip 8PSK transmitter at 8 Msym/s: live FEC into 8-bit hardware shaping

**Result.** One RP2350B at its rated 128 MHz runs the whole chain: on-chip PV-SPI GCS source ->
BBFRAME -> BCH -> LDPC -> rotated 8PSK PL mapping (core 1) -> packed 2-bit axis codes (core 0)
-> seven-instruction PIO address producers -> DMA row fetch from B4's 64 KiB 8-bit table -> PIO
writers + 32 MHz GPIO28 DDR clock. DVB-S2 8PSK 5/6 with pilots at **8 Msym/s, 19.38 Mb/s BB
capacity, 9.6 MHz nominal**. With 8-bit rows: **6 of 6 60-s runs clean**, at bus priority 13
(4 runs) and 0 (2 runs). The old 14-bit rows on the same image ran 2 of 2 clean at priority 13 but
**stalled after 4.4 ms in 2 of 2 runs at priority 0**.

Pico Plus 2, RP2350B A2, USB only, image `8aac68bef5` (`rd/b7-live8`, experiment build,
`CORE0_STACK_SRAM_HI=ON`; `flash.txt`). Command `bench4fec 2 ms invert prio bits`, runner
`host/iq4fec_trial.py --bits 8|14`. Every positive run checks 4096 stride-17 GPIO0..31 snapshots
against an independent reference (GCS BBFRAMEs -> scalar BCH/LDPC -> 8PSK PLFRAMEs -> geometric
four-level codes -> frozen L7 FIR with the table's own rounding), GPIO28 against IQ_FLAG, packet
accounting and the credit order issue <= grant <= publication <= conversion <= encoder output.

| Run set | Rows | Priority | Duration | Result | Core 0 (convert + supervise) | Core 1 encoder |
|---|---|---:|---:|---|---:|---:|
| `quick8/` | 8-bit | 13 | 3 s | clean; inverted clock detected (4096/4096) | 24.5% | 88.2% |
| `live8-60s-prio13-1..4/` | 8-bit | 13 | 60 s each | **4/4 clean** | 24.4% | 87.9% |
| `live8-60s-prio0-1..2/` | 8-bit | 0 | 60 s each | **2/2 clean** | 25.4% | 87.0% |
| `live14-60s-prio13-1..2/` | 14-bit | 13 | 60 s each | 2/2 clean | 24.5% | 89.9% |
| `live14-3s-prio0-1..2/` | 14-bit | 0 | 3 s requested | **writer TX stall at 4.4 ms, 2/2** | — | — |

Minimum credit runway stayed 1125–1130 words (8-bit) and 1065–1068 (14-bit).

## Reading

- The `dmaloop` measurements (`results/dmaloop/2026-10-15`) give this graph's row loop as 12
  SYS cycles with 8-bit rows and 14–15 with 14-bit rows, against a 16-cycle deadline at
  8 Msym/s. The priority-0 pair is the discriminating test: 14-bit fails in milliseconds as B2
  always did at mask 0, 8-bit holds 60 s. The 14-bit priority-13 pair did not stall on this image
  (the merged round-2 image stalled at 40–51 s in 4 of 5 runs), so its rare-stall rate depends on
  the image; two clean minutes are not evidence it is fixed.
- Priority 0 removes the bus-priority setting that previously cost core 1 several points; here
  the encoder measured 87.0% at mask 0 against 87.9% at mask 13.
- Eight runs of 60 s are a few minutes of operation, not a soak.

## Not established

External ingress (the source is the on-chip PV-SPI master at 32 MHz), 16 Msym/s with on-chip FEC
(see `results/c3-8psk-fec16`), AFE pad timing, spectrum (the 8-bit table's modeled upper-mask
margin is +0.026 dB), EVM, receiver decode, temperature and power.
