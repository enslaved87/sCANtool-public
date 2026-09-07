/* Entry for $36 80 download-and-execute.
 *
 * Metal lesson: do not rewrite r1. The top of the 256 KiB SRAM window
 * is not a safe stack on this part; the first C prologue then faults
 * and the ROM handler makes it look like the kernel never ran.
 * Leave r1/r2/r13 alone. Zero .bss, call C, park if C returns. */

    .section .text.entry, "ax"
    .globl  _start
    .type   _start, @function

_start:
    lis     %r3, __bss_beg@h
    ori     %r3, %r3, __bss_beg@l
    lis     %r4, __bss_end@h
    ori     %r4, %r4, __bss_end@l
    li      %r0, 0
.Lzero:
    cmpw    %r3, %r4
    bge     .Lgo
    stw     %r0, 0(%r3)
    addi    %r3, %r3, 4
    b       .Lzero
.Lgo:
    bl      scpb_main
.Lpark:
    b       .Lpark
    .size   _start, . - _start
