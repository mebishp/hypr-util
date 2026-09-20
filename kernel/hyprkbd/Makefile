# Out-of-tree build for the hyprkbd module.
#
#   make            build against the running kernel
#   make clean
#
# DKMS drives this same file through dkms.conf; KERNELRELEASE is set when the
# kernel build system calls us back, which is when obj-m is the only thing
# that matters.
obj-m += hyprkbd.o

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build
PWD  := $(shell pwd)

# A Clang-built kernel (CachyOS ships one) rejects a GCC-built module: the
# two disagree on vermagic, and half the kernel's CFLAGS are Clang-only
# anyway. Follow whatever built the running kernel.
LLVM ?= $(shell grep -iq clang /proc/version && echo 1)

all:
	$(MAKE) $(if $(LLVM),LLVM=1) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) $(if $(LLVM),LLVM=1) -C $(KDIR) M=$(PWD) clean

.PHONY: all clean
