// Exact 16APSK L9/N2 shaper using four-word row bursts, flash cached.
// iq16chunk_kernel_asm(out,in,blocks,prev,tables), same layout as the C leaf.
// A NULL reversal map selects the C baseline. No peripheral/interpolator state.
    .syntax unified
    .thumb
    .text
    .p2align 2
    .macro AXIS shift, offset
    ldr r4,[r9,#-8]                // oldest four symbols
    ubfx r4,r4,#\shift,#12
    add r4,r11,r4,lsl #4
    ldm r4,{r0-r3}
    ldr r4,[r9,#-4]                // middle four symbols
    ubfx r4,r4,#\shift,#12
    add r4,r12,r4,lsl #4
    ldm r4,{r4-r7}
    sadd16 r0,r0,r4
    sadd16 r1,r1,r5
    sadd16 r2,r2,r6
    sadd16 r3,r3,r7
    ldr r4,[r9]                    // newest four symbols, reflected edge row
    ubfx r4,r4,#\shift,#12
    ldrh r4,[lr,r4,lsl #1]
    add r4,r11,r4,lsl #4
    ldm r4,{r4-r7}
    ror r4,r4,#16
    ror r5,r5,#16
    ror r6,r6,#16
    ror r7,r7,#16
    sadd16 r0,r0,r7
    sadd16 r1,r1,r6
    sadd16 r2,r2,r5
    sadd16 r3,r3,r4
    str r0,[r8,#\offset+0]
    str r1,[r8,#\offset+8]
    str r2,[r8,#\offset+16]
    str r3,[r8,#\offset+24]
    .endm
    .global iq16chunk_kernel_asm
    .type iq16chunk_kernel_asm,%function
    .thumb_func
iq16chunk_kernel_asm:
    cmp r2,#0
    beq 3f
    ldr r12,[sp]                   // fifth AAPCS argument
    ldr r12,[r12,#8]
    cmp r12,#0
    beq 4f
    push {r4-r11,lr}
    sub sp,#12                    // outgoing fifth argument;8B stack alignment
    mov r8,r0
    mov r9,r1
    mov r10,r2
    ldr r4,[sp,#48]                // tables
    str r4,[sp,#0]
    movs r5,#2
    cmp r10,#2
    it lo
    movlo r5,r10
    mov r2,r5
    bl iq16chunk_kernel_c          // establish contiguous history for at most8symbols
    add r8,r8,r5,lsl #5
    add r9,r9,r5,lsl #2
    subs r10,r10,r5
    beq 2f
    ldm r4,{r11,r12,lr}            // edge,middle,reverse bases
5:
    AXIS 0,0
    AXIS 16,4
    add r9,#4
    add r8,#32
    subs r10,#1
    bne 5b
2:  add sp,#12
    pop {r4-r11,pc}
3:  bx lr
4:  b iq16chunk_kernel_c
    .size iq16chunk_kernel_asm,.-iq16chunk_kernel_asm
    .purgem AXIS
