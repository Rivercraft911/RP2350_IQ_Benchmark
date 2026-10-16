// Arm leaves for the DVB-S2 encoder; portable fallbacks remain in dvbs2.c.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb

// Five-word BCH remainder. r0=input, r1=parity, r2=table, r3=word count.
// Keep the parity in r4-r8 and four table rows in r9-r12. Alternating r1/lr loads
// separate each load from its XOR. Save the output pointer once, outside the word loop.
// The same loop is emitted twice. The striped-SRAM copy suits core 1 while core 0 runs
// shapers from scratch Y (8PSK). The scratch-Y continue copy is for core-0 frontend
// steps (QPSK and APSK split modes): its fetches then avoid the striped banks shared
// with core 1's code and the output sample DMA. Selected by dvbs2_frontend_set_bank.
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

    .macro BCH160_LOOP check, word, store
\check:
    cmp     r3, #0
    beq     \store
\word:
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
    bne     \word
\store:
    pop     {r0}
    stmia   r0!, {r4-r8}
    movs    r1, #0
    str     r1, [r0]
    pop     {r4-r11, pc}
    .endm

    .section .time_critical.dvbs2_bch160_stream, "ax", %progbits
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
    BCH160_LOOP .Lbch_check, .Lbch_word, .Lbch_store
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

    .section .scratch_y.dvbs2_bch160_continue_y, "ax", %progbits
    .align 2
    .global dvbs2_bch160_continue_y
    .type dvbs2_bch160_continue_y, %function
    .thumb_func
dvbs2_bch160_continue_y:
    push    {r4-r11, lr}
    push    {r1}
    ldmia   r1, {r4-r8}
    BCH160_LOOP .Lbchy_check, .Lbchy_word, .Lbchy_store
    .size dvbs2_bch160_continue_y, .-dvbs2_bch160_continue_y
    .global dvbs2_bch160_continue_y_end
dvbs2_bch160_continue_y_end:
    .purgem BCH160_LOOP
    .purgem BCH160_XOR

// Degree128 (normal8/9 and9/10): four live words, the existing padded table.
// The full-frame copy stays in XIP because core1 can own a full encoder while core0
// executes other scratch-Y kernels. Only the core0 frontend's bank1 uses the Y copy.
    .macro BCH128_XOR dst, old, off
    ldr     lr, [r9, #\off]
    ldr     r1, [r10, #\off]
    eor     \dst, \old, lr
    ldr     lr, [r11, #\off]
    eor     \dst, \dst, r1
    ldr     r1, [r12, #\off]
    eor     \dst, \dst, lr
    eor     \dst, \dst, r1
    .endm

    .macro BCH128_CONTINUE check, word, store
    push    {r4-r11, lr}
    push    {r1}
    ldmia   r1, {r4-r7}
\check:
    cmp     r3, #0
    beq     \store
\word:
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
    BCH128_XOR r4, r5, 0
    BCH128_XOR r5, r6, 4
    BCH128_XOR r6, r7, 8
    ldr     r7, [r9, #12]
    ldr     lr, [r10, #12]
    ldr     r1, [r11, #12]
    eor     r7, r7, lr
    ldr     lr, [r12, #12]
    eor     r7, r7, r1
    eor     r7, r7, lr
    subs    r3, r3, #1
    bne     \word
\store:
    pop     {r0}
    stmia   r0!, {r4-r7}
    movs    r1, #0
    str     r1, [r0]
    str     r1, [r0, #4]
    pop     {r4-r11, pc}
    .endm

    .section .text.dvbs2_bch128_continue, "ax", %progbits
    .align 2
    .global dvbs2_bch128_continue
    .type dvbs2_bch128_continue, %function
    .thumb_func
dvbs2_bch128_continue:
    BCH128_CONTINUE .Lbch128_check, .Lbch128_word, .Lbch128_store
    .size dvbs2_bch128_continue, .-dvbs2_bch128_continue

    .section .scratch_y.dvbs2_bch128_continue_y, "ax", %progbits
    .align 2
    .global dvbs2_bch128_continue_y
    .type dvbs2_bch128_continue_y, %function
    .thumb_func
dvbs2_bch128_continue_y:
    BCH128_CONTINUE .Lbch128y_check, .Lbch128y_word, .Lbch128y_store
    .size dvbs2_bch128_continue_y, .-dvbs2_bch128_continue_y
    .global dvbs2_bch128_continue_y_end
dvbs2_bch128_continue_y_end:
    .purgem BCH128_CONTINUE
    .purgem BCH128_XOR

// Degree192: six live remainder words leave one fewer register for row pointers.
// Borrow r2 for the fourth row and reload the constant table base from one stack slot.
// All table loads are separated from their XOR; no remainder is spilled in the loop.
// Opt-in C4 leaf only, sharing the initialized 32768-byte table with both workers.
    .macro BCH192_XOR dst, old, off
    ldr     lr, [r10, #\off]
    ldr     r1, [r11, #\off]
    eor     \dst, \old, lr
    ldr     lr, [r12, #\off]
    eor     \dst, \dst, r1
    ldr     r1, [r2, #\off]
    eor     \dst, \dst, lr
    eor     \dst, \dst, r1
    .endm

    .section .scratch_y.dvbs2_bch192_continue_y, "ax", %progbits
    .align 2
    .global dvbs2_bch192_continue_y
    .type dvbs2_bch192_continue_y, %function
    .thumb_func
dvbs2_bch192_continue_y:
    push    {r4-r11, lr}
    push    {r1-r3}
    ldmia   r1, {r4-r9}
    cmp     r3, #0
    beq     .Lbch192_store
.Lbch192_word:
    ldr     lr, [r0], #4
    ldr     r2, [sp, #4]
    eor     r4, r4, lr
    ubfx    r10, r4, #24, #8
    add     r10, r2, r10, lsl #5
    add     r10, r10, #24576
    ubfx    r11, r4, #16, #8
    add     r11, r2, r11, lsl #5
    add     r11, r11, #16384
    ubfx    r12, r4, #8, #8
    add     r12, r2, r12, lsl #5
    add     r12, r12, #8192
    uxtb    r1, r4
    add     r2, r2, r1, lsl #5
    BCH192_XOR r4, r5, 0
    BCH192_XOR r5, r6, 4
    BCH192_XOR r6, r7, 8
    BCH192_XOR r7, r8, 12
    BCH192_XOR r8, r9, 16
    ldr     r9, [r10, #20]
    ldr     lr, [r11, #20]
    ldr     r1, [r12, #20]
    eor     r9, r9, lr
    ldr     lr, [r2, #20]
    eor     r9, r9, r1
    eor     r9, r9, lr
    subs    r3, r3, #1
    bne     .Lbch192_word
.Lbch192_store:
    pop     {r0-r2}
    stmia   r0, {r4-r9}
    pop     {r4-r11, pc}
    .size dvbs2_bch192_continue_y, .-dvbs2_bch192_continue_y
    .global dvbs2_bch192_continue_y_end
dvbs2_bch192_continue_y_end:
    .purgem BCH192_XOR

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
    .global dvbs2_transpose32_end
dvbs2_transpose32_end:
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
