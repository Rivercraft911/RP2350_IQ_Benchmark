# CM5 to Pico bench data

Two real IMX900 cameras, 2064×1552 at a requested 30 fps, x264 ultrafast at 4 Mb/s each, 9 Mb/s MPEG-TS, and local recording. SPI requested 20 MHz. Pico Plus 2 ran PV-SPI into its 8 Msym/s QPSK pipeline. No RF hardware was connected.

| Run | Capture time | Encoded fps A / B | Result |
| --- | ---: | ---: | --- |
| `current-uncached-60s` | 60 s | 27.05 / 27.51 | Encoder queues dropped frames; SPI counts and CRC matched |
| `current-cached-60s` | 60 s | 28.73 / 28.79 | Cached buffers reduced queue drops; SPI counts and CRC matched |
| `current-cached-t3-60s` | ~18 s | 29.25 / 29.12 | Failed: CM5 UDP queue overflow |
| `current-cached-t3-priority-60s` | 60 s | 28.71 / 28.63 | Sender nice −5; SPI counts and CRC matched |
| `current-cached-t3-priority-600s` | 210 s of 600 s | 28.42 / 28.41 | Stopped at 80°C; not a completed ten-minute test |
| `camera-only-control-90s` | 90 s | 30.00 / 30.00 | Local UDP drain replacing SPI; no steady-state drops |

FPS excludes the first ten seconds. The native capture binary and camera settings match across the cached/control runs. Starting temperatures differed. The comparison associates the current SPI workload with the frame-rate reduction; it does not isolate scheduling from other shared resources.

The longer SPI attempt sent 179,818 messages, 1,258,721 TS packets and 236,639,548 bytes. Pico accepted them all, with CRC chain `0x465758f0`, two sequence wraps, and zero protocol errors or output underruns. No UDP bytes remained unsent at the thermal stop. All eight local MKV segments decoded: 5,975 frames per camera. The Pico continued inserting null packets until its original 620-second command expired; that is not 620 seconds of camera video.

## Native sender comparison

All runs below used the fan, cached buffers, three encoder threads, 2064×1552,
4 Mb/s per camera, 9 Mb/s TS and local recording. The Python runs retained
loopback UDP; native SPI ran in `pv-capture`'s existing transport worker.

| Run | Seconds | FPS A / B | Total CPU | Intervals >50 ms A / B |
| --- | ---: | ---: | ---: | ---: |
| `duplex-fan-control-60s` | 60 | 29.11 / 29.11 | 81.3% | 45 / 45 |
| `tx-only-t3-60s` | 60 | 29.30 / 29.33 | 80.4% | 35 / 34 |
| `direct-tx-normal-priority-60s` | 60 | 29.78 / 29.80 | 78.8% | 11 / 10 |
| `native-t3-retry-60s` | 60 | 30.00 / 30.00 | 60.5% | 0 / 0 |
| `native-t3-120s` | 120 | 29.99 / 29.99 | 61.4% | 1 / 1 |

The 120-second run sent 102,801 messages and 135,285,740 payload bytes. Pico
counts and CRC `0x0f20927e` matched, with one sequence wrap and zero protocol
errors or underruns. Maximum temperature: 56.75°C, no throttling. Four recordings
decoded without errors: 3,599 frames from A, 3,598 from B. Each camera reported
one startup frame error. The two later 66.7 ms intervals remain unexplained.

Reserving an entire CPU core for the Python sender caused encoder queue drops.
Copying frames before encoding fell to 25.6 fps. Neither is selected. Native
SPI removes the Python/UDP workload; this comparison does not isolate each
individual source of overhead. The native mux-plus-SPI worker used about 0.084
CPU cores in one cumulative sample (`transport-thread.json`).

`native-t3-60s` stopped before capture because discovery counted a GPIO symlink
twice. `native-t3-600s` was cancelled at the user's request and has no Pico report;
the fresh 120-second case is the completed test. `profile-sender-30s` includes
profiler overhead and failed on UDP queue overflow. Those are not successful
performance runs. The Pico firmware was unchanged throughout.

For native runs add `--native-spi --sender-nice 0` to the command below and point
`--binary` to the native SPI build. Native mode uses no Python sender process.

## Files

- `runs.csv`: one row per camera/run, including actual duration and failures.
- Each run: raw Pico/CM5 reports, configuration, source/binary hashes, `telemetry.csv`, `frames.csv`, and `comparison.json`.
- `firmware/`: exact flashed UF2, SHA-256, base revision and source patch. The only firmware change adds READY-pin readback.
- `summary.json`: pattern tests (`smoke-1mhz`, `pattern-*`) and the camera-run comparisons.
- `current-cached-t3-priority-600s/decoded-segments.json`: offline decode counts for the thermal-stop recordings. Video files stay outside Git.

`frames.csv` retains capture timestamps and dropped-frame records. `telemetry.csv` contains cumulative counts and per-interval encoder time. A zero drop counter does not imply 30 fps: the SPI runs have occasional 66 ms delivery intervals. `failures` includes intentional thermal termination and shortened duration, separately from raw transport counters.

## Reproduce

Use the wiring in [the protocol](../../docs/pv-spi-spec.md). Start no other camera or Pico test. Never enable Pico SPI self-test while the CM5 is wired to those pins.

From this repository, after building the current PigeonVision capture executable on the CM5:

```sh
python3 host/cm5/run_live.py --label new-run --seconds 60 \
  --allocator dma_heap_cached --threads 3 --sender-nice -5 \
  --port /dev/cu.usbmodemYOUR_PORT --ssh-config /path/to/ssh_config --ssh-host cm5 \
  --remote-root /path/to/PigeonVision --capture-config /path/to/capture.json \
  --binary /path/to/pv-capture
python3 host/cm5/analyze_live.py results/cm5-spi/new-run
python3 host/cm5/export_runs.py
python3 -m unittest discover -s host/cm5 -p 'test_*.py'
```

The remote checkout needs the SPI-only Python environment from PigeonVision's SPI guide, a working two-camera config, and permission for `sysctl` and `renice`. The runner saves the executed remote script with each case. It stops capture at 80°C, checks effective settings, and preserves failures. Analysis exits nonzero on an incomplete or inconsistent run.

The 90-second control's `run_remote.py` preserves its exact one-off procedure. Analyze it with `--camera-only`; this mode makes no Pico acceptance claim. Physical SPI timing, RF modulation quality and the over-air link remain unmeasured here.
