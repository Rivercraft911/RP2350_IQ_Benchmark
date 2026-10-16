// Position-independent C4 LDPC group leaf. No literal pool, external branch or
// mutable state: an idle caller may copy these bytes into its own SRAM arena.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb

// r0=matrix, r1=input at group start word, r2=group bit offset, r3=row indices;
// stack arguments=rotation offsets, edge count. Extract one 360-bit group into
// u2[0..11], duplicate it at bit360, and update only the caller's matrix.
// Two words in flight separate every shifted EOR from its consumer. The last
// word's store is delayed into the next pair (or past the final branch).
    .macro LDPC_PAIR off, shift
    ldr     r5, [r1, #(\off+4)]
    ldr     r7, [r0, #(\off+4)]
    eor     r6, r6, r4, lsl #\shift
    ldr     r4, [r1, #(\off+8)]
    eor     r6, r6, r5, lsr #(32-\shift)
    eor     r7, r7, r5, lsl #\shift
    str     r6, [r0, #\off]
    eor     r7, r7, r4, lsr #(32-\shift)
    .endm
    .macro LDPC_NEXT_PAIR off, shift
    ldr     r6, [r0, #\off]
    str     r7, [r0, #(\off-4)]
    LDPC_PAIR \off, \shift
    .endm

    .section .text.dvbs2_ldpc_group_pic, "ax", %progbits
    .align 2
    .global dvbs2_ldpc_group_pic
    .global dvbs2_ldpc_group_pic_end
    .type dvbs2_ldpc_group_pic, %function
    .thumb_func
dvbs2_ldpc_group_pic:
    push    {r4-r11, lr}
    sub     sp, #100
    mov     r11, r0
    mov     r9, r3
    ldr     r8, [sp, #136]
    ldr     lr, [sp, #140]
    rsb     r3, r2, #32
    mov     r7, sp
    movs    r10, #12
    ldr     r4, [r1], #4
.Lldpc_extract:
    ldr     r5, [r1], #4
    lsl     r6, r4, r2
    lsr     r12, r5, r3
    orr     r6, r6, r12
    str     r6, [r7], #4
    mov     r4, r5
    subs    r10, #1
    bne     .Lldpc_extract
    ldr     r4, [sp, #44]
    and     r4, r4, #0xff000000
    str     r4, [sp, #44]
    mov     r1, sp
    add     r7, sp, #48
    movs    r10, #11
.Lldpc_duplicate:
    ldr     r4, [r1], #4
    ldr     r5, [r1]
    lsrs    r5, r5, #8
    orr     r5, r5, r4, lsl #24
    str     r5, [r7], #4
    subs    r10, #1
    bne     .Lldpc_duplicate
    ldr     r5, [sp]
    ldr     r4, [sp, #44]
    orr     r4, r4, r5, lsr #8
    str     r4, [sp, #44]
    movs    r4, #0
    str     r4, [sp, #92]
    str     r4, [sp, #96]
    cmp     lr, #0
    beq     .Lldpc_done
.Lldpc_edge:
    ldrh    r0, [r9], #2
    ldrh    r2, [r8], #2
    add     r0, r0, r0, lsl #1
    add     r0, r11, r0, lsl #4
    lsrs    r1, r2, #5
    add     r1, sp, r1, lsl #2
    and     r2, r2, #31
    ldr     r4, [r1]
    tbh     [pc, r2, lsl #1]
.Lldpc_dispatch:
    .irp shift, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
    .hword (.Lldpc_rot\shift - .Lldpc_dispatch) / 2
    .endr
.Lldpc_rot0:
    .irp off, 0,4,8,12,16,20,24,28,32,36,40,44
    ldr     r4, [r1, #\off]
    ldr     r7, [r0, #\off]
    eors    r7, r4
    str     r7, [r0, #\off]
    .endr
    b       .Lldpc_edge_count
    .irp shift, 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
.Lldpc_rot\shift:
    ldr     r6, [r0]
    LDPC_PAIR 0, \shift
    LDPC_NEXT_PAIR 8, \shift
    LDPC_NEXT_PAIR 16, \shift
    LDPC_NEXT_PAIR 24, \shift
    LDPC_NEXT_PAIR 32, \shift
    LDPC_NEXT_PAIR 40, \shift
    b       .Lldpc_edge_store
    .endr
.Lldpc_edge_store:
    str     r7, [r0, #44]
.Lldpc_edge_count:
    subs    lr, lr, #1
    bne     .Lldpc_edge
.Lldpc_done:
    add     sp, #100
    pop     {r4-r11, pc}
    .size dvbs2_ldpc_group_pic, .-dvbs2_ldpc_group_pic
dvbs2_ldpc_group_pic_end:
    .purgem LDPC_PAIR
    .purgem LDPC_NEXT_PAIR
#endif
