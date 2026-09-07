# Build the sCANtool Public read kernel (PowerPC Book E, no VLE).
# Example: make PREFIX=C:/SysGCC/powerpc-eabi/bin/powerpc-eabi-

PREFIX   ?= powerpc-eabi-
CC       := $(PREFIX)gcc
OBJCOPY  := $(PREFIX)objcopy
SIZE     := $(PREFIX)size

# This gcc 4.9 accepts e300c2 (integer Book E). e200z7 is not a -mcpu name here.
CPUFLAGS := -mcpu=e300c2 -mbig
CFLAGS   := $(CPUFLAGS) -std=c11 -Os -Wall -Wextra -ffreestanding \
            -fno-builtin -fno-common -fno-stack-protector -nostdlib \
            -msdata=none -fomit-frame-pointer
LDFLAGS  := $(CPUFLAGS) -nostdlib -Wl,-T,kernel.ld -Wl,-Map,kernel.map \
            -Wl,--gc-sections

OUT_BIN  := ../kernel.bin

all: $(OUT_BIN)

start.o: start.S
	$(CC) $(CFLAGS) -c -o $@ $<

eabi_rest.o: eabi_rest.S
	$(CC) $(CFLAGS) -c -o $@ $<

main.o: main.c mpc5674_flexcan.h
	$(CC) $(CFLAGS) -c -o $@ $<

kernel.elf: start.o main.o eabi_rest.o kernel.ld
	$(CC) $(LDFLAGS) -o $@ start.o main.o eabi_rest.o
	$(SIZE) $@

$(OUT_BIN): kernel.elf
	$(OBJCOPY) -O binary --only-section=.text --only-section=.rodata \
	           --only-section=.data $< $@
	@echo built $@

clean:
	rm -f start.o main.o eabi_rest.o kernel.elf kernel.map $(OUT_BIN)

.PHONY: all clean
