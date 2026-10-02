# CM5 to Pico bench data

Native SPI delivered **29.99 fps per camera at 9 Mb/s MPEG-TS** over a two-minute
run. Two IMX900s at 2064×1552, x264 ultrafast at 4 Mb/s each, local recording,
cached buffers and three encoder threads per camera. SPI requested 20 MHz;
Pico Plus 2 ran its 8 Msym/s QPSK pipeline. No RF hardware was connected.

## Sender comparison

These runs used a fan. FPS and CPU exclude the first ten seconds; CPU is the
percentage of all four CM5 cores. Python runs used loopback UDP. Native SPI
ran in `pv-capture`'s transport worker.

| Run | Seconds | FPS A / B | CPU | Intervals >50 ms A / B |
| --- | ---: | ---: | ---: | ---: |
| `duplex-fan-control-60s` | 60 | 29.11 / 29.11 | 81.3% | 45 / 45 |
| `tx-only-t3-60s` | 60 | 29.30 / 29.33 | 80.4% | 35 / 34 |
| `direct-tx-normal-priority-60s` | 60 | 29.78 / 29.80 | 78.8% | 11 / 10 |
| `native-t3-retry-60s` | 60 | 30.00 / 30.00 | 60.5% | 0 / 0 |
| `native-t3-120s` | 120 | 29.99 / 29.99 | 61.4% | 1 / 1 |

The 120-second run sent 102,801 messages and 135,285,740 payload bytes. Pico
counts and CRC `0x0f20927e` matched, with one sequence wrap and zero protocol
errors or underruns. Maximum temperature: 56.75°C, no throttling. Four recordings
decoded: 3,599 frames from A, 3,598 from B. Each camera had one startup error.
The two later 66.7 ms intervals remain unexplained. Pico firmware was unchanged.

Reserving a CPU core for Python caused encoder queue drops. Copying camera
frames before encoding fell to 25.6 fps. Neither is selected. Native SPI removes
the Python/UDP workload; these tests do not isolate each source of overhead.

## Earlier runs

| Run | Capture time | FPS A / B | Result |
| --- | ---: | ---: | --- |
| `current-uncached-60s` | 60 s | 27.05 / 27.51 | Encoder queue drops |
| `current-cached-60s` | 60 s | 28.73 / 28.79 | Fewer queue drops |
| `current-cached-t3-60s` | ~18 s | 29.25 / 29.12 | UDP queue overflow |
| `current-cached-t3-priority-60s` | 60 s | 28.71 / 28.63 | Sender nice −5 |
| `current-cached-t3-priority-600s` | 210 of 600 s | 28.42 / 28.41 | Stopped at 80°C without a fan |
| `camera-only-control-90s` | 90 s | 30.00 / 30.00 | Local UDP drain, no SPI |

The thermal-stop run's 179,818 messages matched the Pico, CRC `0x465758f0`, with
zero protocol errors or underruns. Eight recordings decoded, 5,975 frames per
camera. Pico's later null output is not additional camera video.

`pattern-20mhz-60s` reran the pattern at full channel rate on firmware `f51d833`, after
the review fixes (PV-SPI pin release, per-message realign). 58,860 messages and CRC
`0x793a718a` matched, with zero protocol errors or underruns; READY waits peaked at 1.8 ms.

`native-t3-60s` failed GPIO discovery before capture. `native-t3-600s` was cancelled
and has no Pico report. `profile-sender-30s` includes profiler overhead and failed
on UDP overflow. All are retained as failed or incomplete runs.

## Files

- `runs.csv`: per-camera measurements, duration and failures.
- Each run: raw reports, configuration, source/binary hashes, `telemetry.csv`, `frames.csv`, `comparison.json`.
- `firmware/`: flashed UF2, hash, base revision and READY-readback patch.
- `summary.json`: initial pattern tests and camera comparisons.
- `decoded-segments.json` in recorded runs: offline decode counts. Video stays outside Git.

## Reproduce

Use [the bench wiring](../../docs/pv-spi-spec.md). No other camera or Pico test
should be running. Never enable Pico SPI self-test with the CM5 wired to it.
Build the current PigeonVision capture executable on the CM5, then:

```sh
python3 host/cm5/run_live.py --native-spi --label new-run --seconds 120 \
  --allocator dma_heap_cached --threads 3 \
  --port /dev/cu.usbmodemYOUR_PORT --ssh-config /path/to/ssh_config --ssh-host cm5 \
  --remote-root /path/to/PigeonVision --capture-config /path/to/capture.json \
  --binary /path/to/pv-capture
python3 host/cm5/analyze_live.py results/cm5-spi/new-run
python3 host/cm5/export_runs.py
python3 -m unittest discover -s host/cm5 -p 'test_*.py'
```

Native mode needs SPI/GPIO access and a working camera config. It does not use
Python SPI packages or `sysctl`. For the Python comparison, omit `--native-spi`,
add `--transfer tx-only --udp-mode direct`, and install PigeonVision's SPI extra.
That path also needs permission to set the UDP receive buffer with `sysctl`.

The runner saves its remote script, checks effective settings and stops at 80°C.
Analysis exits nonzero on incomplete or inconsistent evidence. Use `--camera-only`
to analyze the UDP-discard control without claiming Pico acceptance. Final wiring
timing, RF modulation quality and the over-air link remain unmeasured.
