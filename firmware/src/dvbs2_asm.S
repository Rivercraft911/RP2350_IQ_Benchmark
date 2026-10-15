// Arm leaves for the DVB-S2 encoder; portable fallbacks remain in dvbs2.c.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb

// Five-word BCH remainder. r0=input, r1=parity, r2=table, r3=word count.
// Keep the parity in r4-r8 and four table rows in r9-r12. Alternating r1/lr loads
// separate each load from its XOR. Save the output pointer once, outside the word loop.
// Core 0 fetches this loop from scratch Y, away from striped SRAM DMA traffic.
    // Core0's 8PSK shaper occupies scratch Y. Keep core1's BCH loop in striped
    // SRAM so simultaneous instruction fetches do not serialize on that bank.
    .section .time_critical.dvbs2_bch160_stream, "ax", %progbits
    .macro BCH160_XOR dst, old, off
    ldr     lr, [r9, #\off]
    ldr     r1, [r10, #\off]
    eor     \dst, \old, lr
    ldr     lr, [r11, #\off]
    eor     \dst, \dst, r1
    ldr     r1, [r12, #\off]
    eor     \dst, \dst, lr
    eor     \dst, \dst, r1
    .endm

    .align 2
    .global dvbs2_bch160_stream
    .type dvbs2_bch160_stream, %function
    .thumb_func
dvbs2_bch160_stream:
    push    {r4-r11, lr}
    push    {r1}
    movs    r4, #0
    movs    r5, #0
    movs    r6, #0
    movs    r7, #0
    movs    r8, #0
.Lbch_check:
    cmp     r3, #0
    beq     .Lbch_store
.Lbch_word:
    ldr     lr, [r0], #4
    eor     r4, r4, lr
    ubfx    r9, r4, #24, #8
    add     r9, r2, r9, lsl #5
    add     r9, r9, #24576
    ubfx    r10, r4, #16, #8
    add     r10, r2, r10, lsl #5
    add     r10, r10, #16384
    ubfx    r11, r4, #8, #8
    add     r11, r2, r11, lsl #5
    add     r11, r11, #8192
    uxtb    r12, r4
    add     r12, r2, r12, lsl #5
    BCH160_XOR r4, r5, 0
    BCH160_XOR r5, r6, 4
    BCH160_XOR r6, r7, 8
    BCH160_XOR r7, r8, 12
    ldr     r8, [r9, #16]
    ldr     lr, [r10, #16]
    ldr     r1, [r11, #16]
    eor     r8, r8, lr
    ldr     lr, [r12, #16]
    eor     r8, r8, r1
    eor     r8, r8, lr
    subs    r3, r3, #1
    bne     .Lbch_word
.Lbch_store:
    pop     {r0}
    stmia   r0!, {r4-r8}
    movs    r1, #0
    str     r1, [r0]
    pop     {r4-r11, pc}
    .size dvbs2_bch160_stream, .-dvbs2_bch160_stream

// Same word loop and epilogue, starting from a previous five-word remainder.
    .align 2
    .global dvbs2_bch160_continue
    .type dvbs2_bch160_continue, %function
    .thumb_func
dvbs2_bch160_continue:
    push    {r4-r11, lr}
    push    {r1}
    ldmia   r1, {r4-r8}
    b       .Lbch_check
    .size dvbs2_bch160_continue, .-dvbs2_bch160_continue
    .purgem BCH160_XOR

// In-place 32x32 MSB-first transpose. Fuse stages 16/8 in four-register tiles
// and stages 4/2/1 in eight-register tiles, loading/storing each matrix word twice.
    .section .scratch_x.dvbs2_transpose32, "ax", %progbits
    .macro TRANSPOSE_SWAP a, b, shift, mask
    eor     lr, \a, \b, lsr #\shift
    and     lr, lr, \mask
    eor     \a, \a, lr
    eor     \b, \b, lr, lsl #\shift
    .endm

    .align 2
    .global dvbs2_transpose32
    .type dvbs2_transpose32, %function
    .thumb_func
dvbs2_transpose32:
    push    {r4-r11, lr}
    movs    r1, #8
    mov.w   r3, #0x00ff00ff
.Ltranspose_wide_tile:
    ldr     r4, [r0]
    ldr     r5, [r0, #32]
    ldr     r6, [r0, #64]
    ldr     r7, [r0, #96]
    mov     r8, r4
    pkhtb   r4, r4, r6, asr #16
    pkhbt   r6, r6, r8, lsl #16
    mov     r8, r5
    pkhtb   r5, r5, r7, asr #16
    pkhbt   r7, r7, r8, lsl #16
    TRANSPOSE_SWAP r4, r5, 8, r3
    TRANSPOSE_SWAP r6, r7, 8, r3
    str     r4, [r0]
    str     r5, [r0, #32]
    str     r6, [r0, #64]
    str     r7, [r0, #96]
    adds    r0, #4
    subs    r1, #1
    bne     .Ltranspose_wide_tile
    subs    r0, #32
    movs    r1, #4
    mov.w   r2, #0x0f0f0f0f
    mov.w   r3, #0x33333333
    mov.w   r12, #0x55555555
.Ltranspose_low_tile:
    ldmia   r0, {r4-r11}
    TRANSPOSE_SWAP r4, r8, 4, r2
    TRANSPOSE_SWAP r5, r9, 4, r2
    TRANSPOSE_SWAP r6, r10, 4, r2
    TRANSPOSE_SWAP r7, r11, 4, r2
    TRANSPOSE_SWAP r4, r6, 2, r3
    TRANSPOSE_SWAP r5, r7, 2, r3
    TRANSPOSE_SWAP r8, r10, 2, r3
    TRANSPOSE_SWAP r9, r11, 2, r3
    TRANSPOSE_SWAP r4, r5, 1, r12
    TRANSPOSE_SWAP r6, r7, 1, r12
    TRANSPOSE_SWAP r8, r9, 1, r12
    TRANSPOSE_SWAP r10, r11, 1, r12
    stmia   r0!, {r4-r11}
    subs    r1, #1
    bne     .Ltranspose_low_tile
    pop     {r4-r11, pc}
    .size dvbs2_transpose32, .-dvbs2_transpose32
    .purgem TRANSPOSE_SWAP

// Map/scramble full 16-symbol words and append to a possibly partial ring stream.
// r0=frame, r1=swap, r2=flip, r3=word count; stack arguments are stream and byte LUT.
// r5-r10 hold out, mask, n, acc_i, acc_q, fill. Full-word puts keep fill unchanged.
    .section .scratch_x.dvbs2_map_words, "ax", %progbits
    .align 2
    .global dvbs2_map_words
    .type dvbs2_map_words, %function
    .thumb_func
dvbs2_map_words:
    push    {r4-r11, lr}
    ldr     r11, [sp, #36]
    ldr     r4, [sp, #40]
    ldmia   r11, {r5-r10}
    cmp     r3, #0
    beq     .Lmap_done
.Lmap_word:
    ldr     r12, [r0], #4
    rbit    r12, r12
    ldr     lr, [r1], #4
    eor     r11, r12, r12, lsr #1
    and     r11, r11, lr
    eor     r12, r12, r11
    eor     r12, r12, r11, lsl #1
    ldr     lr, [r2], #4
    eor     r12, r12, lr
    uxtb    lr, r12
    ldr     r11, [r4, lr, lsl #2]
    ubfx    lr, r12, #8, #8
    ldr     lr, [r4, lr, lsl #2]
    orr     r11, r11, lr, lsl #4
    ubfx    lr, r12, #16, #8
    ldr     lr, [r4, lr, lsl #2]
    orr     r11, r11, lr, lsl #8
    lsr     lr, r12, #24
    ldr     lr, [r4, lr, lsl #2]
    orr     r11, r11, lr, lsl #12
    uxth    r12, r11
    lsr     r11, r11, #16
    lsl     r12, r12, r10
    orr     r12, r12, r8
    lsl     r11, r11, r10
    orr     r11, r11, r9
    pkhbt   lr, r12, r11, lsl #16
    and     r8, r7, r6
    str     lr, [r5, r8, lsl #2]
    lsr     r8, r12, #16
    lsr     r9, r11, #16
    adds    r7, #1
    subs    r3, #1
    bne     .Lmap_word
.Lmap_done:
    ldr     r11, [sp, #36]
    stmia   r11, {r5-r10}
    pop     {r4-r11, pc}
    .size dvbs2_map_words, .-dvbs2_map_words
#endif
