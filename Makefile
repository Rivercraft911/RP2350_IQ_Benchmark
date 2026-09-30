# RP2350 I/Q benchmark. Uses the Pico SDK and toolchain installed by the VS Code extension.
PICO_SDK_PATH ?= $(HOME)/.pico-sdk/sdk/2.2.0
PICO_TOOLCHAIN_PATH ?= $(HOME)/.pico-sdk/toolchain/14_2_Rel1
PICOTOOL ?= picotool
BUILD := firmware/build
UF2 := $(BUILD)/iqbench.uf2
export PICO_SDK_PATH PICO_TOOLCHAIN_PATH

.PHONY: build flash bootsel test analyze coeffs bench plots clean

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

analyze:
	python3 reference/analyze.py

coeffs:
	python3 reference/gen_coeffs.py

bench:                 ## run the default sweep on the board and append to the optimization log
	python3 host/iqbench.py sweep

plots:
	python3 host/plot_progress.py

clean:
	rm -rf $(BUILD) host/build
