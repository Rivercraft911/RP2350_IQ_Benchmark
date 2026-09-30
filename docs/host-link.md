# Host link: CM5 → RP2350

**Chosen: RP1 SPI0 master → RP2350 PIO receiver (PV-SPI v1, [spec](pv-spi-spec.md)).** The
RP2350 does the FEC, so the host sends information bits only: ≤ 10.33 Mb/s of TS for
PigeonVision instead of 16 Mb/s of coded symbols. Tags are in [sources/SOURCES.md](sources/SOURCES.md).

Measured with a real CM5, two cameras and a 9 Mb/s TS at 20 MHz SCK: 179 818 messages in 210 s,
CRC chains equal, 0 protocol errors, 0 underruns. A pattern at full channel rate (982 msg/s) had
0 errors, and READY throttled the sender, with waits ≤ 2 ms ([results](../results/cm5-spi/README.md)).

## Options considered (2026-09-29)

| interface | rate | verdict |
|---|---|---|
| RP1 SPI0 → RP2350 PIO | 20 MHz = 20 Mb/s raw; 52 % bus occupancy at full TS rate | **chosen**, measured clean |
| RP1 PIO 4-bit + CLK → RP2350 PIO | 40 Mb/s at 10 MHz; RP1 PIO TX DMA ≈ 10.7 MB/s, ≈ 27 MB/s after linux PR #6994 [RPI-UTILS-116] | fallback if a coded-symbol link is ever needed |
| RP2350 PL022 SPI slave | ≤ clk_peri/12: 12.5 Mb/s at 150 MHz, 10.7 at 128 MHz [RP2350 §12.3.4.4 p1050] | too slow; marginal even for TS (10.45 Mb/s on the wire) |
| USB (RP2350 is Full Speed) | ≤ 9.73 Mb/s bulk payload [USB2 Table 5-9] | too slow |
| DPI, SMI, W5500 | no back-pressure / absent on Pi 5 / extra chip | rejected |

## RP1 SPI facts [RP1], [LINUX]

- SPI clock is 200 MHz / even divider: 50, 33.3, 25, 20, 16.7 … MHz. Requests round down.
- Transfers longer than the 64-byte FIFO use DMA (`spi-dw-dma.c`). The 1332-byte messages do:
  the SPI IRQ count stayed 0 during the camera runs.
- spidev's default `bufsiz` is 4096 bytes, so one 1332-byte message per ioctl fits.
- No dual/quad SPI from Linux, and RP1 SPI target mode is not merged, so the CM5 is the master.
- SPI0 pins: GPIO11 SCLK, GPIO10 MOSI, GPIO9 MISO, GPIO8 CE0. Set GPIO_VREF to 3.3 V to match
  the RP2350.

## RP2350 receiver

The PIO receiver waits for SCK edges, so SCK need not be continuous. Its limit is edge detection:
about 3 clk_sys per SCK half-period (estimate), i.e. roughly 20–25 MHz at 128 MHz. Measured clean
at 21.3 MHz from the on-chip emulator and at 20 MHz from the CM5; above that is untested.

## Open

- CM5 CPU cost: the Python sender costs the camera pipeline about 1–1.5 fps per camera. A C sender
  inside the capture process is the fix to try.
- SCK frequency and edges are not yet checked on a scope.
