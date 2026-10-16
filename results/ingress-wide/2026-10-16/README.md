# Wide ingress on the Pico: four-bit PV messages at up to 76.9 Mb/s from an on-chip sender

Pico Plus 2, RP2350B A2, SYS128, USB only, experiment image `5eaed9b932` (`rd/ingress-wide` on
main `a04e6ba`; `flash-5eaed9b932.txt`). `pvwide ms width half fault internal`: the existing
1332-byte PV message (1316 payload bytes, CRC, sequence, one-message admission credit) on four
data pins GP17..20, sender clock GP21, CS_N GP26, READY GP22, received by a PIO state machine
and DMA. An on-chip PIO sender drives the same pins from the same SYS clock. Every positive run
classifies `digital_ingress_clean`: all messages accepted in sequence, `validated_bytes =
ok x 1316`, independent CRC-field and payload chains matching, no short/long/overflow/lost.

| Run | Width | Wire clock | Payload rate | Result |
|---|---:|---:|---:|---|
| Control, 3 s | 1 | 12.8 MHz | 12.46 Mb/s | clean |
| A/B, 3 s | 4 | 12.8 MHz | 47.80 Mb/s | clean |
| Sweep, 3 s | 4 | 4.0 / 6.4 / 8.0 / 10.7 / 12.8 / 16.0 / 21.3 MHz | 15.5 / 24.6 / 30.5 / 40.2 / 47.8 / 58.9 / 76.9 Mb/s | all clean |
| **60 s** | 4 | 8.0 MHz | **30.51 Mb/s** (target 27.94) | clean |
| **60 s** | 4 | 10.7 MHz | **40.21 Mb/s** (target 31) | clean |
| **60 s** | 4 | 12.8 MHz | **47.81 Mb/s** (target 47) | clean |

Negative controls at width 1 and 4, 12.8 MHz: a corrupted CRC on every 16th message was
rejected exactly (223 and 852 rejections, `crc_control_detected`), and short messages were all
rejected with zero accepted (`short_control_detected`).

CPU: closing a message costs 241 cycles (0.5–0.9% of core 0 at these rates). The benchmark's
consumer, which validates every payload byte against the reference pattern, costs about 1.03
cycles per payload bit (24–37% of a core); that is validation work, not transmitter ingress
cost. `stackcheck` afterwards: code intact, stack paint not exhausted.

## What it does not show

The sender shares the RP2350's clock and pads, so this proves the receiver logic, framing and
throughput, not asynchronous electrical timing from an external sender: no CM5 sender exists
yet, and its GPIO/DPI capability is an open question in `docs/rd/ingress-wide.md`. GP21/GP26
are a bench pin assumption that needs a carrier routing change. Not yet joined to an encoder
(B9 uses an in-memory source).
