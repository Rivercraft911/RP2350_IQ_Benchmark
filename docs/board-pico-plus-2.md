# Pimoroni Pico Plus 2 (PIM724): verified hardware

Checked 2026-09-29 against Pimoroni and Raspberry Pi sources only (listed at the end). The board was not touched. Citations use the tags in [Sources](#sources); `S2 sh1` means schematic sheet 1.

## Conclusion: pin block for D[13:0] + clock + IQ_FLAG

**Use GPIO0–15.** GPIO0–22 is a run of 23 consecutive GPIOs. Every one is on the 40-pin header and none has an on-board function (S2 sh1, sh3), so a 16- or 17-pin block fits.

| Signal | GPIO | Header pin |
|---|---|---|
| D0 (LSB) … D13 (MSB) | GPIO0 … GPIO13 | 1, 2, 4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 17 |
| IQ_FLAG | GPIO14 | 19 |
| CLK (to AFE7071 CLK_IO) | GPIO15 | 20 |
| optional 17th pin (e.g. SYNC_SLEEP) | GPIO16 | 21 (other row, directly across from pin 20) |

Why this block:
- All 16 signals sit in order on one 1×20 row (pins 1–20). Four GND pins (3, 8, 13, 18) sit between them. Every other 16-pin block in 0–22 splits across both rows (S2 sh3, S3).
- PIO mapping: `out pins, 15` with OUT_BASE=0 writes D[13:0] and IQ_FLAG together, and a 1-bit side-set on GPIO15 drives CLK. If you want both CLK and IQ_FLAG on side-set instead, use 2-bit side-set on GPIO14–15 with `out pins, 14`. Keep GPIOBASE=0 on that PIO block (S10 §11.1.1 p.877; GPIOBASE register p.956).
- It leaves GPIO16–22 free for control: UART1 on GP20/21, UART0 on GP16/17, and HSTX on 16–19 (S3). The SP/CE connector (GPIO32–35) is already the SDK's default SPI0 (S5). It can carry the AFE7071 serial config port.

What this block costs:
- **GP0/GP1 are the SDK default UART** (`PICO_DEFAULT_UART_TX/RX_PIN 0/1`, S5). Disable stdio UART and use USB CDC, or override the UART to UART1 on GP20/21. If you don't, `stdio_init_all()` takes D0/D1.
- **GP4/GP5 also go to the Qw/ST connector X3.** They are the SDK default I2C0 (S2 sh2, S5). Leave Qw/ST unplugged.
- **HSTX pins GP12–15 are used.** Every 16-pin block inside 0–22 overlaps HSTX (12–19), so this can't be avoided. HSTX has only 8 outputs and cannot drive a 14-bit bus anyway (S10 §12.11 p.1202).

Alternative: if you need hardware UART on GP0/1, use GPIO2–17. The costs are that GP16/17 are on the other row, and Qw/ST is still consumed.

## Identity

- Product: Pimoroni Pico Plus 2, SKU **PIM724**. The only variant is id 42092668289107, titled "16MB" (S1). The schematic title block reads "Pimoroni Pico Plus 2 (PIM724)" (S2, all sheets).
- MCU: RP2350B (QFN-80, 48 GPIO), 16 MB QSPI flash, 8 MB PSRAM, USB-C (S1, S2 sh1–2). This matches picotool's report (A2, QFN80, 16 MiB).
- Pimoroni states that current PIM724 stock is A2 and affected by erratum RP2350-E9. For inputs that are pulled low, it advises an external pull-down of 8.2 kΩ or less (S1). This matters only for pins used as inputs, for example if the AFE7071 CLK_IO is ever run in output-clock mode.
- **Pico Plus 2 W is a separate product, PIM726** (S8, S9). You can tell them apart by looking at the board:
  - The W has a Raspberry Pi RM2 wireless module and no SP/CE connector.
  - On the W, RM2 uses GPIO23 (WL_ON), GPIO24 (WL_D) and GPIO25 (WL_CS), plus GPIO29 (WL_CLK).
  - On the W, the LED is on RM2 WL_GPIO0, not GP25.
  - Pimoroni says current W stock is A4 stepping.
  - The W's SDK board is `pimoroni_pico_plus2_w_rp2350` (S5w).
  - A2 silicon is consistent with PIM724 but does not prove it. The SP/CE connector is the physical check.

## SDK board header (pico-sdk 2.2.0, `pimoroni_pico_plus2_rp2350.h`, S5)

Pass **`PICO_BOARD=pimoroni_pico_plus2_rp2350`**.

| Macro | Value |
|---|---|
| `PICO_PLATFORM` | rp2350 (cmake) |
| `PICO_RP2350A` | **0** (0 means the B package). The SDK has no `PICO_RP2350B` macro, and `tools/check_board_header.py` flags any header that defines one (S6). |
| `PICO_FLASH_SIZE_BYTES` | 16 × 1024 × 1024 |
| `PICO_BOOT_STAGE2_CHOOSE_W25Q080`, `PICO_FLASH_SPI_CLKDIV` | 1, 2 |
| `PIMORONI_PICO_PLUS2_PSRAM_CS_PIN` | 47 (no PSRAM size or driver macro) |
| `PIMORONI_PICO_PLUS2_USER_SW_PIN` | 45 |
| `PICO_DEFAULT_LED_PIN` | 25 |
| `PICO_VBUS_PIN` / `PICO_VSYS_PIN` | 24 / 43 |
| `PICO_DEFAULT_UART` TX/RX | UART0, 0 / 1 |
| `PICO_DEFAULT_I2C` SDA/SCL | I2C0, 4 / 5 |
| `PICO_DEFAULT_SPI` SCK/TX/RX/CSN | SPI0, 34 / 35 / 32 / 33 (the SP/CE pins) |
| `SPCE_*` | SPI 0; TX_MISO 32, RX_CS 33, NETLIGHT_SCK 34, RESET_MOSI 35, PWRKEY_BL 36 |
| `PICO_RP2350_A2_SUPPORTED` | 1 |
| Crystal | **Not set.** The SDK defaults apply: `XOSC_HZ` = 12 000 000 and `PICO_XOSC_STARTUP_DELAY_MULTIPLIER` = 6 (S6). |

Pimoroni's MicroPython build also uses PSRAM CS 47 and SP/CE on 32–36. That build targets the W header, `pimoroni_pico_plus2w_rp2350` (S7).

## Schematic facts (S2)

| Item | Finding | Cite |
|---|---|---|
| Crystal | X1 with 10 pF load caps (C24, C35) and 1 kΩ series resistor on XOUT (R13). **The frequency is not printed. Unverified.** 12 MHz is strongly indicated: the SDK header relies on the 12 MHz default, and the RP2350 bootrom assumes 12 MHz for USB unless OTP overrides it (S10 p.375). Confirm on hardware: USB enumeration at the SDK default clocks requires a 12 MHz XOSC. | S2 sh2, S5, S6 |
| IOVDD | **Fixed at 3.3 V.** All IOVDD pins, USB_OTP_IOVDD, QSPI_IOVDD and VREG_VIN tie directly to the 3V3 net. No jumper or selector. The "1.8–3.3 V" text in the symbol is the chip's range, not a board option. The AFE7071 IOVDD must therefore be 3.3 V, which it allows (1.71–3.6 V, S11 p.4), or you must level-shift. | S2 sh1 |
| 3V3 regulator | U8 **AP7366EA-33SN-7** LDO fed from VSYS. Enable is 3V3_EN (header pin 37) with a 100 kΩ pull-up to VSYS. Pimoroni rates it at 600 mA max; input range is 3–5.5 V. | S2 sh2, S1 |
| Core 1V1 | RP2350 internal switching regulator, with L1 MLZ2012M3R3HT000 | S2 sh1 |
| VBUS→VSYS | D2 PMEG40T30ERX Schottky diode | S2 sh2 |
| PSRAM | U1 **APS6404L-3SQR-ZR** (8 MB), on the shared QSPI bus. **CS = GPIO47** (QFN pin 58), with a 100 kΩ pull-up (R12). The printed note "PSRAM_CS can be GP0, GP8, GP19 or GP47" lists the chip's CS1 options; the board wires GP47 only. | S2 sh1–2 |
| Flash | U2 W25Q128JVPIQ (16 MB). The BOOT button pulls QSPI CS low through R4 (1 kΩ) and D1 (NSR0530HT1G). | S2 sh2 |
| USB | USB-C receptacle, 5.1 kΩ CC pull-downs (R7, R9), D+/D− through R1/R2 (labelled "27R4") | S2 sh1–2 |
| SWD | X2 **BM03B-SRSS-TB** (JST-SH 3-pin): pin 1 SWCLK, pin 2 GND, pin 3 SWDIO. No SWD pads on the 40-pin header. | S2 sh2–3 |
| Qw/ST | X3 4-pin: pin 1 GND, pin 2 3V3, pin 3 GP4_SDA, pin 4 GP5_SCL. **No I2C pull-ups drawn.** | S2 sh2 |
| SP/CE | J3 8-pin: pin 1 GND, pin 2 GP36 (PWRKEY/BL), pin 3 GP32 (TX/MISO), pin 4 GP35 (RESET/MOSI), pin 5 GP34 (SCK), pin 6 GP33 (CS), pin 7 3V3, pin 8 VSYS | S2 sh1, S3 |

### GPIO allocation (all 48)

| GPIO | Board use | Cite |
|---|---|---|
| 0–22 | 40-pin header, direct connection. 4 and 5 also go to Qw/ST. | S2 sh1, sh3 |
| 23 | Not connected | S2 sh1 |
| 24 | VBUS_SENSE (10 k/10 k divider, VBUS/2) | S2 sh1–2 |
| 25 | User LED (470 Ω resistor, green LED) | S2 sh2 |
| 26, 27, 28 | Header pins 31, 32, 34, **and** each tied through 1 kΩ (RN2) to GPIO40/41/42 | S2 sh1, S3 |
| 29, 30, 31 | Not connected | S2 sh1 |
| 32–36 | SP/CE connector only | S2 sh1 |
| 37, 38, 39 | Not connected | S2 sh1 |
| 40, 41, 42 (ADC0–2) | Reach header GP26/27/28 through the 1 kΩ resistors only | S2 sh1, S3 |
| 43 (ADC3) | VSYS_SENSE: 150 k/75 k divider (VSYS/3) through Q1 SSM3K35AMFV, gate on 3V3 | S2 sh1–2 |
| 44, 46 | Not connected | S2 sh1 |
| 45 | USER_SW (BOOT button through RN1, active low) | S2 sh2, S1 |
| 47 | PSRAM CS | S2 sh1–2 |

The RP2350B extra GPIOs (30–47) come off the board only as **32–36 on SP/CE** and **40–42 through 1 kΩ onto header pins GP26–28**. Nothing else in that range is exposed.

## Consecutive exposed free runs

"Free" means no on-board function other than a bare connector.

| Run | Length | Where | Caveats |
|---|---|---|---|
| **GPIO0–22** | **23** | GP0–15 on left row pins 1–20, in order. GP16–22 on right row pins 21–29. | GP0/1 default UART, GP4/5 default I2C and Qw/ST stub, GP12–19 HSTX |
| GPIO32–36 | 5 | SP/CE JST-SH, connector pin order 3, 6, 5, 4, 2 | Default SPI0. Needs GPIOBASE=16 if driven by PIO, which cuts that PIO block off from GPIO0–15. |
| GPIO26–28 | 3 | Header pins 31, 32, 34 | 1 kΩ to GPIO40–42. Never enable outputs on both ends. |

16-pin block options inside 0–22:

| Block | Rows | Loses |
|---|---|---|
| **0–15 (recommended)** | Left row only | UART0 default, Qw/ST, HSTX 12–15 |
| 2–17 | 14 left + 2 right | Qw/ST, HSTX 12–17 |
| 6–21 | 10 left + 6 right | All HSTX 12–19, UART1 GP20/21 |
| 7–22 | 9 left + 7 right | All HSTX 12–19, UART1 GP20/21 |

- **HSTX-capable exposed pins:** GPIO12–19, all on the header: pins 16, 17, 19, 20 (GP12–15) and 21, 22, 24, 25 (GP16–19). HSTX is output-only (S10 p.1202, S3).
- **ADC pins:** on RP2350B the ADC is on GPIO40–47, and GPIO26–28 are digital only (S10 §12.4 p.1070; S6 `ADC_BASE_PIN 40`). The only exposed ADC inputs are header GP26/27/28, which reach ADC0–2 through 1 kΩ. GPIO43 (ADC3) is VSYS sense. The ADC_VREF header pin (35) is the node on the chip's ADC supply pin (pin 59, labelled ADC_IOVDD), fed through 220 Ω (R3) with 2.2 µF (C9) (S2 sh1).

## Off-board fast-signalling notes

- **No series resistors, buffers, pull-ups or termination** on GPIO0–22 between the RP2350 pads and the header (S2 sh1, sh3). Add any source termination off-board.
- GP4/GP5 carry an extra trace stub to the Qw/ST connector. GP26–28 carry a 1 kΩ branch to the GPIO40–42 pads (S2 sh1–2).
- The header is 2×20 in the Pico footprint. The mechanical drawing does not dimension pitch. Scaled from the drawing, pitch is 2.54 mm and row spacing is 17.8 mm (S4). Pimoroni pairs the board with 1×20 0.1" headers (S1).
- **Ground:** the 40-pin header has 8 GND pins (3, 8, 13, 18, 23, 28, 33, 38). On the recommended row that is 4 GND pins for 16 signals, with at most 4 signal pins between grounds. Extra GND is on Qw/ST pin 1, SP/CE pin 1 and SWD pin 2 (S2 sh2–3).
- Only IOVDD 3.3 V is available on-board. The regulator is a 600 mA LDO shared with flash, PSRAM and the MCU (S1, S2 sh2).

## Unverified

- The crystal frequency and part number. The schematic does not state them.
- Pad drive and slew settings, trace lengths and stub lengths. The schematic doesn't cover them, and Pimoroni publishes no layout.
- Castellated edge pads. The drawing hints at them but nothing states it.

## Sources

Retrieved 2026-09-29 (PDT) with curl. Local paths are relative to `docs/`.

| Tag | What | URL | Local file | sha256 |
|---|---|---|---|---|
| S1 | PIM724 product page (en-us, variant 42092668289107) | https://shop.pimoroni.com/en-us/products/pimoroni-pico-plus-2?variant=42092668289107 | `sources/pimoroni-pico-plus-2_PIM724_product-page_2026-09-30.html` (dynamic page; the hash changes on refetch) | 4d69fb7e733ec20595f38ae1c983411cd120ffd4a79826128fa39b9ad085bc0f |
| S2 | Schematic, 3 sheets, dated 29/08/2024 ("ppico_plus_non_w.sch") | https://cdn.shopify.com/s/files/1/0174/1800/files/Pimoroni_Pico_Plus_2_Schematic.pdf?v=1724926880 | `sources/Pimoroni_Pico_Plus_2_Schematic.pdf` | fb84bd32f63b18296d19af1e8d4d9126b80bb674f042bcf40f4ef3e2788c1418 |
| S3 | Pinout diagram (PDF and PNG) | https://cdn.shopify.com/s/files/1/0174/1800/files/ppico_plus_2_pinout_diagram.pdf?v=1723557334 and the same path `.png?v=1723557327` | `sources/ppico_plus_2_pinout_diagram.pdf`, `.png` | PDF b430f43963c1dc975ccd98e703013e755b221cf6871fafe36c724f1dd4153728; PNG 65e8c30f4c1b47713d1d8ff2409bb1d4fe34cf548c7e0228a8bcb1fb966744a1 |
| S4 | Mechanical diagram | https://cdn.shopify.com/s/files/1/0174/1800/files/pimoroni-pico-plus-2-mechanical-diagram.pdf?v=1725452657 | `sources/pimoroni-pico-plus-2-mechanical-diagram.pdf` | 8a265a01f188e749dc653f240d83d6bca8136ce119e7755cd00942bbe0819d91 |
| S5 | pico-sdk 2.2.0 board header (commit a1438dff) | `~/.pico-sdk/sdk/2.2.0/src/boards/include/boards/pimoroni_pico_plus2_rp2350.h` | not copied | 0a87a90fc6a0cdcabbb5a25f4ef6cdc7366c46a3d39fbed9df811323d8fcb816 |
| S5w | W header, same SDK | `…/boards/pimoroni_pico_plus2_w_rp2350.h` | not copied | 3c02f6655aceda33ac98d524542b0319787392346d1c4bb0e874aeb48bfe90a3 |
| S6 | SDK defaults: `src/rp2350/hardware_regs/include/hardware/platform_defs.h` (XOSC_HZ, ADC_BASE_PIN), `src/rp2350/pico_platform/include/pico/platform.h` (PICO_RP2350A), `src/rp2_common/hardware_xosc/include/hardware/xosc.h`, `tools/check_board_header.py` l.429 | local SDK 2.2.0 | not copied | n/a |
| S7 | Pimoroni GitHub, pimoroni-pico-rp2350 @ e2d60b2b8e928c2aae0cfd71585416d02766e5a2, `boards/pimoroni_pico_plus2/` | https://github.com/pimoroni/pimoroni-pico-rp2350/tree/e2d60b2b8e928c2aae0cfd71585416d02766e5a2/boards/pimoroni_pico_plus2 | `sources/pimoroni-pico-rp2350_e2d60b2/mpconfigboard.cmake`, `pins.csv` | e7af013168410cad2f8092649547a00a71061430e0d52032b02a6cb2f4c1081b; 9b40d65c594949f874fe926d0faf66d1e1e4175b3d086a6538af76254a2dbb40 |
| S8 | Pico Plus 2 W product page (PIM726) | https://shop.pimoroni.com/products/pimoroni-pico-plus-2-w | not saved | n/a |
| S9 | Pico Plus 2 W schematic, dated 26/09/2024 | https://cdn.shopify.com/s/files/1/0174/1800/files/Pimoroni_Pico_Plus_2_W_Schematic.pdf?v=1727350279 | `sources/Pimoroni_Pico_Plus_2_W_Schematic.pdf` | cdb3d8c7c03ff2bbbf640867d6d6ac98fbb8eb0c8f307732e55bb63a8475ca5e |
| S10 | RP2350 datasheet, build-date 2025-07-29. Printed page numbers; PDF page = printed + 1. | (already in `sources/`) | `sources/rp2350-datasheet.pdf` | 2877d0f270fb6d6a57943bee58aaad536aa027bea1e5b1c4ce2541a3230d4be8 |
| S11 | TI AFE7071 datasheet SLOS789C (pin functions p.3, recommended operating conditions p.4) | (already in `sources/`) | `sources/afe7071-rev-c.pdf` | c1602d4a98dd72976d13e2bf02fa8d3fdf33401e2dde7d913f6c751edc0b8413 |
