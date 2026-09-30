# PV-SPI v1: CM5 → RP2350 downlink interface

The CM5 sends its MPEG-TS over one SPI lane, as the same 1316-byte datagrams it would send the
E200. The RP2350 builds and transmits DVB-S2 with the E200 profile, so the ground side
(E200 + gr-dvbs2rx) does not change.

| | |
|---|---|
| Waveform | DVB-S2 single TS, CCM, QPSK 2/3, normal FECFRAME, pilots on, roll-off 0.20, 8 Msym/s |
| TS capacity | 10.33 Mb/s at 8 Msym/s (8e6 × 42 960 / 33 282), about 981 full messages/s |
| Ground | `dvbs2-rx --modcod qpsk2/3 --frame-size normal --pilots on --rolloff 0.2` |
| Firmware | `RP2350_IQ_Benchmark/firmware`, command `pvtx` (see Bring-up) |
| Status | RP2350 side implemented; on-chip self-test clean for 60 s at 8 Msym/s (58 866 messages, 0 errors, output bit-exact) |

The CM5 has no real-time duty: the RP2350 owns the symbol clock. When the CM5 sends less than
the channel rate, the RP2350 inserts TS null packets (PID 0x1FFF), so the RF stream never gaps.

## Pinout

Both sides are 3.3 V CMOS. Pico Plus 2 pin numbers follow the standard Pico header; CM5 pins
are the CM5 IO Board 40-pin header (RP1 SPI0).

| Signal | Dir | Pico Plus 2 | CM5 |
|---|---|---|---|
| SCK | CM5 → Pico | GP17 (pin 22) | GPIO11 (pin 23) |
| MOSI | CM5 → Pico | GP18 (pin 24) | GPIO10 (pin 19) |
| CS_N | CM5 → Pico | GP19 (pin 25) | GPIO8 / CE0 (pin 24) |
| MISO | Pico → CM5 | GP20 (pin 26), not driven in v1 | GPIO9 (pin 21) |
| READY | Pico → CM5 | GP22 (pin 29) | any GPIO, e.g. GPIO25 (pin 22) |
| RUN (reset, optional) | CM5 → Pico | RUN (pin 30), open-drain low = reset | e.g. GPIO24 (pin 18) |
| GND | | pins 23, 28 | pins 20, 25 |

- Put a 4.7 kΩ pull-up on CS_N and a 10 kΩ pull-down on READY. The bench RP2350 is rev A2,
  where erratum E9 makes a floating input unreliable, and a pull-down keeps READY low while the
  Pico is off or resetting.
- Power the Pico before the CM5 drives the SPI pins, so the RP2350 is not back-fed through them.
- Keep jumpers short (≤ 15 cm at 20 MHz), with a ground wire next to SCK.

## SPI settings

- Mode 0 (CPOL 0, CPHA 0), MSB first, 8-bit words.
- SCK: 1 MHz for bring-up, **20 MHz** in service (200 MHz / 10 on RP1). The minimum for full rate
  is 16 MHz (see Rates).
- **One message = one CS_N assertion = exactly 1332 bytes**: a single `SPI_IOC_MESSAGE` of
  length 1332, never split across transfers.
- CS_N must be high for ≥ 10 µs between messages, and READY is valid 10 µs after CS_N rises.
  The RP2350 closes each message in an interrupt on the CS_N rising edge and realigns on it, so a
  bad or short transfer costs only that message.

## Message format

Fixed 1332 bytes. Multi-byte fields are little-endian.

| Offset | Size | Field | Value |
|---|---|---|---|
| 0 | 2 | magic | 0x5650 (bytes `50 56`, "PV") |
| 2 | 1 | version | 1 |
| 3 | 1 | type | 1 = TS_DATA, 0 = NOP (ignored, allowed as a keep-alive) |
| 4 | 2 | seq | +1 per message, wraps 65535 → 0 |
| 6 | 2 | length | payload bytes: 188 × n with n = 1…7 (TS_DATA), 0 (NOP) |
| 8 | 4 | reserved | 0 |
| 12 | 1316 | payload | n TS packets, each starting 0x47, then zero padding |
| 1328 | 4 | crc32 | CRC-32 over bytes 0–1327 |

- CRC-32 is IEEE 802.3, reflected, init 0xFFFFFFFF, final XOR 0xFFFFFFFF, i.e. Python
  `zlib.crc32`. Its check value is `crc32("123456789") = 0xCBF43926`.
- The RP2350 checks the CRC in hardware, with the DMA sniffer during reception. It drops, and
  counts, any message with bad magic, version, type, length or CRC, a packet without 0x47, or a
  short or long transfer.
- A jump in `seq` is counted as lost messages. The data is still accepted.
- TS continuity and PCR are the CM5 mux's job; the RP2350 passes packets through unchanged.

## Flow control and rates

**READY** (Pico → CM5, active high) means the RP2350 has room for at least one more message.
Check it immediately before starting each message. If it is low, wait, polling or on an edge
event; normal waits are about 1 ms. A message started while READY is low may be dropped and
counted as an overflow. READY stays low at boot until the transmitter runs.

| Quantity | Value |
|---|---|
| Messages/s at full TS rate | 10.33 Mb/s / (1316 × 8) = 981 |
| Line rate needed | 981 × 1332 × 8 = 10.45 Mb/s |
| Bus occupancy at 20 MHz / 16 MHz / 12.5 MHz | 52 % / 65 % / 84 % |
| RP2350 queue | 24 messages ≈ 25 ms at full rate |
| Added latency | ≤ queue + about 5 ms (one BBFRAME plus shaping) |

On the CM5 side, absorb Linux scheduling gaps upstream of the SPI writes, e.g. `SO_RCVBUF`
≥ 1 MB on the UDP socket and a SCHED_FIFO or high-priority sender thread. At lower symbol rates
(1, 2, 4 Msym/s for early ground tests) the capacity scales down proportionally; READY throttles
the sender automatically.

## Host driver outline

1. Open `/dev/spidev0.0`: mode 0, 8 bits, `max_speed_hz` 20 000 000. Request READY as an input
   (libgpiod).
2. For each 1316-byte TS datagram (the current E200 feed, UDP 230.10.0.1:1234) or n × 188 bytes:
   fill the header, copy the payload, zero-pad, append the CRC.
3. Wait for READY high, then send one 1332-byte transfer. Increment `seq`.
4. Count READY waits and their longest duration; log if a wait exceeds 20 ms.

`host/cm5/pv_spi_tx.py` is a reference sender (Python, spidev + gpiod) with `--udp`, `--file`
and `--pattern` sources. Use it for bring-up and as a check against a C driver.

## Test vector

One TS packet, PID 0x100, continuity counter 0, payload bytes `i & 0xFF` for i = 0…183;
seq = 1. The full message is 1332 bytes:

```
bytes 0-15:  50 56 01 01 01 00 bc 00 00 00 00 00 47 01 00 10
bytes 16-199: 00 01 02 ... b7          (payload i & 0xFF, i = 0..183)
bytes 200-1327: 00                     (padding)
bytes 1328-1331: 8d 32 be 51          (crc32 = 0x51be328d)
```

Regenerate it with `python3 host/cm5/pv_spi_tx.py --vector`.

## Bring-up

Start the RP2350 first, from a PC or the CM5 on its USB port:
`python3 host/iqbench.py pvtx --ms 600000` (10 min; add `--cpw 4/8/16` for 4/2/1 Msym/s). Then
start the sender. At the end the RP2350 reports:
- messages OK, header/CRC/sync errors, lost (sequence gaps), short/long transfers and overflows;
- TS and null packet counts;
- `crc_chain`, the CRC-32 over the CRC fields of the accepted messages. It must equal the
  sender's `crc_chain`.

| Step | Setup | Pass |
|---|---|---|
| 0 | Pico alone: `iqbench.py pvtx --selftest --cap 4096` (on-chip emulated master, 21 MHz) | PASS: 0 errors, BBFRAMEs and output capture match the reference |
| 1 | Wired, 1 MHz, `pv_spi_tx.py --pattern --count 10000` | 10 000 OK, 0 errors, `crc_chain` equal on both sides |
| 2 | 5 → 10 → 16 → 20 MHz, pattern at full rate, 60 s each | 0 errors, ≥ 981 msg/s accepted, READY throttling the sender |
| 3 | 20 MHz, real TS from the video mux, 10 min | 0 errors or gaps; null packets = unused capacity |
| 4 | later: AFE7071 + LO, ground E200 decode | per the IREC modem plan |

## Not in v1

- Status on MISO (planned v1.1: a status block returned during NOP messages clocked ≤ 4 MHz).
- Runtime configuration (MODCOD, symbol rate, frequency, RF enable) over SPI. v1 is configured
  over USB.
- Final carrier pinout and connector; the pins above are the Pico Plus 2 bench wiring.
