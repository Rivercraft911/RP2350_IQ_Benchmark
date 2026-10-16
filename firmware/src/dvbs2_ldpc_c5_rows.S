// C5 bounded row passes. Copied after the C5 group leaf into an idle-only arena.
// Both entries are PIC, reentrant and accept 1..8 rows per call. No shared scratch.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .cpu cortex-m33
    .thumb
    .section .text.dvbs2_ldpc_rows_c5_pic, "ax", %progbits
    .balign 4
    .global dvbs2_ldpc_rows_c5_pic
    .global dvbs2_ldpc_accumulate_c5_pic
    .global dvbs2_ldpc_rows_c5_pic_end
    .type dvbs2_ldpc_rows_c5_pic, %function
    .thumb_func
dvbs2_ldpc_rows_c5_pic:
    push    {r4-r6, lr}
    movs    r3, #0
    movs    r4, #0
    movs    r5, #0
    movs    r6, #0
.Lc5_clear:
    stm     r0!, {r3-r6}
    stm     r0!, {r3-r6}
    stm     r0!, {r3-r6}
    subs    r1, #1
    bne     .Lc5_clear
    pop     {r4-r6, pc}
    .size dvbs2_ldpc_rows_c5_pic, .-dvbs2_ldpc_rows_c5_pic

    .balign 4
    .type dvbs2_ldpc_accumulate_c5_pic, %function
    .thumb_func
dvbs2_ldpc_accumulate_c5_pic:
    push    {r4-r10, lr}
    mov     r2, r1
    sub     r1, r0, #48
.Lc5_accumulate:
    .rept 3
    mov     r12, r0
    ldm     r1!, {r3-r6}
    ldm     r0!, {r7-r10}
    eor     r7, r3
    eor     r8, r4
    eor     r9, r5
    eor     r10, r6
    stm     r12!, {r7-r10}
    .endr
    subs    r2, #1
    bne     .Lc5_accumulate
    pop     {r4-r10, pc}
    .size dvbs2_ldpc_accumulate_c5_pic, .-dvbs2_ldpc_accumulate_c5_pic
dvbs2_ldpc_rows_c5_pic_end:
#endif
    .section .note.GNU-stack, "", %progbits
