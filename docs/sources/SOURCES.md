# Sources

One list for the whole repo. Docs cite these tags. Retrieved 2026-09-29 unless dated.
`reference/dvbs2/README.md` keeps its own list for the DVB-S2 cross-check.

## Local copies

PDFs, text extracts and page snapshots in this folder are not tracked (`.gitignore`).
Re-download and compare hashes. Text extracts are `pdftotext -layout` (poppler); the DVB-S2 table
generator uses `pdftotext -raw`.

| tag | document | file | sha256 |
|---|---|---|---|
| RP2350 | RP2350 datasheet, build 2025-07-29 d126e9e. Cited by printed page (PDF page = printed + 1). https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf | `rp2350-datasheet.pdf`, `rp2350.txt` | 2877d0f270fb6d6a57943bee58aaad536aa027bea1e5b1c4ce2541a3230d4be8 |
| AFE7071 | TI AFE7071 datasheet SLOS789C, https://www.ti.com/lit/gpn/AFE7071 | `afe7071-rev-c.pdf`, `afe7071.txt` | c1602d4a98dd72976d13e2bf02fa8d3fdf33401e2dde7d913f6c751edc0b8413 |
| EN302307 | ETSI EN 302 307-1 V1.4.1 (2014-11), https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf | `en_30230701v010401p.pdf`, `.txt` | 19077f80420eb17519fcf6f83e2847d7a877940b134c5306814b8c6f60d8475d |
| PPP2-SCH | Pimoroni Pico Plus 2 schematic, 3 sheets, 29/08/2024, https://cdn.shopify.com/s/files/1/0174/1800/files/Pimoroni_Pico_Plus_2_Schematic.pdf?v=1724926880 | `Pimoroni_Pico_Plus_2_Schematic.pdf` | fb84bd32f63b18296d19af1e8d4d9126b80bb674f042bcf40f4ef3e2788c1418 |
| PPP2-PIN | Pico Plus 2 pinout diagram, https://cdn.shopify.com/s/files/1/0174/1800/files/ppico_plus_2_pinout_diagram.pdf?v=1723557334 (PNG tracked) | `ppico_plus_2_pinout_diagram.pdf`, `.png` | b430f43963c1dc975ccd98e703013e755b221cf6871fafe36c724f1dd4153728 |
| PPP2-MECH | Pico Plus 2 mechanical diagram, https://cdn.shopify.com/s/files/1/0174/1800/files/pimoroni-pico-plus-2-mechanical-diagram.pdf?v=1725452657 | `pimoroni-pico-plus-2-mechanical-diagram.pdf` | 8a265a01f188e749dc653f240d83d6bca8136ce119e7755cd00942bbe0819d91 |
| PPP2W-SCH | Pico Plus 2 W schematic, 26/09/2024, https://cdn.shopify.com/s/files/1/0174/1800/files/Pimoroni_Pico_Plus_2_W_Schematic.pdf?v=1727350279 | `Pimoroni_Pico_Plus_2_W_Schematic.pdf` | cdb3d8c7c03ff2bbbf640867d6d6ac98fbb8eb0c8f307732e55bb63a8475ca5e |
| PPP2-PAGE | PIM724 product page (E9 note, A2 stock), https://shop.pimoroni.com/en-us/products/pimoroni-pico-plus-2?variant=42092668289107 | `pimoroni-pico-plus-2_PIM724_product-page_2026-09-30.html` | changes per fetch |
| PPP2-BOARD | Pimoroni board files, pimoroni-pico-rp2350 @ e2d60b2, `boards/pimoroni_pico_plus2/` (tracked) | `pimoroni-pico-rp2350_e2d60b2/` | – |

## Toolchain

| tag | what |
|---|---|
| SDK | pico-sdk 2.2.0 (`~/.pico-sdk/sdk/2.2.0`): `boards/pimoroni_pico_plus2_rp2350.h`, `platform_defs.h`, linker scripts |
| M33-TRM | Arm Cortex-M33 TRM r1p0, 100230_0100_03_en (no instruction cycle table) |

## Raspberry Pi and Linux

| tag | what |
|---|---|
| RP1 | RP1 peripherals v1.1, 2023-11-07, https://datasheets.raspberrypi.com/rp1/rp1-peripherals.pdf (§2.5 clocks, §3.1 GPIO, §3.6 SPI) |
| CM5 | CM5 datasheet, https://datasheets.raspberrypi.com/cm5/cm5-datasheet.pdf (§2.9 GPIO) |
| LINUX | raspberrypi/linux rpi-6.18.y: `drivers/spi/spi-dw-core.c`, `spi-dw-dma.c`, `spidev.c`, `drivers/misc/rp1-pio.c`, `rp1.dtsi` |
| RPI-6020 | SPI clock divider and SCLK duty, https://github.com/raspberrypi/linux/issues/6020 |
| RPI-6994 | RP1 PIO DMA performance, https://github.com/raspberrypi/linux/pull/6994 |
| RPI-7132 | RP1 SPI target mode (unmerged), https://github.com/raspberrypi/linux/pull/7132 |
| RPI-UTILS-116 | piolib DMA throughput, https://github.com/raspberrypi/utils/issues/116 |
| PIOLIB | piolib, https://github.com/raspberrypi/utils/tree/master/piolib |
| USB2 | USB 2.0 specification, Table 5-9 (full-speed bulk limit), https://www.usb.org/document-library/usb-20-specification |

## TI parts and application notes

| tag | what |
|---|---|
| SLOU337A | AFE707xEVM user guide, https://www.ti.com/lit/ug/slou337/slou337.pdf |
| SLOA313 | AFE7070 optimized operation in the VHF band, https://www.ti.com/lit/an/sloa313/sloa313.pdf |
| LMX2572 | SNAS740B, https://www.ti.com/lit/ds/symlink/lmx2572.pdf |
| LMX2572LP | SNAS764, https://www.ti.com/lit/ds/symlink/lmx2572lp.pdf |
| LMX2582 | SNAS680E, https://www.ti.com/lit/ds/symlink/lmx2582.pdf |
| CDCE6214 | SNAS811A, https://www.ti.com/lit/ds/symlink/cdce6214.pdf |
| LMK1C1104 | SNAS791D, https://www.ti.com/lit/ds/symlink/lmk1c1104.pdf |
| ADF4351 | ADI ADF4351 Rev. A, https://www.analog.com/media/en/technical-documentation/data-sheets/ADF4351.pdf |

Prices and lifecycle status are from ti.com product pages on 2026-09-29 and change over time.

## Project context

Paths are relative to the repo root.

- `../IREC/Pigeon_Vision/DESIGN.md` §4: 1.28 GHz design point, E200 profile, AFE7071 comparison.
- `../SATS/Next Satellite/research/mcu-qpsk-feasibility.json`: SATS rate assumptions.
