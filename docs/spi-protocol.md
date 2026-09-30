# Host → RP2350B over one SPI lane (proposal)

User question (2026-09-29): could the CM5 (PigeonVision) and CM4 (SATS) talk to the RP2350B over
a single SPI lane with a custom protocol, and let the RP2350B frame and transmit at the target
rate? **Yes, provided the RP2350B does the DVB-S2 FEC.** The lane then carries information bits,
not coded symbols.

## Rates

| system | information rate on SPI | coded rate (if host did FEC) | 1 lane at 20 MHz |
|---|---|---|---|
| PigeonVision, 8 Msym/s QPSK 2/3 normal + pilots | ≤ 10.33 Mb/s TS (9 Mb/s planned) | 16 Mb/s | 1.9× margin vs 1.25× |
| SATS, 1 Msym/s QPSK 1/2 | ≤ 0.97 Mb/s | 2 Mb/s | > 20× |

The 10.33 Mb/s figure is 8e6 × 42 960 / 33 282, from the DFL of normal 2/3 over PLFRAME symbols
with pilots. `../../IREC/Pigeon_Vision/DESIGN.md` gives the same figure.

Measured receiver capacity (on-chip loopback, 128 MHz, while streaming 8 Msym/s):
- 21.3 MHz and 32 MHz SCK received clean;
- 16 MHz, which is below the 16 Mb/s coded need, was flagged as underrunning.

The loopback is synchronous and skew-free. An external master is asynchronous, and the 2-flop
input synchroniser adds ±1 clk_sys, so plan on about 20 MHz and verify over the real cable.

## Link

- SPI mode 0, host is master. SCK, MOSI, MISO and CS_N go to GPIO17–20. READY is on GPIO22,
  driven by the RP2350.
- The RP2350 receives with a PIO SM. CS_N delimits a message and resets the bit counter. DMA
  places the data into a message queue in SRAM (PSRAM for larger buffers).
- Flow control: READY high means at least one maximum-size message fits. The host waits for READY
  before each message (GPIO edge event). A full-duplex MISO stream carries the RP2350's status
  word, so the host needs no separate poll.
- Put external pulls on CS_N and READY (≤ 8.2 kΩ on rev A2 per erratum E9), so a reset host or
  modem leaves a defined state.

## Message format

| field | bytes | notes |
|---|---|---|
| magic | 2 | 0xD5 0x2E |
| type | 1 | DATA, CONFIG, NOP |
| flags | 1 | e.g. end-of-file |
| seq | 2 | increments per DATA message; gaps are counted |
| len | 2 | payload bytes |
| payload | len | DATA: TS packets (PigeonVision) or file segments (SATS) |
| crc32 | 4 | over header + payload; failed messages are dropped and counted |

- DATA payload for PigeonVision: an integer number of 188-byte TS packets. The RP2350 builds
  BBFRAMEs per EN 302 307-1 Table 4: UPL = 1504, SYNC = 0x47, SYNCD, CRC-8 per packet.
- DATA payload for SATS: numbered file segments with their own CRC. BBFRAME encapsulation is
  generic packetized or GSE; that choice is open.
- CONFIG: MODCOD, frame size and pilots, symbol rate (cpw), RF enable. RF enable is interlocked
  on the RP2350 side and never set by reset.
- Status on MISO: queue fill, messages OK / CRC-failed / sequence gaps, dummy frames sent,
  underruns, TX state, temperature.

## Buffering

Linux can stall the sender for tens of milliseconds. At 10.3 Mb/s, 50 ms is 64 KiB, so size the
queue for 64–128 KiB. SRAM has room at 8 Msym/s; PSRAM would allow more. When the queue is empty
the RP2350 transmits DVB-S2 dummy PLFRAMEs (clause 5.5.1, 36 slots of unmodulated carrier), so
the symbol stream and the receiver lock never gap.

## Division of work

Host: capture and compression (PigeonVision H.264/TS; SATS images and files), segmentation, and
SPI writes. No real-time requirement.

RP2350: queue, BBFRAME, BCH, LDPC, PL framing and scrambling, pulse shaping, DMA/PIO to the AFE7071.
Also clock, LO and PA control, and fault handling. It owns every real-time deadline.

## To test

1. The PIO SPI slave with CS_N framing and CRC checking, against the existing loopback emulator
   (upgrade it to emit messages).
2. A real CM5/Pi 5 spidev sender at 20 MHz: sustained rate, transfer gaps, and READY behaviour
   under load. Then CM4.
3. The end-to-end chain: TS file → SPI → RP2350 DVB-S2 → PIO capture → gr-dvbs2rx decode of the
   captured symbols.
