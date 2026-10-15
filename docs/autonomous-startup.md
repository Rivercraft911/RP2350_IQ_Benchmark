# Autonomous digital startup

The `pvflight` target starts the existing 8 Msym/s transmitter profile without a USB host.
The flight hardware targets the RP2350B chip; the firmware build and bench pin assignments
currently use the Pimoroni Pico Plus 2 development board. Newer opt-in throughput experiments
do not change this startup profile.

Run `make build` from the repository root to produce `firmware/build/iqbench.uf2`, the USB
command bench, and `firmware/build/pvflight.uf2`, the autonomous target.
The stored profile is 128 MHz, 8 Msym/s, normal QPSK 2/3 with pilots, 4 samples/symbol,
L=12, `lut_asm_p`, cpw=2. It runs until stopped and sends TS null packets when input is absent.

External-input startup fills the rings with nulls, starts digital output, then arms reception.
[PV-SPI v1](pv-spi-spec.md) is unchanged. GP22 READY means a free receive slot is armed while
digital output runs; it does not indicate RF readiness. Shutdown withdraws READY before
stopping output. The target adds no AFE, LO or PA configuration or enable; analog operation
remains untested.

During transmission, USB accepts only `status` and `stop` lines. Status is a bounded,
non-atomic counter snapshot, limited to once per second, with one buffered reply and no wait
for the host. Excess requests are ignored, and a USB disconnect can lose a reply. The final
`stream` JSON contains the complete per-run PV-SPI and pipeline counters.

`stop` is checked once per millisecond, exits at a shaping boundary and drains the encoder
frame in progress, normally about 3 ms. These timings still require board verification.
After stop, `start` restarts the stored profile and the existing bench commands are available.
`iqbench` continues to accept `pvtx 2 120000 0`.

`pvflight` disables the 1200-baud USB reset and vendor reset interface. There is no USB BOOTSEL
command during transmission; `bootsel` is available after `stop`. Physical recovery remains
available by holding BOOTSEL while resetting or powering up. `iqbench` retains its existing
USB reset behavior.

The autonomous target has compiled and passed host model/protocol tests. USB-free boot,
disconnect/reconnect and 120 s continuity have **not yet been measured on the board**. To
qualify that behavior, collect `status`, send CM5 SPI traffic for 120 s, then issue `stop` and
check the final counters. The companion sender's state and data flow are documented in the
[PigeonVision architecture](https://github.com/stanford-ssi/PigeonVision/blob/main/software/architecture.drawio).
