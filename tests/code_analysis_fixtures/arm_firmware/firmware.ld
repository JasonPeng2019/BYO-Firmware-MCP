ENTRY(reset_handler)
MEMORY { FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 64K
         RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 16K }
_stack_top = ORIGIN(RAM) + LENGTH(RAM);
SECTIONS {
 .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
 .text : { *(.text*) *(.rodata*) } > FLASH
 .ARM.exidx : { *(.ARM.exidx*) } > FLASH
 .data : { _data_start = .; *(.data*) _data_end = .; } > RAM AT> FLASH
 _data_load = LOADADDR(.data);
 .bss (NOLOAD) : { _bss_start = .; *(.bss*) *(COMMON) _bss_end = .; } > RAM
}
