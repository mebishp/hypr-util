# Installation

```
./setup.sh
```

Installs into `/usr/local` (override with `PREFIX=...`), then restarts the
services so what is running is what was just installed. Re-run it after
`git pull` -- the installed copy is what executes, so changes are not live
until you do.

It also builds the `hyprkbd` keyboard lighting module through DKMS, which
needs `dkms` and your kernel's headers. If either is missing it says so and
carries on: everything except the Laptop keyboard page works without it.

```
./uninstall.sh
```

Removes everything `setup.sh` installed. Settings in `~/.config/hypr-util`
are kept.

## Building the kernel module by hand

`setup.sh` does this for you, but the module is a normal out-of-tree build:

```
cd kernel/hyprkbd && make && sudo insmod hyprkbd.ko
```

It needs your kernel's headers, and it follows whatever compiler built the
running kernel -- a Clang-built kernel (CachyOS ships one) rejects a
GCC-built module.

## Desktop environment

Nothing here is GNOME-specific -- see
[Desktop environment compatibility](desktop-environments.md) for the one
feature that is, and why everything else isn't.
