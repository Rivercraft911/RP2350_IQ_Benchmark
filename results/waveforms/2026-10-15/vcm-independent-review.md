# Continuous QPSK/8PSK VCM independent review

2026-10-15. Read-only firmware/standards review, followed by parent-authorized changes only to host/vcm_trial.py and a narrow common runtime-check extraction in host/iq8live_trial.py. No shared build, USB or firmware edits. Saved raw runs, captures, manifests and binaries remain unchanged; new revalidation.json files are additive.

## Result

No waveform, frame-geometry, source-lifetime or queue-ownership defect was found in this fixed alternating normal5/6 experiment. The original VCM host runner had concrete false-pass gaps for identity, CPU accounting, capture progress and BBFRAME counts. They are now fixed and covered by negative controls. Both original3s and60s records at128MHz and150MHz pass the stricter offline validator with expected identity f4a2ea4a7c-dirty and all8192 captured halfwords exact, including a mode boundary and the complete next90-symbol PLHEADER.

This is continuous matched-pulse QPSK/8PSK digital VCM on one clock. It does not qualify a receiver's live demodulator transition, analog/RF performance, external-source operation or transitions to native APSK/different pulse/clock profiles.

## Standards and waveform

[ETSI EN302307-1 V1.4.1](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf) §5.1.6/Table3 assigns the ACM signalling value to VCM. MATYPE0x62 correctly selects generic continuous input, single stream, VCM/ACM flag0, inactiveISSY/NPD and rolloff.20. UPL0 and privateSYNC0xb9 are consistent. §4.1 permits modulation/FEC changes between frames; §5.5 separates PLHEADER from Gold-scrambled data/pilots. The two mapper configurations advertise QPSK5/6 MODCOD9 and8PSK5/6 MODCOD15 with normal-frame/pilotTYPE1. This preprogrammed VCM schedule requires no return channel.

The constant whole-streamπ/8 rotation is coherent across both modes. Let native unit-QPSK coordinates be(x,y)=(sI,sQ)/√2. Existing plane weights satisfyA=cos(π/8)/√2 andB=sin(π/8)/√2. Therefore A(sI,sQ)+B(−sQ,sI)=exp(jπ/8)(x+jy), exactly. Bit signs becomeA=w andB=(~Q,I); the new append routine implements that identity for complete16-symbol words and the partial tail. The existing QPSK mapper builds its actual90-symbol MODCOD9 header and Gold/pilots before the transform. The8PSK mapper separately generates MODCOD15. No mapper flush or shaper-history reset occurs between physical frames, so neither a phase step nor a zero-padded RRC seam is introduced.

Both modes consume the same53840-bit BBFRAME and64800-bit FECFRAME at5/6. QPSK has32400data+792pilot+90header=33282symbols.8PSK has21600data+504pilot+90header=22194. The alternating period is55476symbols for two53760-bit BB datafields. At8/9.375Msym/s, aggregate BB capacity is15.505083/18.1700195Mb/s; it is not the pure8PSK22.708840Mb/s rate.

## Memory, publication and source lifetime

- `live.frame_groups=ceil(33282/16)=2081` reserves the largest mode before encode. Given any incoming fill0..15, QPSK can append at most2081 complete groups. The producer's additional+1 reservation and the consumer's retained previous group prevent overwriting the FIR history. Consumer input blocks are64groups and the ring is4096groups; block boundaries cannot straddle unhandled chunks because4096 is divisible by64.
- `dvbs2_8psk_arena_words` returns1384words for normal pilots. Gold occupies[0,1384), QPSK scratch[1384,5480), below the8192-word cfg.ti arena. The QPSK scratch stream starts empty each frame and writes2080 full words plus a two-symbol accumulator, below4096. FE storage, shared output stream and the A/B shaping tables are separate allocations.
- `pv_gcs_bbframe` preserves `cur_off` across alternating frames. The borrowed188-byte source packet remains owned until the next packet callback. `pvspi_next_packet` only releases a completed slot on that next request, so time spent inFEC or either mapper does not release a partial packet. BB bytes are consumed in order, then byte-swapped once for the FE encoder. No per-mode source reset occurs.
- Existing release/acquire DMBs protect complete-frame publication, input sample loads and history retirement. All configuration and Gold/header tables are initialized before the worker starts and remain constant throughout the bench. Per-core fields consumed while active are volatile publication/control fields; aggregate mode/frame statistics are read only after the core handshake.
- The native actual-encode_frame test reran successfully:512blocks,524288symbols,1048576raw32bit words,eight source-ring wraps in each mode. Fixed8 rawCRC3539388180 remains unchanged; VCM rawCRC355845684. It also hashes live unread groups around each producer call. VCM prefill is3467groups,fill4,CRC215158474; BBCRCs2430356946,1153874154,1617543358,3202956426 match independent scalar references.

## Capture boundary math

`frame_start(i)` is the exact prefix sum of alternating frame lengths; `frame_index(s)` is its containing-frame inverse. The firmware selects the strict next boundary after the integer midpoint, advancing again if boundary%1024>768. Host validation independently recomputes that exact target. The common runtime checks tie cursor alignment, done-block lead, source publication and capture time to that target.

The callback supplies a finite sample window with a real previous frame for FIR history. Only the common helper's old fixed-frame *global-offset label* is overridden: actual search/reference indices already use the callback's correct variable-length origin. A global bus offset divided by4 gives exact quarter-symbol positions at these run lengths. The next-boundary check uses floor(start), so captures starting on Q or the second sample remain correctly classified. Every8192-halfword slice is checked at one offset; no wrap, per-frame padding or independently chosen phase is accepted.

|Record|Clock MHz|Seconds|Symbols captured before boundary|Symbols after boundary|Checked halfwords / errors|
|---|---:|---:|---:|---:|---:|
|quick-1|128|3|1551.50|496.50|8192 / 0|
|quick-1|150|3|1564.00|484.00|8192 / 0|
|soak-1|128|60|1003.75|1044.25|8192 / 0|
|soak-1|150|60|1115.50|932.50|8192 / 0|

All four windows extend well past the next90-symbol header and theL9 FIR transient. Independent interval-edge checks cover4096frame intervals and their start/start+1/end−1 points; the strict midpoint selector is checked at both clocks and1000/3000/60000ms. Original and hardened VCM selftests cover both transition directions, corruption, false boundary labels and unflushed prefill. The reference's artificial initial history is excluded by at least one prior full frame for these captures.

## Concrete host defects and repairs

Original `faults()` returned an empty list for an otherwise valid quick record mutated to each of:

1. git='unrelated-image';
2. encoder_busy/gen_busy=NaN;
3. empty stage totals/maxima;
4. cap_done_blocks=cap_input_groups=0;
5. one extra reportedBBFRAME with self-consistent GCSbyte/container totals but unchanged encoder frame count.

The original reproduction is `/tmp/yapogee-vcm-review-negative-controls.json`. `base.runtime_faults()` now owns the mode-independent checks: exact requested image identity, integer monotonic counters, finite positive work fractions, stage-key/type/total/max/budget consistency, consumed/history/lead bounds, capture block/time/done/publication bounds, SPI clock, BB↔encoder counts and independent packet/CRC-chain checks. The caller retains its own fixed8 or alternating-mode prefix arithmetic; VCM passesccm=False and its exact boundary target.

The VCM driver also adds --expected-git/--port; checks BCH/map selector and clock replies before running; hashes all transitive host/reference Python helpers plus firmware source/CMake/git-generator/import files; preserves failures and cleanup errors in a summary. Four coherent synthetic clock/duration profiles reject17metadata mutations each, plus nine selector/config-error cases. Fixed8 selftest also still passes after the narrow extraction.

## Additive revalidation and provenance

- `results/vcm/2026-10-15/quick-1/revalidation.json`: passed=true for both3s runs.
- `results/vcm/2026-10-15/soak-1/revalidation.json`: passed=true for both60s runs.

Both record expected_firmware_git=f4a2ea4a7c-dirty, fresh hashes of the unchanged runlog/capture/manifest/summary files, original copied image hashes, current validator/model hashes and frozen source/CMake hashes. Existing manifest-bound source hashes are crosschecked. Revalidation requires a completed summary and the complete requested clock set, refuses an existing revalidation destination, and never opens a serial session.

Original UF2 SHA256: `966e0f8f47f0d5dd513eb11fa0ea57bc6ecb54901dd29ea70366fbe47fb1c40e`.
Original ELF SHA256: `f9922175126e5dc47018d1044fee1dac2045e1481edae9334366b6e3449b677b`.

Reproduce only into a fresh additive destination by preserving the original report first, or call the pure checks against the raw records. The original files were not rewritten to retrospectively improve their metadata or provenance.
