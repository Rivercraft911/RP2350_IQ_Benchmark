# GPIO28 DDR DAC clock

`dualclock 1` makes the fast QPSK engine (`dual_dma.c`) drive the Yapogee DAC clock:
a continuous PIO clock on GPIO28 for the AFE7071 single differential DDR mode, through
the SN65LVDM31 on the board. It is opt-in; `dualclock 0` keeps the qualified HSTX CLK_IO
test clock on GPIO16. This is a digital result: it fixes which bus word each clock edge
belongs to, to one SYS cycle. Pad skew, the SN65 and the AFE are not part of the test.

## What the AFE needs

In single differential DDR mode the AFE latches I on the rising and Q on the falling
DACCLK edge, bypasses its FIFO and runs DACCLK at the per-channel sample rate
[SLOS789C p.27]. Setup is at least 0 ns and hold at least 2 ns, specified relative to the
DACCLKP/N rising edge [SLOS789C p.5]. Using the same limits for the falling (Q) edge is
an assumption; the datasheet does not tabulate it separately.

For writer divider `d` (SYS cycles per bus word) and N samples per symbol:

```text
bus word period   T_w   = d / f_SYS
DACCLK            f_clk = f_SYS / (2 d) = N x R_s       (one I and one Q word per period)
```

| Profile at SYS128 | d | Bus | DACCLK | T_w |
|---|---:|---:|---:|---:|
| 8 Msym/s, N4 (IREC nominal) | 2 | 64 Mword/s | 32 MHz | 15.625 ns |
| 16 Msym/s, N4 | 1 | 128 Mword/s | 64 MHz | 7.8125 ns |

Fractional dividers are rejected: a 1.5 divider alternates 1- and 2-cycle ticks, so the
edges would stay paired with the data but the DAC sample period would not be uniform.

## How it works

The clock is a separate two-instruction state machine with one mandatory side-set bit:

```text
mov y, y  side 1      ; 0xb042, I slot
mov y, y  side 0      ; 0xa042, Q slot
```

It runs at the writers' integer divider. Every writer layout (two or four writers, one-,
two- or four-symbol rows) emits I in even and Q in odd bus slots from its first enabled
cycle, so a clock that starts with them on the high instruction is high for exactly the
I words. Each edge leaves the RP2350 on the same SYS edge as the word it latches. The
SN65 propagation delay then gives the AFE its setup time.

**Atomic start.** The capture SM lives in PIO1. One set-alias write to PIO1 `CTRL` enables
the writers and the clock in PIO0 (`PREV_PIO_MASK`) and PIO2 (`NEXT_PIO_MASK`) with
`NEXTPREV_SM_ENABLE | NEXTPREV_CLKDIV_RESTART`, and restarts only the capture SM's divider.
The clock SM goes in whichever of PIO0/PIO2 has no writers, else beside them:
PIO0 for four writers (PIO2), PIO2 for two writers (PIO0). Before the write, GPIO28 is
driven low by the PIO, then handed to it: the first edge is the rising edge with I0.
Cost: one SM and two instruction words, no DMA channel and no CPU work while running.

**Stop and faults.** A fault or stop disables the writers and the clock in one PIO1
`CTRL` write, leaving GPIO28 at its last level. Yapogee's notes require the SN65 to be
disabled before its clock stops, and the whole stream rebuilt before a restart. This
firmware has no clock-permission output (the Pico Plus 2 has no CLOCK_REQ pin), so that
step is board firmware still to write. Restarts here always rebuild the stream and start
from slot 0.

GPIO16 and HSTX are left untouched in DDR mode. On the Pico Plus 2, GPIO28 also connects
through 1 kΩ to GPIO42 [PPP2-SCH]; leave GPIO42 an input.

## How it is checked

1. **Model.** `host/test_dacclk.py` parses the opcode arrays and start state from
   `dual_dma.c` and steps the clock with all four writer layouts. The clock is high in
   exactly the I slots; the inverted start is high in exactly the Q slots; a one-slot
   skew would appear in the strided capture at both dividers.
2. **Device capture.** `dual_dma_capture_pins()` keeps the 17-cycle stride but stores one
   GPIO0..31 snapshot per word, with input synchronizers bypassed for GPIO0–15 and 28.
   `host/dual_trial.py --dac-clock ddr` checks the bus against the independent reference,
   then requires GPIO28 == IQ_FLAG (GPIO14) in every snapshot. 17 is odd, so the snapshots
   cover both phases of the 2- and 4-cycle clock periods.
3. **Negative control.** `dualclock 2` starts the clock on the low instruction. The data
   must still match while every snapshot fails the parity check.

## Measured result

The complete SPI/FEC QPSK 5/6 pipeline (four writers, L8 pulse) ran 60 s with the
clock at 8 Msym/s / 32 MHz (TS and GCS input) and 16 Msym/s / 64 MHz (GCS). Every one of
4096 snapshots per run had GPIO28 equal to IQ_FLAG, the bus matched the reference, and
CPU loads matched the HSTX-clock runs (86.2% / 88.2% at 16 Msym/s). The inverted control
failed all 4096 snapshots with correct data. Short PRBS runs covered all four writer
layouts at both dividers, with the clock on PIO2 (two writers) and PIO0 (four), except
two writers with one-symbol rows at divider 1, a DMA ceiling that has always stalled.
TS input at 16 Msym/s
saturates core 1 with either clock source; see the campaign
[README](../results/dac-clock/2026-10-15/README.md).

## What this does not establish

- **Pad skew.** Both pins change on the same SYS edge, but their pad delays differ. The
  RP2350 tables give clk→pad ≤ 4.1 ns and bank-0 skew ≤ 2.1 ns (QMI figures,
  docs/rp2350-notes.md), not PIO-path values. The Yapogee timing audit quotes a
  1.8–2.9 ns SN65LVDM31 propagation delay into its test load (not rechecked here). With
  equal paths the AFE then sees 1.8 ns setup and 7.8125 − 2.9 = 4.9 ns hold at 64 MHz,
  against 0 ns / 2 ns required. Setup is the tighter margin: data arriving more than
  1.8 ns after the clock would violate it. At 32 MHz the window is 15.625 ns wide.
  Measure GPIO28 against GPIO0–13 at the AFE pins (gate G5).
- SN65 delay, swing and common mode, loaded edges, duty cycle and jitter.
- AFE capture, decoded waveform, spectrum and EVM.
- Startup and recovery with a real driver enable, brownout and partial power.
- The other output engines: `iqout.c` (default `pvflight` profile) and the 2-SPS 8PSK/APSK
  paths still have no DDR clock.
