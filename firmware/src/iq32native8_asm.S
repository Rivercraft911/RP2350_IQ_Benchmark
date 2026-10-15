// Exact L8/N2 32APSK: paired radix17 address calculation, native333 rows.
    .syntax unified
    .thumb
    .text
    .p2align 2
    .global iq32native8_kernel_asm
    .type iq32native8_kernel_asm,%function
    .thumb_func
iq32native8_kernel_asm:
    cmp r2,#0
    beq 3f
    push {r4-r11,lr}
    sub sp,#12
    mov r8,r0
    mov r9,r1
    mov r10,r2
    ldr r4,[sp,#48]
    str r4,[sp,#0]
    movs r5,#4
    cmp r10,#4
    it lo
    movlo r5,r10
    mov r2,r5
    bl iq32native8_kernel_c
    add r8,r8,r5,lsl #4
    add r9,r9,r5,lsl #2
    subs r10,r10,r5
    beq 2f
    ldm r4,{r11,r12,lr}
5:
    // old = last(a) +17*(first(b)+17*last(b)), both axes at once.
    ldr r4,[r9,#-16]
    ldr r5,[r9,#-12]
    lsr r4,r4,#5
    and r4,r4,#0x001f001f
    and r6,r5,#0x001f001f
    lsr r5,r5,#5
    and r5,r5,#0x001f001f
    add r5,r5,r5,lsl #4
    add r5,r5,r6
    add r5,r5,r5,lsl #4
    add r0,r5,r4
    // middle = first(c) +17*(last(c)+17*first(d)).
    ldr r4,[r9,#-8]
    ldr r5,[r9,#-4]
    and r6,r4,#0x001f001f
    lsr r4,r4,#5
    and r4,r4,#0x001f001f
    and r5,r5,#0x001f001f
    add r5,r5,r5,lsl #4
    add r5,r5,r4
    add r5,r5,r5,lsl #4
    add r1,r5,r6
    // future = last(d) +17*(first(e)+17*last(e)).
    ldr r4,[r9,#-4]
    ldr r5,[r9]
    lsr r4,r4,#5
    and r4,r4,#0x001f001f
    and r6,r5,#0x001f001f
    lsr r5,r5,#5
    and r5,r5,#0x001f001f
    add r5,r5,r5,lsl #4
    add r5,r5,r6
    add r5,r5,r5,lsl #4
    add r2,r5,r4
    // Preserve all four output words in bus order; one STM publishes them.
    lsr r6,r0,#16
    uxth r0,r0
    add r0,r11,r0,lsl #3
    ldm r0,{r3,r5}
    add r6,r11,r6,lsl #3
    ldm r6,{r4,r6}
    uxth r0,r1
    add r0,r12,r0,lsl #3
    ldm r0,{r0,r7}
    sadd16 r3,r3,r0
    sadd16 r5,r5,r7
    lsr r1,r1,#16
    add r1,r12,r1,lsl #3
    ldm r1,{r0,r7}
    sadd16 r4,r4,r0
    sadd16 r6,r6,r7
    uxth r0,r2
    add r0,lr,r0,lsl #3
    ldm r0,{r0,r7}
    sadd16 r3,r3,r0
    sadd16 r5,r5,r7
    lsr r2,r2,#16
    add r2,lr,r2,lsl #3
    ldm r2,{r0,r7}
    sadd16 r4,r4,r0
    sadd16 r6,r6,r7
    stm r8!,{r3-r6}
    add r9,#4
    subs r10,#1
    bne 5b
2:  add sp,#12
    pop {r4-r11,pc}
3:  bx lr
    .size iq32native8_kernel_asm,.-iq32native8_kernel_asm
