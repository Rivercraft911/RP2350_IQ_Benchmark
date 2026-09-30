# DVB-S2 QPSK reference transmitter

Python model of the DVB-S2 transmit chain from user bits to PLFRAME symbols, QPSK only, for the RP2350
firmware to match bit for bit. Standard: ETSI EN 302 307-1 V1.4.1 (2014-11) [S1]. Clause, table and figure
numbers refer to it.

Result: the model agrees bit for bit with gr-dtv [S2] at every stage in all 21 QPSK modes, with and without
pilots. It agrees with leansdr [S3] in 18 modes. In the other 3, leansdr's own LDPC tables are wrong.

```
python3 reference/dvbs2/test_dvbs2.py          # all checks, ~4 s
sh reference/dvbs2/crosscheck/build.sh         # optional: fetch + build gr-dtv and leansdr for a live cross-check
python3 reference/dvbs2/gen_tables.py          # regenerate dvbs2_tables.py from the PDF (needs poppler pdftotext)
python3 reference/dvbs2/test_dvbs2.py --record # rewrite crosscheck/digests.json from a live gr-dtv/leansdr run
```

## Files

| File | Content |
|---|---|
| `dvbs2.py` | The model, numpy only |
| `dvbs2_tables.py` | Tables 5a/5b, 6a/6b, 7a/7b and Annex B/C, generated from the PDF text. `reference/gen_dvbs2_codes.py` reads it. |
| `gen_tables.py` | Generator for the tables file. It checks the PDF hash and records the provenance. |
| `test_dvbs2.py` | Checks, listed under Verification |
| `crosscheck/` | `build.sh` (pinned commits), harnesses `grdtv_tx.cc` (with runtime stubs `gr_stub/`) and `leansdr_tx.cc`, `check_tables.py`, and `digests.json` (stage digests recorded from the two external programs) |

## What is implemented

| Stage | Function | Clause |
|---|---|---|
| CRC-8, g = X^8+X^7+X^6+X^4+X^2+1 | `crc8` | 5.1.4, Figure 2 |
| BBHEADER (MATYPE, UPL, DFL, SYNC, SYNCD, CRC-8) | `bbheader`, `matype1` | 5.1.6, Table 3 |
| GCS BBFRAME with zero padding | `bbframe` | 5.1.5, 5.2.1 |
| Single-TS BBFRAMEs (CRC-8 per packet, SYNCD), only for the cross-check | `ts_bbframes` | 5.1.4-5.1.6, Table 4 |
| BB scrambling, PRBS 1+X^14+X^15 | `bb_prbs`, `bb_scramble` | 5.2.2, Figure 5 |
| BCH, bit-serial and 256-entry byte table | `bch_encode`, `bch_encode_bytes` | 5.3.1, Tables 5, 6 |
| LDPC, bit-serial as written in the standard | `ldpc_encode` | 5.3.2, Table 7, Annex B/C |
| LDPC, 360-bit group form (the firmware form, identical output) | `ldpc_encode_groups` | same |
| QPSK mapping | `qpsk_map` | 5.4.1, Figure 9 |
| PLHEADER: SOF, PLS code, pi/2-BPSK | `plheader`, `pls_code` | 5.5.2 |
| Pilots | `insert_pilots` | 5.5.3 |
| PL scrambling, Gold code n (default 0) | `pl_scrambling_sequence`, `pl_scramble` | 5.5.4 |
| PLFRAME as (bI, bQ) bits, whole-file helper | `plframe`, `transmit` | 5.5 |
| Firmware words | `pack_words` | (`reference/iqlut.py` layout) |

Scope: MODCOD 1-11 (QPSK), normal and short FECFRAMEs, pilots on or off, any n.

**Mode adaptation for file transfer: Generic Continuous Stream.** The field values are MATYPE-1 = `01 1 1 0 0 RO`
(GCS, single stream, CCM, no ISSY, no null-packet deletion; 0x70 for roll-off 0.35), MATYPE-2 = 0, UPL = 0,
DFL = user bits in the frame, SYNC = 0x00 and SYNCD = 0 (5.1.6). The reasons:
- File framing, sequence numbers and a strong CRC belong to the application layer.
- GCS skips the per-packet CRC-8 (5.1.4) and needs no slicer bookkeeping.
- DFL marks the valid bits, so the last frame of a file is short and zero-padded (5.2.1).
- Application packets aligned to BBFRAMEs are lost only with their own frame.

SYNC values B9-FF are "user private" (5.1.6) if a private protocol tag is wanted.

**Symbol bits.** A symbol is a bit pair: I = (1-2bI)/sqrt2, Q = (1-2bQ)/sqrt2. Every PLFRAME symbol lies on
one of the four QPSK points:

| Symbol | (bI, bQ) |
|---|---|
| Data (Figure 9) | (FEC bit 2k, FEC bit 2k+1) |
| PLHEADER bit y, 0-based even index k: (1-2y)(1+j)/sqrt2 | (y, y) |
| PLHEADER bit y, odd k: (1-2y)(-1+j)/sqrt2 | (1-y, y) |
| Pilot, (1+j)/sqrt2 | (0, 0) |
| After scrambling by exp(jR pi/2): R = 0, 1, 2, 3 | (bI, bQ), (1-bQ, bI), (1-bI, 1-bQ), (bQ, 1-bI) |

The PLHEADER is not scrambled. `pack_words` puts symbol j of word w at I bit j and Q bit 16+j. A final
partial word is padded with (0, 0). Frame lengths are 33282 / 32490 symbols (normal, with and without pilots)
and 8370 / 8190 (short). They are even but not multiples of 16. Pack a multi-frame stream in one call:
8 frames always fill whole words.

## Verification

Checked by `test_dvbs2.py`:
- **Tables.** Values are extracted from the PDF.
  - All 21 LDPC tables are identical, row by row, to gr-dtv [S2] and xdsopl/LDPC [S4].
  - Tables 5, 6 and 7 are mutually consistent: deg g = Nbch-Kbch = 16t or 14t, nldpc-kldpc = 360q, and
    there are kldpc/360 rows.
  - The worked address examples of 5.3.2.1 (rate 2/3) and Annex B (Table B.4) are reproduced.
- **CRC-8.** The check value 0xBC matches the RevEng catalogue entry CRC-8/DVB-S2 [S5].
- **BB PRBS.** The sequence starts 00000011 as in Figure 5, with period 32767.
- **PLS code.** The 128 codewords have d_min = 32, as 5.5.2 states.
- **BCH.** c(x) is divisible by g(x) (long division) in all 21 modes. The byte-table encoder gives the same
  output.
- **LDPC.** H·c = 0 in all 21 modes, with H built directly from the tables. The serial and group-form
  encoders give the same output.
- **Constellation.** Every PLFRAME symbol equals the complex value from the formulas of 5.4.1 and
  5.5.2-5.5.4, and lies on (±1±j)/sqrt2.
- **Round trip.** A GCS round trip recovers the BBHEADER fields, the padding and the data.
- **`pack_words`.** It is the inverse of `iqlut.unpack_bits`.
- **Independent cross-check.** The cross-check uses TS input, because both programs only generate TS mode:
  42 configurations (21 modes × pilots on/off), 3 consecutive frames each.
  - Five stages are compared: BBFRAME, scrambled BBFRAME, BCH codeword, FECFRAME and PLFRAME symbols.
  - gr-dtv matches in all 42. It also matches for Gold codes n = 1, 99999 and 262141 (normal 1/2 and short
    2/3, with pilots).
  - leansdr matches in 36. In normal 1/3, short 2/5 and short 3/4 it agrees through the BCH codeword and
    then differs. Its tables (`dvbs2_data.h`) split the 1/3 normal rows 12/11/13, following the PDF's line
    breaks, and carry wrong entry counts in short 2/5 and 3/4.
  - Both programs' symbols have |I| = |Q| and constant amplitude.
  - Errors injected by hand into the model (one PLS bit, the CRC, n = 1 instead of 0) each make the
    cross-check fail.
  - With `crosscheck/build/` present the programs run live. Without it, the model is compared with
    `digests.json`, which was recorded from the two programs.

Not verified:
- **GCS header values.** They are checked only by reading Table 3 and by the round trip. Neither reference
  program generates GCS, and no receiver has decoded this output.
- **Real receivers.** No receiver has been tested. A digital match is not evidence of RF performance. The
  next check is to decode captured AFE7071 output, or the packed words looped back, with a real receiver
  (gr-dvbs2rx, leandvb or a hardware demodulator).
- **RO field.** Only roll-off 0.35 (RO = 00) is exercised.
- **Firmware word-level code.** It is not modelled here. `ldpc_encode_groups` is the vector-level form, and
  the firmware must be compared against `dvbs2.py` outputs.

Findings about the sources:
- **Text error in the standard.** Clause 5.3.1 says "table 5b" for the short-frame polynomials; Table 6b is
  meant.
- **LDPC row boundaries.** The PDF typesets Annex B/C as flowed text and runs some rows together (Table B.2
  rows 2-3, for example), so the row boundaries cannot be recovered from the PDF alone. `gen_tables.py`
  takes the values from the PDF and the row lengths from gr-dtv and xdsopl, which agree with each other.
  leansdr's normal 1/3 table shows the trap: it follows the PDF's line breaks.

## Cortex-M33 port: operation counts

**LDPC group form.** The parity bits sit in a q × 360 bit matrix, 12 words per row (11.25 used). Per
frame:
- E rotated 360-bit XORs into rows r = x mod q, rotation c = floor(x/q). Store (r, c) per entry.
- G = kldpc/360 input groups, each doubled to a 720-bit buffer so that any rotation is a word-unaligned
  window.
- About 2q row XORs for the accumulation: a prefix down the rows, then the carry from the 360-bit running
  XOR of the last row.
- A q × 360 bit transpose for the natural-order readout.

For even q, the even rows are the parity I bits and the odd rows the Q bits, so the transpose can do the I/Q
split.

| Mode | E entries | G groups | q rows | word XORs 12E | P bytes (48q) | M = n-kldpc |
|---|---|---|---|---|---|---|
| normal 1/4 | 270 | 45 | 135 | 3240 | 6480 | 48600 |
| normal 1/3 | 360 | 60 | 120 | 4320 | 5760 | 43200 |
| normal 2/5 | 432 | 72 | 108 | 5184 | 5184 | 38880 |
| normal 1/2 | 450 | 90 | 90 | 5400 | 4320 | 32400 |
| normal 3/5 | 648 | 108 | 72 | 7776 | 3456 | 25920 |
| normal 2/3 | 480 | 120 | 60 | 5760 | 2880 | 21600 |
| normal 3/4 | 540 | 135 | 45 | 6480 | 2160 | 16200 |
| normal 4/5 | 576 | 144 | 36 | 6912 | 1728 | 12960 |
| normal 5/6 | 600 | 150 | 30 | 7200 | 1440 | 10800 |
| normal 8/9 | 500 | 160 | 20 | 6000 | 960 | 7200 |
| normal 9/10 | 504 | 162 | 18 | 6048 | 864 | 6480 |
| short 1/4 | 63 | 9 | 36 | 756 | 1728 | 12960 |
| short 1/3 | 90 | 15 | 30 | 1080 | 1440 | 10800 |
| short 2/5 | 108 | 18 | 27 | 1296 | 1296 | 9720 |
| short 1/2 | 85 | 20 | 25 | 1020 | 1200 | 9000 |
| short 3/5 | 162 | 27 | 18 | 1944 | 864 | 6480 |
| short 2/3 | 120 | 30 | 15 | 1440 | 720 | 5400 |
| short 3/4 | 108 | 33 | 12 | 1296 | 576 | 4320 |
| short 4/5 | 105 | 35 | 10 | 1260 | 480 | 3600 |
| short 5/6 | 121 | 37 | 8 | 1452 | 384 | 2880 |
| short 8/9 | 125 | 40 | 5 | 1500 | 240 | 1800 |

All 21 tables together hold 6447 entries, 25.8 KB as (r, c) pairs.

**BCH.** The remainder is 192 bits for normal frames with t = 12, 160 bits with t = 10, 128 bits with t = 8,
and 168 bits for short frames (t = 12). A 256-entry byte table takes 6 / 5 / 4 KiB (short: 5.25 KiB, 6 KiB
word-padded). Slicing-by-4 takes 4× that.

Measured firmware cost (normal 2/3 with pilots): 353 k cycles per frame, BCH 110 k, LDPC 127 k,
framing 116 k. The optimization steps are in the top-level README.

## Gaps

- No dummy PLFRAME (5.5.1). The firmware keeps the stream continuous with TS null packets instead.
- No 8PSK/APSK or bit interleaver (5.3.3), ISSY, null-packet deletion, multiple streams, ACM or DVB-S2X.
- Pulse shaping (5.6) is the job of `reference/iqlut.py`. Here roll-off is only the MATYPE RO field.

## Sources

Retrieved 2026-09-29. Tools: Python 3.13.2, numpy 2.2.5, poppler pdftotext 26.06.0, Apple clang 17.0.0.

| Tag | What | Location | Identity |
|---|---|---|---|
| S1 | ETSI EN 302 307-1 V1.4.1 (2014-11), 80 pp. | https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf, local `docs/sources/en_30230701v010401p.pdf` (git-ignored; `.txt` is its `pdftotext -layout` output) | sha256 19077f80420eb17519fcf6f83e2847d7a877940b134c5306814b8c6f60d8475d |
| S2 | GNU Radio gr-dtv, `lib/dvb/*`, `lib/dvbs2/*` (GPL-3.0) | https://github.com/gnuradio/gnuradio | commit aee9fd3f79389c4282a98e8d62c8405c73fd91df (2026-08-28) |
| S3 | leansdr, branch `work` (GPL-3.0), `dvbs2.h`, `dvbs2_data.h`, `bch.h`, `ldpc.h` | https://github.com/pabr/leansdr | commit 84c59e1c7a1a79338d5722d63f28640cc9d350f3 (2022-12-01) |
| S4 | xdsopl/LDPC `dvb_s2_tables.hh` (tables only) | https://github.com/xdsopl/LDPC | commit 32357d8ad55a6a302c34e093759f0454e45cca56 (2023-05-05) |
| S5 | CRC RevEng catalogue, CRC-8/DVB-S2 | https://reveng.sourceforge.io/crc-catalogue/1-15.htm | width 8, poly 0xd5, init 0, check 0xbc |

S2 and S3 are fetched by `build.sh`, not vendored. gr-dtv's blocks are compiled unmodified against stubs.
leansdr is written for GCC on Linux. `build.sh` rewrites one VLA initializer in its BCH decoder
(Berlekamp-Massey) and, on macOS, supplies `exp10f` and `F_SETPIPE_SZ` by `-D`. None of these is on the
transmit path.
