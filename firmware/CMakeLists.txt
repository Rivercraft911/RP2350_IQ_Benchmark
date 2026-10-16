cmake_minimum_required(VERSION 3.13)

set(PICO_BOARD pimoroni_pico_plus2_rp2350 CACHE STRING "")
set(PICO_PLATFORM rp2350-arm-s CACHE STRING "")
include(pico_sdk_import.cmake)
project(iqbench C CXX ASM)
pico_sdk_init()

option(IQ4HIST_EXPERIMENT "L7 four-level hardware shaper and aligned borrowed ring" OFF)

set(TX_SOURCES
    src/app/main.c src/app/tx.c src/ingress/link.c src/output/iqout.c src/cpu_shaping/iqgen.c
    src/cpu_shaping/iqasm.S src/output/iqdesc.c src/output/iqdesc_asm.S src/output/iqpair.c
    src/output/iqquad.c src/output/iqquadptr.c src/output/dual_dma.c src/cpu_shaping/iq8.c
    src/cpu_shaping/iq8quad.c src/cpu_shaping/iq8quad_asm.S src/apsk/iq16chunk.c
    src/apsk/iq16chunk_asm.S src/apsk/iq16native8.c src/apsk/iq16native8_asm.S src/apsk/iq16fast_asm.S
    src/apsk/iq16fast_planar_asm.S src/bench/bench16out.c src/bench/bench16planar.c
    src/apsk/iq32native8.c src/apsk/iq32native8_asm.S src/bench/bench32.c src/apsk/iq32keys.c
    src/apsk/iq32keys_asm.S src/apsk/iq32pairkey.c src/apsk/iq32pairkey_asm.S src/bench/bench32out.c
    src/apsk/iq32mapkeys.c src/apsk/iq32mapkeys_pair.c src/apsk/iq16maplabels.c src/apsk/iq32offsets.c
    src/apsk/iq32offsets_asm.S src/apsk/iq32offsets_native.c src/bench/bench_apsk_live.c
    src/bench/bench32map.c src/bench/bench32bus.c src/bench/bench_apsk_fec.c src/dvbs2/dvbs2_apsk.c
    src/dvbs2/dvbs2.c src/dvbs2/dvbs2_8psk.c src/dvbs2/dvbs2_qpsk8.c src/dvbs2/dvbs2_asm.S
    src/dvbs2/dvbs2_bch_fast.S src/dvbs2/dvbs2_ldpc_asm.S src/dvbs2/dvbs2_ldpc_c5_rows.S
    src/dvbs2/dvbs2_ldpc_c5.S src/dvbs2/dvbs2_map_asm.S src/dvbs2/dvbs2_map8_asm.S
    src/ingress/pvproto.c src/ingress/pvproto_asm.S src/ingress/pvspi.c src/bench/bench.c
    src/bench/bench_ddr.c src/bench/bench_dual.c src/bench/bench_ptr.c src/bench/bench8quad.c
    src/bench/bench8live.c src/bench/bench16.c src/bench/bench_pvstress.c src/bench/bench_quadptr.c
    src/bench/bench_quadlive.c)
# Exact placement controls: one original instruction stream, separate code banks.
set(OFFSET_ASM "${CMAKE_CURRENT_SOURCE_DIR}/src/apsk/iq32offsets_asm.S")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${OFFSET_ASM}")
file(READ "${OFFSET_ASM}" OFFSET_SOURCE)
foreach(bank x y)
    string(REPLACE "    .text" "    .section .scratch_${bank}.iq32offsets, \"ax\", %progbits"
           PLACED_SOURCE "${OFFSET_SOURCE}")
    string(REPLACE "iq32offsets_kernel_asm" "iq32offsets_kernel_sram_${bank}"
           PLACED_SOURCE "${PLACED_SOURCE}")
    set(PLACED_ASM "${CMAKE_CURRENT_BINARY_DIR}/iq32offsets_sram_${bank}.S")
    file(WRITE "${PLACED_ASM}" "${PLACED_SOURCE}")
    list(APPEND TX_SOURCES "${PLACED_ASM}")
endforeach()
add_executable(iqbench ${TX_SOURCES})
add_executable(pvflight ${TX_SOURCES})
if(IQ4HIST_EXPERIMENT)
    # Lab-only commands: the autonomous pvflight image has neither them nor their SRAM.
    target_sources(iqbench PRIVATE
        src/output/iq4hist.c src/output/iq4ptr.c src/bench/bench4live.c src/bench/bench4fec.c
        src/output/iq4quad.c src/bench/bench4quad.c src/output/iq5hist.c src/bench/bench5quad.c
        src/output/iq4prod.c src/output/iq4prod_dma.c src/output/iq4prod_source.c
        src/bench/bench4prod.c src/bench/dmaloop.c src/dvbs2/c3_fec.c src/bench/bench_c3_fec.c
        src/bench/bench_c3_pair.c src/output/r2grant.c src/bench/bench_r2grant.c src/bench/bench_b9.c
        src/ingress/pvwide.c)
    target_compile_definitions(iqbench PRIVATE IQ4HIST_EXPERIMENT=1)
endif()
# A3 multiply-form 32APSK conversion: linked last so its SCRATCH_Y section follows
# every existing scratch leaf and none of them moves.
foreach(target iqbench pvflight)
    target_sources(${target} PRIVATE src/apsk/iq32offsets_mla.c)
endforeach()
# git_rev.h is rewritten on every build (only when the revision changes), so logged firmware
# revisions are current. Records made before 2026-09-30 could carry a stale configure-time value.
add_custom_target(git_rev ALL
    COMMAND ${CMAKE_COMMAND} -DSRC=${CMAKE_CURRENT_SOURCE_DIR} -DOUT=${CMAKE_BINARY_DIR}/git_rev.h
            -P ${CMAKE_CURRENT_SOURCE_DIR}/git_rev.cmake
    BYPRODUCTS ${CMAKE_BINARY_DIR}/git_rev.h)
set(IQ_KERNEL_OPT "-O3" CACHE STRING "optimisation flag for the C kernels (iqgen.c), for A/B runs")
set_source_files_properties(src/cpu_shaping/iqgen.c PROPERTIES COMPILE_OPTIONS "${IQ_KERNEL_OPT}")
# Declared before its first use below: a fresh configure must see it (R3 review).
option(CORE0_STACK_SRAM_HI "core 0 stack (4 KiB) at the top of SRAM4-7 instead of above SCRATCH_Y code" ON)
# Sources are grouped by area under src/; headers are included by bare name from any area.
set(SRC_AREAS app output dvbs2 cpu_shaping apsk ingress bench)
list(TRANSFORM SRC_AREAS PREPEND "${CMAKE_CURRENT_SOURCE_DIR}/src/" OUTPUT_VARIABLE SRC_DIRS)
foreach(target iqbench pvflight)
    add_dependencies(${target} git_rev)
    target_include_directories(${target} PRIVATE ${CMAKE_BINARY_DIR} ${SRC_DIRS})
    target_compile_definitions(${target} PRIVATE IQ_ON_DEVICE=1)
    # Lab waveforms share the fixed buffers and never allocate. Keep both2KiB
    # stacks; reserve1KiB minimum heap while retaining the SRAM bank boundary.
    target_compile_definitions(${target} PRIVATE PICO_HEAP_SIZE=1024)
    if(CORE0_STACK_SRAM_HI)
        # Core 1 keeps its 2 KiB SCRATCH_X stack (the SDK would otherwise inherit PICO_STACK_SIZE).
        target_compile_definitions(${target} PRIVATE PICO_STACK_SIZE=0x1000 PICO_CORE1_STACK_SIZE=0x800)
    endif()
    target_compile_options(${target} PRIVATE -O3 -Wall -Wextra -Wno-unused-parameter -Werror=array-bounds)
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
if(IQ4HIST_EXPERIMENT)
    # The idle-only experiment borrows the entire existing output ring as one LUT.
    # Reserve it first, at RAM origin, so PIO can concatenate a 17-bit byte offset.
    string(REPLACE "    .input_ring (NOLOAD) :"
           "    .waveform_ring (NOLOAD) : { *(.bss.ring_buf) } > RAM\n\n    .input_ring (NOLOAD) :"
           LD "${LD}")
endif()
# Scratch-Y's producer state is mutable. Keep it after the immutable code so
# stackcheck can compare every instruction/literal with flash without treating
# a completed experiment's saved results as code corruption. The total end is
# still the stack-paint boundary; no data is painted or omitted from allocation.
string(REPLACE "        *(.scratch_y.*)\n        . = ALIGN(4);\n        __scratch_y_end__ = .;"
       "        *(.scratch_y.*)\n        . = ALIGN(4);\n        __scratch_y_code_end__ = .;\n        *(.scratch_y_data.*)\n        . = ALIGN(4);\n        __scratch_y_end__ = .;"
       LD "${LD}")
if(NOT LD MATCHES "__scratch_y_code_end__")
    message(FATAL_ERROR "SDK scratch-Y section changed; update the stackcheck boundary patch")
endif()
if(CORE0_STACK_SRAM_HI)
    # The default 2 KiB core-0 stack sits directly above SCRATCH_Y code; deep print paths
    # overflow it into that code (results/round2/2026-10-15/stack-1). Move it to RAM_HI.
    string(REPLACE "        KEEP(*(.stack*))\n    } > SCRATCH_Y" "        KEEP(*(.stack*))\n    } > RAM_HI" LD "${LD}")
    string(REPLACE "__StackTop = ORIGIN(SCRATCH_Y) + LENGTH(SCRATCH_Y);"
           "__StackTop = ORIGIN(RAM_HI) + LENGTH(RAM_HI);\n    ASSERT(__StackTop - SIZEOF(.stack_dummy) >= ADDR(.stack_dummy), \"core 0 stack overlaps .sram_hi\")"
           LD "${LD}")
    if(NOT LD MATCHES "> RAM_HI\n\n    .flash_end")
        message(FATAL_ERROR "SDK stack section changed; update the CORE0_STACK_SRAM_HI patch")
    endif()
endif()
if(NOT LD MATCHES "RAM_HI" OR NOT LD MATCHES "sram_hi" OR NOT LD MATCHES "input_ring")
    message(FATAL_ERROR "SDK linker script changed; update the RAM_HI patch")
endif()
file(WRITE ${CMAKE_BINARY_DIR}/memmap_iq.ld "${LD}")
foreach(target iqbench pvflight)
    pico_set_linker_script(${target} ${CMAKE_BINARY_DIR}/memmap_iq.ld)
endforeach()
