// Descriptor-only L12,N4 QPSK experiment. Contract in iqdesc.h; no sample reads.
// Two symbols/pair: four extracts, four address additions, two doubleword stores.
// r0/r1 output I/Q; r4/r5 base I/Q; r6 prev; r7 cur; r8/r11 window I/Q;
// r9 input; r10 end; r12/lr symbol j addresses; r2/r3 symbol j+1 addresses.
    .syntax unified
    .thumb
    .section .time_critical.iqdesc_asm, "ax"
    .p2align 2

    .macro DESCPAIR j
    ubfx    r12, r8, #(5+\j), #12
    ubfx    lr, r11, #(5+\j), #12
    ubfx    r2, r8, #(6+\j), #12
    ubfx    r3, r11, #(6+\j), #12
    add     r12, r4, r12, lsl #3
    add     lr, r5, lr, lsl #3
    add     r2, r4, r2, lsl #3
    add     r3, r5, r3, lsl #3
    strd    r12, r2, [r0], #8
    strd    lr, r3, [r1], #8
    .endm

    .global iqdesc_asm
    .type iqdesc_asm, %function
    .thumb_func
iqdesc_asm:
    push    {r4-r11, lr}
    mov     r9, r2
    add     r10, r2, r3, lsl #2
    ldr     r6, [sp, #36]               // prev (5th argument)
    ldr     r4, [sp, #40]               // base_i
    ldr     r5, [sp, #44]               // base_q
1:  cmp     r9, r10
    beq     2f
    ldr     r7, [r9], #4
    pkhbt   r8, r6, r7, lsl #16
    pkhtb   r11, r7, r6, asr #16
    mov     r6, r7
    .irp j, 0,2,4,6,8,10,12,14
    DESCPAIR \j
    .endr
    b       1b
2:  pop     {r4-r11, pc}
    .size iqdesc_asm, . - iqdesc_asm

// Paired rows: each 13-bit key encodes histories for two chronological symbols.
// Four symbols/pair: four extracts, four row-address additions, two doubleword stores.
    .section .time_critical.iqdesc_pair_asm, "ax"
    .p2align 2
    .macro PAIRDESCPAIR j
    ubfx    r12, r8, #(5+\j), #13
    ubfx    lr, r11, #(5+\j), #13
    ubfx    r2, r8, #(7+\j), #13
    ubfx    r3, r11, #(7+\j), #13
    add     r12, r4, r12, lsl #4
    add     lr, r5, lr, lsl #4
    add     r2, r4, r2, lsl #4
    add     r3, r5, r3, lsl #4
    strd    r12, r2, [r0], #8
    strd    lr, r3, [r1], #8
    .endm

    .global iqdesc_pair_asm
    .type iqdesc_pair_asm, %function
    .thumb_func
iqdesc_pair_asm:
    push    {r4-r11, lr}
    mov     r9, r2
    add     r10, r2, r3, lsl #2
    ldr     r6, [sp, #36]
    ldr     r4, [sp, #40]
    ldr     r5, [sp, #44]
1:  cmp     r9, r10
    beq     2f
    ldr     r7, [r9], #4
    pkhbt   r8, r6, r7, lsl #16
    pkhtb   r11, r7, r6, asr #16
    mov     r6, r7
    .irp j, 0,4,8,12
    PAIRDESCPAIR \j
    .endr
    b       1b
2:  pop     {r4-r11, pc}
    .size iqdesc_pair_asm, . - iqdesc_pair_asm

// L9 four-symbol rows. Two adjacent descriptors cover eight symbols; each row
// selects12 bits and occupies32 bytes. Stack arguments match the older leaves.
    .section .scratch_y.iqdesc_quad_asm, "ax"
    .p2align 2
    .macro QUADDESCPAIR j
    ubfx    r12, r8, #(8+\j), #12
    ubfx    lr, r11, #(8+\j), #12
    ubfx    r2, r8, #(12+\j), #12
    ubfx    r3, r11, #(12+\j), #12
    add     r12, r4, r12, lsl #5
    add     lr, r5, lr, lsl #5
    add     r2, r4, r2, lsl #5
    add     r3, r5, r3, lsl #5
    strd    r12, r2, [r0], #8
    strd    lr, r3, [r1], #8
    .endm

    .global iqdesc_quad_asm
    .type iqdesc_quad_asm, %function
    .thumb_func
iqdesc_quad_asm:
    push    {r4-r11, lr}
    mov     r9, r2
    add     r10, r2, r3, lsl #2
    ldr     r6, [sp, #36]
    ldr     r4, [sp, #40]
    ldr     r5, [sp, #44]
1:  cmp     r9, r10
    beq     2f
    ldr     r7, [r9], #4
    pkhbt   r8, r6, r7, lsl #16
    pkhtb   r11, r7, r6, asr #16
    mov     r6, r7
    .irp j, 0,8
    QUADDESCPAIR \j
    .endr
    b       1b
2:  pop     {r4-r11, pc}
    .size iqdesc_quad_asm, . - iqdesc_quad_asm
    .purgem QUADDESCPAIR
