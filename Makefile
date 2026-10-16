# RP2350 I/Q benchmark. Uses the Pico SDK and toolchain installed by the VS Code extension.
PICO_SDK_PATH ?= $(HOME)/.pico-sdk/sdk/2.2.0
PICO_TOOLCHAIN_PATH ?= $(HOME)/.pico-sdk/toolchain/14_2_Rel1
PICOTOOL ?= picotool
BUILD := firmware/build
UF2 := $(BUILD)/iqbench.uf2
export PICO_SDK_PATH PICO_TOOLCHAIN_PATH

.PHONY: build flash bootsel test check analyze coeffs bench smoke plots clean

build: $(BUILD)/build.ninja
	ninja -C $(BUILD)

$(BUILD)/build.ninja: firmware/CMakeLists.txt
	cmake -S firmware -B $(BUILD) -G Ninja -DCMAKE_BUILD_TYPE=Release

flash: build           ## load and run; works from BOOTSEL or from running iqbench firmware
	$(PICOTOOL) load -v -x $(UF2) -f

bootsel:
	$(PICOTOOL) reboot -f -u

test:                  ## host-native kernels vs Python model, bit-exact
	python3 host/test_native.py

check: test            ## encoder, protocol, PIO/DMA models and result validation; no USB
	python3 host/test_dvbs2_native.py
	python3 host/test_dvbs2_8psk.py
	python3 host/test_dvbs2_8psk_codes.py
	python3 host/test_c4_mapper_pic.py
	python3 host/test_dvbs2_parallel.py
	python3 host/test_c3_pipeline.py
	python3 host/test_c3_bench.py
	python3 host/test_c3_fec_trial.py
	python3 host/c3_fec_trial.py --self-test
	python3 host/test_r2grant.py
	python3 host/test_bench_r2grant.py
	python3 host/r2_grant_trial.py --self-test
	python3 host/test_r2_hazard3.py
	python3 host/r2_hazard3_trial.py --self-test
	python3 host/test_dvbs2_qpsk8.py
	python3 host/test_dvbs2_apsk.py
	python3 host/test_dvbs2_map.py
	python3 host/test_dvbs2_buffers.py
	python3 host/test_frame_order.py
	python3 host/test_dvbs2_fec.py
	python3 host/test_bch128_asm.py
	python3 host/test_ldpc_asm.py
	python3 host/test_pv_native.py
	python3 host/test_gcs_native.py
	python3 host/test_pvspi_rx.py
	python3 host/test_startup.py
	python3 host/test_flight_service.py
	python3 host/test_iqdesc.py
	python3 host/test_iqpair.py
	python3 host/test_iqquad.py
	python3 host/test_iq8.py
	python3 host/test_iq8quad.py
	python3 host/test_iq16chunk.py
	python3 host/test_iq16chunk_asm.py
	python3 host/test_iq16native8.py
	python3 host/test_iq16fast.py
	python3 host/test_iq16fast_planar.py
	python3 host/test_iq32native8.py
	python3 host/test_iq32native8_asm.py
	python3 host/test_iq32pairkey.py
	python3 host/test_iq32keys.py
	python3 host/test_iq32mix.py
	python3 host/test_iq32mapkeys.py
	python3 host/test_iq32mapkeys_pair.py
	python3 host/test_iq32offsets.py
	python3 host/test_iq32offsets_native.py
	python3 host/test_apsk_live.py
	python3 host/test_apsk_fec_native.py
	python3 host/test_apsk_split_native.py
	python3 host/test_apsk_fast_pic.py
	python3 host/apsk_live_trial.py --self-test
	python3 host/apsk_fec_trial.py --self-test
	python3 host/test_iq16out.py
	python3 host/iq16out_trial.py --self-test
	python3 host/test_iq16planar.py
	python3 host/iq16planar_trial.py --self-test
	python3 host/test_iq32out.py
	python3 host/iq32out_trial.py --self-test
	python3 host/test_8live_native.py
	python3 host/iq8live_trial.py --self-test
	python3 host/vcm_trial.py --self-test
	python3 host/test_iqquadptr.py
	python3 host/test_iq4hist.py
	python3 host/test_iq4hist8.py
	python3 host/test_iq4ptr.py
	python3 host/test_iq4ptr8.py
	python3 host/test_iq4writer8.py
	python3 host/test_iq4quad.py
	python3 host/test_quad_rows.py
	python3 host/test_bench4quad.py
	python3 host/test_iq4quad_trial.py
	python3 host/test_iq4prod.py
	python3 host/test_iq4prod_source.py
	python3 host/test_iq4prod_trial.py
	python3 host/test_bench4prod.py
	python3 host/test_iq4hist_trial.py
	python3 host/test_quadptr_trial.py
	python3 host/test_quadlive_trial.py
	python3 host/test_dualptr_native.py
	python3 host/test_dualptr_trial.py
	python3 host/test_iqout_raw.py
	python3 host/test_iqout_odd.py
	python3 host/test_dual_pio.py
	python3 host/test_quad_fifo_order.py
	python3 host/test_dacclk.py
	python3 host/test_dual_faults.py
	python3 host/test_dual_fifo.py
	python3 host/test_dual_spi.py
	python3 host/test_dual_codes.py
	python3 host/test_ptrprog.py
	python3 host/dual_trial.py --self-test
	python3 host/plot_dual.py --self-test
	python3 host/plot_waveforms.py --self-test
	python3 host/plot_apsk_frontier.py --self-test
	python3 host/plot_apsk_dual.py --self-test
	python3 host/plot_apsk_fec.py --self-test
	python3 host/cm5/test_analyze_live.py
	python3 host/cm5/test_live_remote.py
	python3 host/check_links.py

analyze:               ## filter sweep and samples/symbol figures
	python3 reference/analyze.py
	python3 reference/analyze_sps.py

coeffs:
	python3 reference/gen_coeffs.py

bench:                 ## run the default sweep on the board and append to the optimization log
	python3 host/iqbench.py sweep

smoke:                 ## every command once on the board, briefly; not logged
	python3 host/iqbench.py smoke

plots:
	python3 host/plot_progress.py
	python3 host/plot_waveforms.py
	python3 host/plot_apsk_frontier.py
	python3 host/plot_apsk_dual.py
	python3 host/plot_apsk_fec.py

clean:
	rm -rf $(BUILD) host/build
