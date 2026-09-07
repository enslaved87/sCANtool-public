/* SRAM-resident read kernel. Loaded at 0x40001000; low 4 KiB stays
 * with the stock stack the bootloader already set up. */

OUTPUT_FORMAT("elf32-powerpc", "elf32-powerpc", "elf32-powerpc")
OUTPUT_ARCH(powerpc)
ENTRY(_start)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x40001000, LENGTH = 12K
}

SECTIONS
{
    .text 0x40001000 : {
        KEEP(*(.text.entry))
        *(.text .text.*)
        KEEP(*(.rodata .rodata.*))
        . = ALIGN(4);
    } > RAM

    .data : {
        *(.data .data.*)
        . = ALIGN(4);
    } > RAM

    .bss (NOLOAD) : {
        __bss_beg = .;
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > RAM

    /DISCARD/ : { *(.comment) *(.note.*) *(.eh_frame) }
}
