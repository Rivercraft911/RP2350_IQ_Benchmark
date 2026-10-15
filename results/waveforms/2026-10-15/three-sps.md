# 3 SPS / four-symbol rows: future experiment sanity check

Read-only research, 15 October 2026. No firmware changes, build, USB access or new performance qualification. Current supported waveform paths remain 2 and4 SPS.

**Verdict: technically credible as a bounded digital component experiment, but low priority as the next AFE7071 throughput milestone.** The proposed21.333Msym/s rate needs25.6MHz nominal RF bandwidth, beyond the AFE7071's stated20MHz. It also exceeds the present encoder partition unless BB/GCS work moves off Core1. A six-word DMA burst alone does not produce a complete faster radio.

## Arithmetic, not measurements

At128MHz, one interleaved16-bit slot per system cycle and3 samples/symbol give Rs=128MHz/(2×3)=21.333333Msym/s and64MS/s per axis. Normal QPSK with pilots occupies33,282 symbols/frame. BB data excludes the80-bit BBHEADER:

| Normal code | Kbch | BB data at21.333Msym/s | Relative to current16Msym/s5/6 |
|---|---:|---:|---:|
|2/3|43,040|27.536807Mb/s|+6.5476%|
|3/4|48,408|30.977626Mb/s|+19.8611%|
|5/6|53,840|34.459468Mb/s|+33.3333%; exceeds32MHz SPI input|

Current reference is25.844601Mb/s. Wider RF occupancy is a cost:25.6MHz versus19.2MHz at16Msym/s, both alpha0.20. The2/3 proposal improves absolute payload but loses payload per nominal hertz relative to5/6. It buys stronger coding and more bandwidth, not greater spectral efficiency.

PV-SPI carries1,316 payload bytes per1,332-byte message. Its wire-only32MHz payload ceiling is31.615616Mb/s. The actual on-chip source adds1,024 gap cycles and message-control instructions:43,654 cycles/message at128MHz, yielding at most30.869657Mb/s before READY waiting. Therefore the proposed3/4 rate already exceeds the present self-test source ceiling by0.350%; a source-gap/clock change or a slightly lower symbol rate is required even if encoding were free. An external sender with a shorter qualified gap is a separate qualification.2/3 has roughly10.8% headroom below the on-chip source ceiling.

## Backend: small two-writer change, meaningful integration work

`dual_dma.c` already sends contiguous32-bit words using TRANS_COUNT and chains back through AL3_READ_ADDR_TRIG. The two-writer PIO is only OUT16/NOP with optional IQ side-set; it has no row-length loop. Six-word transfers are supported by the silicon, but rejected by current `dual_dma_set_row_words` and FIFO-start validation, whose FIFO path hard-codes eight. Generalizing those explicit checks is plausible.

ForL8 and four-symbol rows there are2^(8+3)=2,048 rows. Store12 chronological axis samples in the first24 bytes of each32-byte row, leaving8 bytes unused:64KiB shared table,64KiB alignment, key<<5, same eleven-bit pointer-key permutation. The existing49-cycle raw16-symbol pointer program can remain unchanged: its unbackpressured ceiling is41.796Msym/s at128MHz, almost twice the proposed rate. No input repacking, new arithmetic peripheral or extra buffer is needed.

The builder must flatten all12 samples before pairing them into six words. Three samples per symbol means word boundaries cross alternate symbol boundaries; the current symbol-local `sps/2` loop cannot simply become3/2. `iqlut.Lut.words()` also assumes even SPS and needs a separate quad packing reference. The frozenL8N4 coefficient array is not anL8N3 profile: design and qualify24 new coefficients, new direct/permuted table CRCs, all2,048 keys, raw-word/frame boundaries, and exact sparse captures.

At the same64MS/s axis output, sample DMA traffic remains64million32-bit table reads and64million PIO writes per second across both axes. Axis rows rise4.0→5.333million/s; total pointer/control words rise8.0→10.667million/s. Control read/trigger traffic rises33%, while sample traffic stays fixed. Joined output FIFO depth remains eight words; therefore a six-word burst does **not** automatically cut the FIFO's time cushion by25%. More frequent handoffs and arbitration are the concrete risk; only a stressed Pico run settles it.

Keep two writers first. Existing four-writer pause programs encode8- or16-axis-sample batches and16/32 silent slots, so six-word rows require a real12-sample/24-silent-slot schedule and new phase proof. The FIFO API currently supports only two writers. A four-writer fallback adds resources and complexity; it is not an immediate configuration switch. With two writers, the current pointer+output+capture+SPI self-test resource envelope remains about8 SMs/12 DMA channels, not a new allocation problem. Existing credit and physical-lookahead proofs must be rechecked;49-cycle source-issue and finite-credit bounds remain applicable, but do not assume an old physical sample-cursor formula understands3 SPS.

## Encoding:2/3 can plausibly fit after ownership changes;3/4 does not fit the current split

A frame now has199,692 Core1 cycles at128MHz. CRC-qualified component rows in `results/higher-mapping/2026-10-15/fast-1/runs.jsonl`, lines8/10/12, measured the following on the same image (map_kernel1, pilots on):

| Code | BB+BCH cycles | LDPC+QPSK-map cycles | Projected Core1 LDPC/map fraction |
|---|---:|---:|---:|
|2/3|91,911|177,573|88.92%|
|3/4|139,288|214,179|107.25%|
|5/6|113,952|196,805|98.55%|

These are isolated component measurements projected onto a new frame budget, not a runtime forecast with established margins. In the existing live2/3 TS60s record (`qpsk-codes/.../soak-5-6-map1-1/runs.jsonl:1`), Core1 LDPC/map is68.37% and TS18.25% at16Msym/s. Scaling both by4/3 gives115.49%. GCS removes per-packet TS processing but the current `split_backend` still calls `refill_bb_queue()` on Core1. Keeping that ownership probably remains too tight: measured2/3 live LDPC/map alone scales to91.16%.

The useful architecture change is to make Core0 own GCS adaptation **and** its existing BB scrambling/BCH, releasing complete FE slots to Core1 for LDPC/map. It removes a BB cross-core queue and places the SPI packet borrower with the frontend. New PIO addressing frees ample Core0 capacity. A rough2/3 projection using measured5/6 GCS byte cost scaled by payload length gives Core0 ~46% frontend +13% GCS +3–4% raw-PIO supervision, while Core1 is ~89–92% LDPC/map. Bus contention, IRQ latency and worst frame time must still be measured.3/4 needs at least another14,487 Core1 cycles/frame removed or moved even before supervision/input overhead; its BCH192 path is also more expensive than2/3 BCH160. Do not infer that a higher code rate is always cheaper.

## Analog gate: rate compliance does not establish bandwidth compliance

[TI's AFE7071 datasheet](https://www.ti.com/lit/ds/symlink/afe7071.pdf?ts=1779477858460), features/p1 and AC table/p6, specifies65MSPS maximum DAC rate,20MHz maximum RF signal bandwidth and a programmable fourth-order baseband filter. Its widest setting has typical1dB attenuation at10MHz and18dB at20MHz baseband. The proposed RRC reaches12.8MHz on each side. The repo's explicitly approximate log-frequency typical-filter model predicts about7.05dB attenuation there; this is screening evidence, not a guaranteed device response. Pre-equalization could trade headroom and sensitivity for flatness, but does not turn this into a datasheet-qualified25.6MHz radio.

DAC-hold edge droop grows from0.324dB at4 SPS to0.579dB at3 SPS (alpha0.20). At fixed64MS/s DAC rate the nearest image edge moves54.4→51.2MHz; at fixed symbol rate, reducing4→3 SPS moves the image much closer. [ETSI EN302307-1 AnnexA](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf) explicitly requires rectangular-input x/sin(x) correction. Qualify the new pulse densely with the hold, integer rounding, headroom, matched-receiver EVM/ISI and far-stop/image constraints; then measure the actual AFE amplitude/phase and RF spectrum. Copying the N4 pulse, checking Nyquist alone or calling64<65 an RF pass is insufficient.

At20MHz nominal RF width and alpha0.20, Rs≤16.666667Msym/s. Keeping5/6 then yields at most26.921459Mb/s, only4.17% above the current qualified digital point: below the requested5% material-gain bar. A lower-rate3SPS mode may reduce bus traffic25% at fixed Rs, but worsens image separation and is not a demonstrated throughput improvement. Existing supported divider1.5 gives14.222Msym/s at128MHz or16.6667 at150MHz for3 SPS. The latter uses an odd HSTX clock divider and unequal integer-cycle phases; AFE clock-duty/setup/hold qualification is required. Arbitrary same-Rs3SPS rates need fractional PIO timing and an HSTX clock plan that the present integer-divider setup does not supply.150MHz with3SPS at divider1 instead gives75MS/s/axis, above65MSPS.

## Ranked next action and stopping criteria

1. **First integrate the already proven PIO-address engine with the existing4SPS full encoder/SPI path**, retaining its immutable-data credit gate and fail-closed cleanup. Move GCS/BB ownership to Core0 if needed. This preserves the main analog assumptions and tests the architecture that3SPS would require anyway.
2. **If a wider analog frontend is a real platform target, run a contained3SPS digital component experiment.** Native all-key/word-boundary/reference checks first; then two-writer6-word PRBS at128MHz,60s, zero stall/error flags, all8,192 captured observations exact and finite-credit starvation detected. Repeat with simultaneous SPI and both encoder cores to expose arbitration. Fail if six-word handoffs cannot sustain the clock without a substantially more complex output backend; retain4SPS.
3. **Attempt full2/3 GCS only after Core1 worst-frame time and ring runway pass**, targeting27.5368Mb/s with complete BB/packet CRC chains and exact frame-boundary captures. Label it digital/wideband research until a new frontend or explicit AFE characterization supports25.6MHz. Defer3/4: current encoder partition and self-test input are both over budget.

Reproducible arithmetic: `/tmp/yapogee-three-sps-arithmetic.json`. [RP2350 datasheet §12.6.2.2](https://pip-assets.raspberrypi.com/categories/1214-rp2350/documents/RP-008373-DS-2-rp2350-datasheet.pdf?disposition=inline) confirms arbitrary transfer counts/reload semantics; no programmable DMA arithmetic is assumed. All throughput/CPU numbers above labeled projected are calculations, not new measurements.
