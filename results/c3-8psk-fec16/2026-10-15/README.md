# On-chip 8PSK FEC at 16 Msym/s does not fit SYS128 with C3's two-core pipeline; 8 Msym/s does

Pico Plus 2, RP2350B A2, SYS128, USB only. `c3fec` (branch `rd/c3-8psk-fec16`): in-memory GCS
BBFRAMEs -> BB scrambling -> BCH -> LDPC -> fused 8PSK PL mapper writing packed axis codes,
even frames on core 0 and odd frames on core 1, ordered publication, frames retired by a timer
at the on-air cadence. No output path. Normal frames with pilots (22,194 symbols).

## Runs

- `control-8m/`, `target-16m/` (image `116915dd11`, `host/c3_fec_trial.py`): every run stopped
  after the first two frames with "core 0 service gap exceeded 128 us" (largest slices 16.8k to
  21.3k cycles, 131–166 us). `slow-negative/` (5 ms of injected work per frame) missed the
  cadence as required.
- `gap4096.jsonl` (image `9c33ace2a9`, same pipeline plus an opt-in `gap_us` argument that
  relaxes only that fault for CPU-fit measurement): 3 s per code.

| Rate | 8 Msym/s (budget 710,208 cycles per core per frame) | 16 Msym/s (budget 355,104) |
|---|---|---|
| 2/3 (code 5) | 1081/1081 frames on time; core busy 69.0% / 60.9%; max service gap 145 us | first frame late |
| 5/6 (code 8) | 1081/1081; 71.2% / 63.6%; gap 166 us | first frame late |
| 3/4 (code 6) | 1081/1081; 79.5% / 72.1%; gap 198 us | first frame late |

Steady-state work per frame at 8 Msym/s, cycles (core 0 / core 1, averaged over ~540 frames each):

| Rate | GCS + BB | BCH | LDPC | Fused map | Total | vs 16 Msym/s budget |
|---|---:|---:|---:|---:|---:|---:|
| 2/3 | 31k / 30k | 86k / 88k | 157k / 164k | 128k / 133k | 402k / 415k | **113–117%** |
| 5/6 | 38k / 39k | 107k / 109k | 149k / 155k | 128k / 133k | 422k / 436k | **119–123%** |
| 3/4 | 36k / 33k | 136k / 145k | 161k / 175k | 149k / 142k | 482k / 495k | **136–139%** |

## Reading

- At 16 Msym/s every rate needs 13–39% more than two cores have. The first-frame (cold-cache)
  totals at 16 Msym/s were higher still (446k–527k).
- Compared with earlier single-core stage measurements, C3's reentrant LDPC in flash costs
  about 150–175k cycles against about 108–115k for the old pack1 LDPC, and the fused packed-code
  mapper about 128–155k against about 48k for the old signed-plane mapper. With the old LDPC and
  a mapper near 80k, 2/3 would total roughly 300k (about 85%); that is an untested estimate.
- At 8 Msym/s all three rates fit with both cores at 61–80%, but the slices exceed the 128 us
  service gap that the output engines assume, so slicing must be refined before integration.
- This pipeline uses both cores; B6's live producers currently reserve core 1 for grant
  re-arming, so integration also needs a different scheduling arrangement.
