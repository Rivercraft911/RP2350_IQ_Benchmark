cmake_minimum_required(VERSION 3.13)

set(PICO_BOARD pimoroni_pico_plus2_rp2350 CACHE STRING "")
set(PICO_PLATFORM rp2350-arm-s CACHE STRING "")
include(pico_sdk_import.cmake)
project(iqbench C CXX ASM)
pico_sdk_init()


set(TX_SOURCES src/main.c src/tx.c src/bench.c src/bench_ddr.c src/bench_dual.c src/bench_ptr.c src/dual_dma.c
               src/iqdesc.c src/iqdesc_asm.S src/iqpair.c src/iqquad.c src/iq8.c src/iqgen.c src/iqasm.S src/iqout.c
               src/link.c src/dvbs2.c src/dvbs2_asm.S src/pvproto.c src/pvspi.c)
add_executable(iqbench ${TX_SOURCES})
add_executable(pvflight ${TX_SOURCES})
# git_rev.h is rewritten on every build (only when the revision changes), so logged firmware
# revisions are current. Records made before 2026-09-30 could carry a stale configure-time value.
add_custom_target(git_rev ALL
    COMMAND ${CMAKE_COMMAND} -DSRC=${CMAKE_CURRENT_SOURCE_DIR} -DOUT=${CMAKE_BINARY_DIR}/git_rev.h
            -P ${CMAKE_CURRENT_SOURCE_DIR}/git_rev.cmake
    BYPRODUCTS ${CMAKE_BINARY_DIR}/git_rev.h)
set(IQ_KERNEL_OPT "-O3" CACHE STRING "optimisation flag for the C kernels (iqgen.c), for A/B runs")
set_source_files_properties(src/iqgen.c PROPERTIES COMPILE_OPTIONS "${IQ_KERNEL_OPT}")
foreach(target iqbench pvflight)
    add_dependencies(${target} git_rev)
    target_include_directories(${target} PRIVATE ${CMAKE_BINARY_DIR})
    target_compile_definitions(${target} PRIVATE IQ_ON_DEVICE=1)
    target_compile_options(${target} PRIVATE -O3 -Wall -Wextra -Wno-unused-parameter)
    target_link_libraries(${target} pico_stdlib pico_multicore hardware_pio hardware_dma
                          hardware_clocks)
    pico_enable_stdio_usb(${target} 1)
    pico_enable_stdio_uart(${target} 0)
    pico_add_extra_outputs(${target})

endforeach()

target_compile_definitions(iqbench PRIVATE PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=1)
# USB monitoring must not reset an active flight transport session.
target_compile_definitions(pvflight PRIVATE PV_AUTONOMOUS=1
    PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=0
    PICO_STDIO_USB_ENABLE_RESET_VIA_VENDOR_INTERFACE=0
    PICO_STDIO_USB_CONNECT_WAIT_TIMEOUT_MS=0 PICO_STDIO_USB_POST_CONNECT_WAIT_DELAY_MS=0
    PICO_STDIO_DEADLOCK_TIMEOUT_MS=0)

# Memory placement: split the striped SRAM into RAM (SRAM0-3: code, DMA ring, input rings) and
# RAM_HI (SRAM4-7: tables, capture) so CPU table loads and DMA traffic use different banks.
# Generated from the SDK default script so SDK updates carry over.
file(READ ${PICO_SDK_PATH}/src/rp2_common/pico_crt0/rp2350/memmap_default.ld LD)
string(REPLACE "RAM(rwx) : ORIGIN =  0x20000000, LENGTH = 512k"
       "RAM(rwx) : ORIGIN =  0x20000000, LENGTH = 256k\n    RAM_HI(rwx) : ORIGIN = 0x20040000, LENGTH = 256k"
       LD "${LD}")
string(REPLACE "    .heap (NOLOAD):"
       "    .sram_hi (NOLOAD) : { *(.sram_hi*) } > RAM_HI\n\n    .heap (NOLOAD):" LD "${LD}")
# Reserve the two 32-KiB-aligned input rings before code. Otherwise a few KiB of extra experimental
# hot code pushes its alignment from 32 to 64 KiB and wastes an entire 32-KiB SRAM interval.
# tx_init explicitly initializes these NOLOAD rings before any consumer is started.
string(REPLACE "    .ram_vector_table (NOLOAD):"
       "    .input_ring (NOLOAD) : { *(.bss.in_buf) *(.bss.link_ring) } > RAM\n\n    .ram_vector_table (NOLOAD):"
       LD "${LD}")
if(NOT LD MATCHES "RAM_HI" OR NOT LD MATCHES "sram_hi" OR NOT LD MATCHES "input_ring")
    message(FATAL_ERROR "SDK linker script changed; update the RAM_HI patch")
endif()
file(WRITE ${CMAKE_BINARY_DIR}/memmap_iq.ld "${LD}")
foreach(target iqbench pvflight)
    pico_set_linker_script(${target} ${CMAKE_BINARY_DIR}/memmap_iq.ld)
endforeach()
