cmake_minimum_required(VERSION 3.13)

set(PICO_BOARD pimoroni_pico_plus2_rp2350 CACHE STRING "")
set(PICO_PLATFORM rp2350-arm-s CACHE STRING "")
include(pico_sdk_import.cmake)
project(iqbench C CXX ASM)
pico_sdk_init()

execute_process(COMMAND git describe --always --dirty --abbrev=10
                WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                OUTPUT_VARIABLE GIT_REV OUTPUT_STRIP_TRAILING_WHITESPACE)

add_executable(iqbench src/main.c src/iqgen.c src/iqout.c src/link.c)
target_compile_definitions(iqbench PRIVATE IQ_ON_DEVICE=1 GIT_REV="${GIT_REV}"
                           PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=1)
target_compile_options(iqbench PRIVATE -O3 -Wall -Wextra -Wno-unused-parameter)
target_link_libraries(iqbench pico_stdlib pico_multicore hardware_pio hardware_dma
                      hardware_clocks)
pico_enable_stdio_usb(iqbench 1)
pico_enable_stdio_uart(iqbench 0)
pico_add_extra_outputs(iqbench)
