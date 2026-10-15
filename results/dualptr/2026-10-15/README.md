# Live SPI/FEC with hardware QPSK shaping (`dualptr`)

**Result:** at **8 Msym/s** (IREC nominal, N4, 32 MHz GPIO28 DDR clock, SYS128) the live
SPI/FEC pipeline with PIO-generated LUT addresses ran 60 s clean for both SPI GCS and TS
input. Core 0 dropped from 47.2% to 25.7% work against the same-rate CPU-descriptor
control; core 1 is unchanged. At **16 Msym/s** every run stopped within 3–9 ms with output
TX stalls: the two pointer-fed writers cannot hide the sample-DMA handoff once live SPI and
FEC traffic share the bus, although the same two-writer pointer engine ran 60 s from a cyclic
source (results/quad-pointers). Four pointer-fed writers with alternating FIFO ownership are
the identified next step (REPORT.md).

Image `98a9c42f92` (branch `rd/a1-qpsk-hwshape`, UF2 sha256 `0f968ca9…2d87`; flash records
`flash.txt`, `flash-8msym.txt`). Runner `host/dualptr_trial.py`. QPSK 5/6 + pilots, L8, two
writers fed by two L8 pointer SMs, `ldpcpack 1`, `bchbank 1`, on-chip SPI master.

60 s at 8 Msym/s (`a1-8msym-60s/`); every row: full duration, 0 of 4096 bus mismatches,
GPIO28 == IQ_FLAG in all 4096 snapshots:

| Input | Path | Core 0 work | of which supervision / BCH | Core 1 work |
|---|---|---:|---|---:|
| GCS | CPU descriptors, four writers (control) | 47.2% | — / 22.4% | 41.0% |
| GCS | `dualptr` hardware pointers | 25.7% | 3.2% / 22.5% | 40.7% |
| TS | CPU descriptors, four writers (control) | 47.3% | — / 22.5% | 46.7% |
| TS | `dualptr` hardware pointers | 25.8% | 3.2% / 22.6% | 46.4% |

3 s matrix (`a1-matrix-3s/`): at 8 Msym/s both inputs clean, the inverted clock detected
(4096/4096 parity failures, data correct) and credit starvation detected (pointer
SOURCE_EMPTY, no read beyond the frozen grant). At 16 Msym/s all six rows failed with output
TX stalls 3–9 ms after start (`a1-gcs-3s/`, `a1-gcs-60s/`, `a1-ts-60s/` repeat this; their
CPU-descriptor controls passed). "Core 0 work" for `dualptr` times supervision and BCH; the
control's figure is the runner's descriptor-path instrumentation. Frame-parallel FEC exists
only as the native ownership model (`host/test_frame_order.py`).
