# 8PSK full-pipeline SRAM contention review

2026-10-15. Read-only source, ELF, recorded-device and primary-datasheet review. No shared build, USB access, or repository mutation by this investigator. The sibling controls described below are unbuilt.

## Finding

The explanation that the first full 8PSK image suffered **two cores fetching hot instructions from the same single-port scratch SRAM bank** is strongly supported by the physical bank map, linked addresses, and the large recovery after BCH relocation. It is more precise to say bank arbitration, rather than a generic lack of SRAM bandwidth. Independent core instruction/data interfaces do not give each core a private port into every bank.

The passing image now demonstrates complete standalone normal 5/6 8PSK with pilots, Gold0, a coherent π/8 rotation of the whole PLFRAME, L9/2SPS shaping, on-chip GCS/PV-SPI ingress, all FEC and PL mapping, sample production and actual output DMA/PIO. Both 128MHz/8Msym/s and 150MHz/9.375Msym/s passed three seconds, including all 8192 captured halfwords at one finite contiguous waveform offset. The latter offers 22.708840Mb/s of BB datafield capacity in nominal 11.25MHz α=.2 width. These are digital bench results. No AFE, RF, external-CM5 ingress, arbitrary TS fallback at this mode, or seamless mode switch has been qualified by this trial.

## Hardware mechanism

[Raspberry Pi's RP2350 datasheet](https://pip-assets.raspberrypi.com/categories/1214-rp2350/documents/RP-008373-DS-2-rp2350-datasheet.pdf?disposition=inline), §§2.1.1,3.7.2.1.1,3.7.4.9.12,4.2, explains the relevant constraints. Each SRAM bank has an independent downstream arbiter and accepts one access per system clock. Equal-priority requests for the same bank are round-robin arbitrated; accesses to distinct banks proceed in parallel. The low 256KiB is striped word-by-word over banks0–3; the high 256KiB is striped over banks4–7. The 4KiB ranges starting 0x20080000 and 0x20081000 directly select banks8 and9. They become contention-free per-core scratch only when software actually keeps other managers out.

RP2350's UNCROSS_I_D modification always routes instruction fetch through the core Code AHB interface and load/store through its System interface. This removes a core-internal routing conflict but leaves conflicts at the SRAM bank's downstream port. The core's instruction prefetch also cannot absorb an indefinitely oversubscribed bank; sequential prefetch is still limited by its bank grants.

In the failing ELF, `iq8quad_kernel_interp_asm` occupies 456 bytes at0x20081370. The 248-byte `dvbs2_bch160_stream` starts0x20081538, followed by its12-byte continuation at0x20081630. Both hot instruction streams select bank9. The core0 stack also occupies0x20081800..0x20081fff in that bank. Core1 performs the full BB+BCH stage while core0 continuously executes the shaper, so the two fetch streams overlap for a substantial part of each frame. There is no instruction cache on this SRAM path that makes the second stream free.

In the passing ELF, the same BCH functions start0x20018244 and0x2001833c in striped banks0–3; the shaper remains in bank9 at0x20081368. Shaper A/B table reads also use banks0–3 (`ring_buf`), whereas output samples/capture (`cap_buf`) and the BCH lookup table use banks4–7. This is not perfect conflict elimination: BCH instruction fetch can now overlap shaper table reads in banks0–3. It does remove the continuous two-core fetch collision from a single bank and spreads those remaining conflicts across four physical arbiters. Core0's stack remains bank9, so short local instruction/data collisions can still occur.

This is why raising one core's bus priority would be an inferior operational fix: it redistributes same-bank stalls and may make the other deadline fail. Bank separation addresses the resource conflict itself. The two-core whole-pipeline schedule, rather than a universal association of “BCH belongs in scratchY,” determines placement.

## Recorded evidence and limits

Historical comparison below is **failing / passing**; BB+BCH is the `dvbs2_frame_begin` stage, excluding the separately timed GCS BB construction. Instrumented work fractions are not OS idle or sleep.

|Clock MHz|Elapsed ms|BB+BCH cycles/frame|Shaper cycles/symbol|Core1 work|Passing capture|
|---|---:|---:|---:|---:|---:|
|128|22.080 / 3000.062|176,373.12 / 121,469.64|13.3477 / 12.0803|99.96% / 92.11%|8192, 0 errors|
|150|18.828 / 3000.016|176,188.25 / 121,503.30|13.3485 / 12.0995|99.96% / 92.12%|8192, 0 errors|

The failing records stop after only eight runtime frames: `quick-1` has one input underrun and one idle insertion at both clocks, with no midpoint capture. The passing `bch-placement-quick-2` records run1082/1268 runtime frames with zero loss, underrun, idle insertion, ownership fault or TX stall; minimum available input is1089 complete16-symbol groups. The first four BB CRCs, two-frame unflushed mapper-prefill CRC, and both shaper table CRCs all match the independent native models. The shaper's reported work is75.50%/75.62%, while core1 is92.11%/92.12%.

The BB+BCH average falls about31%, while shaping falls9.5% at128MHz and9.4% at150MHz. LDPC remains near128.55kcycles/frame and mapping near49.5k. These changes have the expected direction and scale for removing overlapping instruction-fetch contention. Separate component timing predicted roughly92% core1 work once normal mapping cost was reduced from95,675 to47,952cycles/frame; the recovered full pipeline is consistent with that independent budget.

Do not call the historical comparison a perfectly isolated one-line causal experiment. The two frozen source trees also differ in the following files:

- `src/bench/bench.h`
- `src/bench/bench_dual.c`
- `src/dvbs2/dvbs2_asm.S`
- `src/output/iqdesc.c`
- `src/output/iqdesc.h`
- `src/output/iqquad.c`
- `src/output/iqquad.h`
- `src/output/iqquadptr.c`
- `src/output/iqquadptr.h`
- `src/app/main.c`

There are nine changed files besides the BCH section file. The first placement trial with correct digital outputs but the wrong expected-git argument is retained under `bch-placement-quick-1`; use the identity-correct `quick-2` for qualification. The measured recovery is real; exact attribution of every cycle to placement alone remains a controlled-test question. The failing observation's short run also overweights startup frame work; compare matched short windows or steady-state complete windows for numerical attribution.

## Frozen one-line controls

`/tmp/yapogee-bch-placement-control/ramlow/firmware` is byte-identical to the exact passing frozen source. `scratchy/firmware` differs only by replacing `.time_critical.dvbs2_bch160_stream` with `.scratch_y.dvbs2_bch160_stream`. All56 firmware source/configuration files are individually SHA256-bound in `manifest.json`; the prior ELF/UF2 and raw run logs are bound too. Complete diff: `placement-only.patch`. Manifest SHA256: `7f8cb41878f68e87ebc62c2a7a96296e6fae0ee63039dbaa2bc2c784987a2e06`.

Neither control has been built or flashed. Both use identical source comments, even where a comment therefore becomes stale in scratchY. Build in distinct directories with the same SDK/toolchain and flags. Their unchanged git-header generator defaults to `unknown` outside Git; preserve an identical identity override in both controls if desired and record it. An old expected identity must not be used to validate a newly generated `unknown` image. `README.md` records this limitation and the original identity.

A useful A/B/A experiment is RAMlow3s → scratchY3s → RAMlow3s at each clock. Before flashing confirm both maps, unchanged table/prefill fixture CRCs, and unchanged instruction content except addresses/relocations; moving a section legitimately shifts some linked addresses. No operational success is expected from the deliberately contended control. Preserve its early underrun. Strong causal support is a scratchY BB+BCH rise≥20% or shaper rise≥5%, followed by RAMlow returning within3% of its first timing. The full operational passing image should additionally complete60s at both clocks. This control need not delay that soak.

Optional matched counter images can select bank9 ACCESS_CONTESTED/ACCESS plus STALL_UPSTREAM/STALL_DOWNSTREAM. SDK2.2.0 `busctrl.h` event values are0x12/0x13/0x10/0x11 respectively. Count all bank traffic, including stack; do not label these instruction-only counters. Compare equal-duration windows, normalize appropriately, and check overflow. Counter instrumentation is a separate pair of controls, not required to accept the existing three-second digital pass.

## Practical consequence

The full 8PSK5/6 mode is no longer blocked by the old 37.37-cycle prototype or by the first failed integrated run. Four-symbol rows, two-SPS shaping, direct interleaver/Gold mapping, cheaper GCS ingress and appropriate dual-core memory placement jointly make it plausible as a readable selectable mode. A60s digital soak, resource/stack accounting, and then electrical/RF qualification establish the next boundaries. The128MHz mode saves half the nominal symbol bandwidth compared with16Msym QPSK; the150MHz mode also exceeds the qualified QPSK2/3 BB capacity by about10%, at a separately stated clock and higher-SNR code/modulation trade.
