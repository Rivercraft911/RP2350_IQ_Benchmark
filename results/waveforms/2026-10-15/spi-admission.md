# PV-SPI late-CS admission audit and hardware gate

The b29d1a7 normal5/6 TS/8Msym run recorded short=2, late_close=1, drain_timeout=1, lost=2; its last short was330 of333words, FIFO0, CS1, PC29, CTRL0x07186049. The captured waveform and accepted-message CRC chain failed; preserving that result was correct. Firmware's ordinary dualstream `errors` was zero because these transport counters were not forwarded to its output-stop path.

## Specific causal hypothesis

The original READY is a CPU/SIO level. It remains high throughout message A and its subsequent intermessage gap until A's CS interrupt runs. A delayed handler can therefore leave READY high when a conforming master checks it and begins B. The old emulator's actual gap is1027sys clocks (~8.023us at128MHz), shorter than the external protocol's10us validity interval, but neither constant is an unconditional IRQ latency bound.

If A's handler runs after B starts, it records `late_close`, drops A, aborts/reset/rearms RX in the middle of B, and drops B as a short at its eventual CS end. Dropping96 leading bits gives the observed330words, empty FIFO and RX waiting for another SCK rising edge. Waiting3us at B's end cannot recreate those bits. This explains the exact two-message counter pattern; it is an inference, not proof that this was the unique cause of the observed run. The receiver-only masked-IRQ negative control is the decisive experiment.

PIO allocation is top-down: RX is the first4-word program on this PIO (28..31); PC29 is its WAIT SCK1 at off_rx+1. CTRL0x07186049 means RX DMA channel3, DREQ12 (PIO1RX0), enabled/BUSY,32-bit, incrementing destination, BSWAP and sniffer enabled; no AHB/read/write error. Missing3words with FIFO0 is consistent with absent input bits, rather than a last queued SRAM write. A historic RX FIFO stall could also lose edges and was not recorded by these diagnostics.

BCH slices are long foreground calls but do not themselves disable IRQs. No CPSID appears in the relevant BCH/descriptor leaves. The SDK starts IRQs at default priority0x80; its USB background worker calls `tud_task` from another IRQ and is not explicitly assigned a lower priority in the inspected SDK2.2.0 source. Same-priority USB work or another interrupt-masked section is a plausible latency source. The original blocking source is unproven.

## Implemented admission invariant

The existing CS SM now owns READY by mandatory side-set:

```
pull block       side0
wait 1 gpio CS   side0
wait 0 gpio CS   side1
wait 1 gpio CS   side0
irq wait0        side0
```

Its actual opcode words are80a0,2093,3013,2093,c020. A CPU TX token grants exactly one CS assertion. READY falls one PIO execution cycle after CS fall is observed through its synchronizer, independently of CPU interrupt latency. After CS rises, IRQ WAIT holds it low until the CPU closes/retire-checks DMA, publishes the old immutable slot, resets RX, and arms the next free slot. Only a fresh unarmed->armed transition posts a token; repeated consumers cannot duplicate it. The ISR clears IRQ0 last, followed by DSB before exception return. The existing3us DMA retirement wait remains unchanged.

Pause clears admitting under the shared lock and forces GPIO OUTOVER LOW. An already-started message may finish, but no fresh arm/token follows pause. Stop disables the gate, clears its token FIFO, prepares SIO's low latch/output direction, selects SIO, then restores normal override. Restart initializes side0/PULL and fresh DMA state before removing the override. No resume API or retained grants exist across stop/start.

Resources: unchanged3SM/3DMA for SPI;19PIO words instead of17. The sparse capture's3words still leave10free instruction words in that PIO. Additional persistent storage is one4-byte sticky error word plus4bytes for the longer program array (link alignment may alter the final map). Sticky errors distinguish short/long/late/overflow/AHB; downstream packet CRC/header/sequence faults remain in pv_stats and must also stop a strict trial.

The gate assumes the documented master rule: inspect READY only before beginning a message and do not start while low. It prevents a CPU stall from issuing a second stale grant. It does not supply an independent asynchronous overflow monitor while PULL is blocked on a missing token. A master ignoring READY is outside that admission contract; existing CRC/length diagnostics still apply when its data reaches a closed slot.

## Evidence and remaining device test

`host/test_pvspi_rx.py` compiles the actual close/IRQ/arm functions and actual program-builder body. Native tests cover last-beat retirement, all333 true truncation lengths, long FIFO1..8, AHB, late CS, CRC rejection, pause and no duplicate grants. The independent decoder verifies the actual five opcodes and runs CPU service delays0/1/20/128/1000us: the new gate has zero unclosed-message overlaps/late closes; the old3-word control overlaps at20us. This is a logical admission model, not a bus-arbitration or full bit-accurate SPI throughput model.

Arm GCC14.2 -Werror isolated compile passed, including the final DSB addition. No shared firmware build, USB, commits or changes outside pvspi.c/.h and its assigned test were made for the fix. Exact pre-gate files are /tmp/yapogee-pv-before/pvspi.c and .h from git b29d1a7.

Next decisive Pico test: same receiver-only pvstress firmware and32MHz source, periodically mask CPU IRQs for20us. Old gate must reproduce late/short/lost or the causal hypothesis remains unconfirmed. New gate must deliver only complete accepted messages with an exact accepted CRC chain, no framing/packet errors and no post-pause admissions beyond the outstanding message. Then repeat QPSK5/6 TS/GCS8/16Msym and8live under realistic encoder/output/capture traffic. A delay-only pass does not establish real CM5 or external32MHz electrical qualification.

Primary references: Raspberry Pi RP2350 datasheet §11.2.5 (side-set occurs even on a stalled instruction), §11.4.11 (IRQ WAIT set-and-wait-clear), §12.6.8.3 (DMA completion/retirement), and register CTRL_TRIG:
https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf
SDK2.2.0 default IRQ priority and USB worker source:
https://github.com/raspberrypi/pico-sdk/blob/2.2.0/src/rp2_common/hardware_irq/include/hardware/irq.h
https://github.com/raspberrypi/pico-sdk/blob/2.2.0/src/rp2_common/pico_stdio_usb/stdio_usb.c
