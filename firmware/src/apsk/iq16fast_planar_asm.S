// Consecutive output registers permit one four-word STM per axis. The existing
// C history prefix emits RAW_PAIRS, so transpose only its at most two blocks.
    .syntax unified
    .thumb
    .text
    .p2align 2
    .macro AXIS shift, offset
    ldr r4,[r9,#-8]
    ubfx r4,r4,#\shift+3,#9
    add r4,r11,r4,lsl #4
    ldm r4,{r0-r2}
    ldr r4,[r9,#-4]
    ubfx r4,r4,#\shift,#12
    add r4,r12,r4,lsl #4
    ldm r4,{r3-r6}
    sadd16 r0,r0,r3
    sadd16 r1,r1,r4
    sadd16 r2,r2,r5
    ldr r4,[r9]
    ubfx r4,r4,#\shift,#12
    add r4,lr,r4,lsl #4
    ldm r4,{r3-r5,r7}
    sadd16 r0,r0,r3
    sadd16 r1,r1,r4
    sadd16 r2,r2,r5
    sadd16 r3,r6,r7
    add r4,r8,#\offset
    stm r4,{r0-r3}
    .endm
    .global iq16fast_planar_asm
    .type iq16fast_planar_asm,%function
    .thumb_func
iq16fast_planar_asm:
    cmp r2,#0
    beq 4f
    push {r4-r11,lr}
    sub sp,#12
    mov r8,r0
    mov r9,r1
    mov r10,r2
    ldr r4,[sp,#48]
    str r4,[sp,#0]
    movs r5,#2
    cmp r10,#2
    it lo
    movlo r5,r10
    mov r2,r5
    bl iq16native8_kernel_c
    mov r0,r8
    mov r1,r5
1:
    ldr r2,[r0,#4]
    ldr r3,[r0,#8]
    ldr r6,[r0,#12]
    ldr r7,[r0,#16]
    ldr r12,[r0,#20]
    ldr lr,[r0,#24]
    str r3,[r0,#4]
    str r7,[r0,#8]
    str lr,[r0,#12]
    str r2,[r0,#16]
    str r6,[r0,#20]
    str r12,[r0,#24]
    add r0,#32
    subs r1,#1
    bne 1b
    add r8,r8,r5,lsl #5
    add r9,r9,r5,lsl #2
    subs r10,r10,r5
    beq 3f
    ldm r4,{r11,r12,lr}
2:
    AXIS 0,0
    AXIS 16,16
    add r9,#4
    add r8,#32
    subs r10,#1
    bne 2b
3:  add sp,#12
    pop {r4-r11,pc}
4:  bx lr
    .size iq16fast_planar_asm,.-iq16fast_planar_asm
    .purgem AXIS
