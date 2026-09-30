cmake_minimum_required(VERSION 3.13)

set(PICO_BOARD pimoroni_pico_plus2_rp2350 CACHE STRING "")
set(PICO_PLATFORM rp2350-arm-s CACHE STRING "")
include(pico_sdk_import.cmake)
project(iqbench C CXX ASM)
pico_sdk_init()


add_executable(iqbench src/main.c src/iqgen.c src/iqasm.S src/iqout.c src/link.c src/dvbs2.c
               src/pvproto.c src/pvspi.c)
# git_rev.h is rewritten on every build (only when the revision changes), so logged firmware
# revisions are current. Records made before 2026-09-30 could carry a stale configure-time value.
add_custom_target(git_rev ALL
    COMMAND ${CMAKE_COMMAND} -DSRC=${CMAKE_CURRENT_SOURCE_DIR} -DOUT=${CMAKE_BINARY_DIR}/git_rev.h
            -P ${CMAKE_CURRENT_SOURCE_DIR}/git_rev.cmake
    BYPRODUCTS ${CMAKE_BINARY_DIR}/git_rev.h)
add_dependencies(iqbench git_rev)
target_include_directories(iqbench PRIVATE ${CMAKE_BINARY_DIR})
target_compile_definitions(iqbench PRIVATE IQ_ON_DEVICE=1
                           PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=1)
target_compile_options(iqbench PRIVATE -O3 -Wall -Wextra -Wno-unused-parameter)
set(IQ_KERNEL_OPT "-O3" CACHE STRING "optimisation flag for the C kernels (iqgen.c), for A/B runs")
set_source_files_properties(src/iqgen.c PROPERTIES COMPILE_OPTIONS "${IQ_KERNEL_OPT}")
target_link_libraries(iqbench pico_stdlib pico_multicore hardware_pio hardware_dma
                      hardware_clocks)
pico_enable_stdio_usb(iqbench 1)
pico_enable_stdio_uart(iqbench 0)
pico_add_extra_outputs(iqbench)

# Memory placement: split the striped SRAM into RAM (SRAM0-3: code, DMA ring, input rings) and
# RAM_HI (SRAM4-7: tables, capture) so CPU table loads and DMA traffic use different banks.
# Generated from the SDK default script so SDK updates carry over.
file(READ ${PICO_SDK_PATH}/src/rp2_common/pico_crt0/rp2350/memmap_default.ld LD)
string(REPLACE "RAM(rwx) : ORIGIN =  0x20000000, LENGTH = 512k"
       "RAM(rwx) : ORIGIN =  0x20000000, LENGTH = 256k\n    RAM_HI(rwx) : ORIGIN = 0x20040000, LENGTH = 256k"
       LD "${LD}")
string(REPLACE "    .heap (NOLOAD):"
       "    .sram_hi (NOLOAD) : { *(.sram_hi*) } > RAM_HI\n\n    .heap (NOLOAD):" LD "${LD}")
if(NOT LD MATCHES "RAM_HI" OR NOT LD MATCHES "sram_hi")
    message(FATAL_ERROR "SDK linker script changed; update the RAM_HI patch")
endif()
file(WRITE ${CMAKE_BINARY_DIR}/memmap_iq.ld "${LD}")
pico_set_linker_script(iqbench ${CMAKE_BINARY_DIR}/memmap_iq.ld)
