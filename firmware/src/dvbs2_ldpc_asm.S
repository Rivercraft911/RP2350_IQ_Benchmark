// Scheduled LDPC row updates; the portable equivalent remains in dvbs2.c.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    .syntax unified
    .thumb

// r0=matrix, r1=duplicated group, r2=row byte offsets, r3=packed source offsets;
// stack argument=count. Source metadata is (byte offset << 8) | bit shift.
// Each word retains seven instructions but loads the row before shifting, hiding
// the baseline LDR-to-EOR dependency. Split XORs equal OR then XOR because the
// shifted fields do not overlap. Register LSR by 32 supplies zero when shift=0.
    .macro LDPC_XOR_WORD off, lo, hi
    ldr     \hi, [r1, #(\off+4)]
    ldr     r7, [r0, #\off]
    lsl     r6, \lo, r2
    lsr     r12, \hi, r3
    eors    r7, r6
    eor     r7, r7, r12
    str     r7, [r0, #\off]
    .endm

// Scratch Y has 304 bytes spare in both binaries. Low registers keep each word
// update at 20 bytes; this entire leaf occupies 300 bytes without extra tables.
    .section .scratch_y.dvbs2_ldpc_xor_entries, "ax", %progbits
    .align 2
    .global dvbs2_ldpc_xor_entries
    .type dvbs2_ldpc_xor_entries, %function
    .thumb_func
dvbs2_ldpc_xor_entries:
    push    {r4-r11, lr}
    mov     r11, r0
    mov     r10, r1
    mov     r9, r2
    mov     r8, r3
    ldr     lr, [sp, #36]
    cmp     lr, #0
    beq     .Lldpc_done
.Lldpc_entry:
    ldrh    r0, [r9], #2
    ldrh    r2, [r8], #2
    add     r0, r11, r0
    lsrs    r1, r2, #8
    and     r2, r2, #31
    add     r1, r10, r1
    rsb     r3, r2, #32
    ldr     r4, [r1]
    LDPC_XOR_WORD 0, r4, r5
    LDPC_XOR_WORD 4, r5, r4
    LDPC_XOR_WORD 8, r4, r5
    LDPC_XOR_WORD 12, r5, r4
    LDPC_XOR_WORD 16, r4, r5
    LDPC_XOR_WORD 20, r5, r4
    LDPC_XOR_WORD 24, r4, r5
    LDPC_XOR_WORD 28, r5, r4
    LDPC_XOR_WORD 32, r4, r5
    LDPC_XOR_WORD 36, r5, r4
    LDPC_XOR_WORD 40, r4, r5
    LDPC_XOR_WORD 44, r5, r4
    subs    lr, lr, #1
    bne     .Lldpc_entry
.Lldpc_done:
    pop     {r4-r11, pc}
    .size dvbs2_ldpc_xor_entries, .-dvbs2_ldpc_xor_entries
    .purgem LDPC_XOR_WORD
#endif
