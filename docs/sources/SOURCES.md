# Downloaded sources

The PDFs and text extracts in this folder are not tracked (see `.gitignore`); re-download from
the URLs and compare hashes. Retrieved 2026-09-29.

| file | source | sha256 |
|---|---|---|
| afe7071-rev-c.pdf | TI SLOS789C, https://www.ti.com/lit/gpn/AFE7071 | c1602d4a98dd72976d13e2bf02fa8d3fdf33401e2dde7d913f6c751edc0b8413 |
| rp2350-datasheet.pdf | Raspberry Pi, build 2025-07-29 d126e9e, https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf | 2877d0f270fb6d6a57943bee58aaad536aa027bea1e5b1c4ce2541a3230d4be8 |
| en_30230701v010401p.pdf | ETSI EN 302 307-1 V1.4.1 (2014-11), https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf | 19077f80420eb17519fcf6f83e2847d7a877940b134c5306814b8c6f60d8475d |
| Pimoroni_Pico_Plus_2_Schematic.pdf | https://cdn.shopify.com/s/files/1/0174/1800/files/Pimoroni_Pico_Plus_2_Schematic.pdf?v=1724926880 | fb84bd32f63b18296d19af1e8d4d9126b80bb674f042bcf40f4ef3e2788c1418 |
| Pimoroni_Pico_Plus_2_W_Schematic.pdf | https://cdn.shopify.com/s/files/1/0174/1800/files/Pimoroni_Pico_Plus_2_W_Schematic.pdf?v=1727350279 | cdb3d8c7c03ff2bbbf640867d6d6ac98fbb8eb0c8f307732e55bb63a8475ca5e |
| pimoroni-pico-plus-2-mechanical-diagram.pdf | https://cdn.shopify.com/s/files/1/0174/1800/files/pimoroni-pico-plus-2-mechanical-diagram.pdf?v=1725452657 | 8a265a01f188e749dc653f240d83d6bca8136ce119e7755cd00942bbe0819d91 |
| ppico_plus_2_pinout_diagram.pdf | https://cdn.shopify.com/s/files/1/0174/1800/files/ppico_plus_2_pinout_diagram.pdf?v=1723557334 | b430f43963c1dc975ccd98e703013e755b221cf6871fafe36c724f1dd4153728 |
| pimoroni-pico-plus-2_PIM724_product-page_2026-09-30.html | https://shop.pimoroni.com/en-us/products/pimoroni-pico-plus-2?variant=42092668289107 (page changes per fetch; hash not reproducible) | – |
| pimoroni-pico-rp2350_e2d60b2/ | Pimoroni board files at commit e2d60b2, see `../board-pico-plus-2.md` | – |

The text extracts (`*.txt`) are `pdftotext -layout` output from poppler (Homebrew); the DVB-S2
table generator uses `pdftotext -raw` (see `reference/dvbs2/gen_tables.py`).
