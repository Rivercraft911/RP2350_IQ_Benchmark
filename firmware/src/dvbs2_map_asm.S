// Opt-in QPSK mapper: packed carry, contiguous output, independent table loads.
// Same six-argument ABI and canonical FEC/table formats as dvbs2_map_words.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb
    .section .scratch_x.dvbs2_map_words_rotate_chunks_scheduled, "ax", %progbits
    .align 2
    .global dvbs2_map_words_rotate_chunks_scheduled
    .type dvbs2_map_words_rotate_chunks_scheduled, %function
    .thumb_func
dvbs2_map_words_rotate_chunks_scheduled:
    push    {r4-r11, lr}
    ldr     r11, [sp, #36]
    ldr     r4, [sp, #40]
    ldmia   r11, {r5-r10}
    // r8=duplicated low-fill mask, r9=packed I/Q carry, r10=32-fill.
    orr     r9, r8, r9, lsl #16
    rsb     r10, r10, #32
    mov.w   r8, #0xffffffff
    lsr     r8, r8, r10
    orr     r8, r8, r8, lsl #16
    cmp     r3, #0
    beq     .Lrotate_done
.Lrotate_chunk:
    // Clamp to a contiguous output interval. Compare lengths minus1 to support
    // mask=~0 (a linear stream) without overflowing a 2^32-word capacity.
    and     r12, r7, r6
    add     r5, r5, r12, lsl #2
    sub     r6, r6, r12
    subs    r11, r3, #1
    cmp     r11, r6
    it      lo
    movlo   r6, r11
    adds    r6, #1
    sub     r3, r3, r6
    add     r7, r7, r6
    push    {r3} // remaining words; free r3 to pipeline independent table loads
.Lrotate_word:
    ldr     r12, [r0], #4
    ldr     lr, [r1], #4
    rbit    r12, r12
    eor     r11, r12, r12, lsr #1
    and     r11, r11, lr
    ldr     lr, [r2], #4
    eor     r12, r12, r11
    eor     r12, r12, r11, lsl #1
    eor     r12, r12, lr
    uxtb    lr, r12
    ubfx    r3, r12, #8, #8
    ldr     r11, [r4, lr, lsl #2]
    ldr     r3, [r4, r3, lsl #2]
    ubfx    lr, r12, #16, #8
    orr     r11, r11, r3, lsl #4
    ldr     lr, [r4, lr, lsl #2]
    lsr     r3, r12, #24
    ldr     r3, [r4, r3, lsl #2]
    orr     r11, r11, lr, lsl #8
    orr     r11, r11, r3, lsl #12
    ror     r12, r11, r10
    bic     lr, r12, r8
    orr     lr, lr, r9
    and     r9, r12, r8
    ror     r9, r9, #16
    str     lr, [r5], #4
    subs    r6, #1
    bne     .Lrotate_word
    pop     {r3}
    ldr     r11, [sp, #36]
    ldmia   r11, {r5-r6}
    cmp     r3, #0
    bne     .Lrotate_chunk
.Lrotate_done:
    // Restore the public stream representation; fill is unchanged by full-word appends.
    rsb     r10, r10, #32
    uxth    r8, r9
    lsr     r9, r9, #16
    ldr     r11, [sp, #36]
    stmia   r11, {r5-r10}
    pop     {r4-r11, pc}
    .size dvbs2_map_words_rotate_chunks_scheduled, .-dvbs2_map_words_rotate_chunks_scheduled
#endif
