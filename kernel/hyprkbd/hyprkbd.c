// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * hyprkbd - four-zone keyboard lighting on HP OMEN and Victus laptops.
 *
 * The colours of the built-in keyboard live behind one ACPI-WMI method:
 * \_SB.WMID.WMAA, under the PNP0C14 device whose GUID is
 * 5FB7F034-2C63-45E9-BE91-3D44E2C707E4. The kernel's own hp-wmi driver
 * speaks that mailbox for fans, the platform profile and the hotkeys, but
 * has no lighting code, and the WMI bus creates no character device for the
 * GUID -- so before this driver the only route from userspace was acpi_call,
 * which means root and means every other ACPI method on the machine along
 * with it.
 *
 * This driver claims no WMI GUID (hp-wmi coexists untouched); it only
 * evaluates the method, exactly as hp-wmi does for its own queries.
 *
 * Wire format, the same one hp_wmi_perform_query builds:
 *
 *	in:	u32 signature 0x55434553 ("SECU")
 *		u32 command
 *		u32 command type
 *		u32 data size
 *		u8  data[max(data size, 128)]
 *	out:	u32 signature passthrough
 *		u32 return code (non-zero = the firmware refused)
 *		u8  data[...]
 *
 * The data block is padded to 128 bytes however little of it means
 * anything; this firmware insists on it, and so does hp-wmi.
 *
 * Lighting commands, all under 0x20009 except the keyboard type:
 *
 *	0x20008/0x2B  out4	[0] = keyboard type byte
 *	0x20009/0x01  out128	[0] bit 0 = lighting supported (weak signal)
 *	0x20009/0x02  out128	the colour table
 *	0x20009/0x03  in128	write the colour table
 *	0x20009/0x04  out128	[0] = backlight byte, bit 7 = lit
 *	0x20009/0x05  in4	{byte,0,0,0}: set the backlight byte
 *
 * Zone i sits at table bytes 25+3i..27+3i as R, G, B. Bytes 0..24 belong to
 * something else and are written back exactly as they were read, which is
 * what the vendor software does.
 *
 * The table is cached, so putting a frame of an animation on the keyboard
 * costs one WMI call rather than a read and a write. Nothing else on the
 * machine writes it; `refresh` re-reads it if something ever does.
 *
 * Copyright (C) 2026 hypr-util
 * Based on the mailbox handling in hp-wmi.c by Matthew Garrett and Anssi
 * Hannula, and on the lighting protocol documented by the Ohman project.
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/acpi.h>
#include <linux/ctype.h>
#include <linux/device.h>
#include <linux/errno.h>
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/types.h>
#include <linux/wmi.h>

#define HPWMI_BIOS_GUID		"5FB7F034-2C63-45E9-BE91-3D44E2C707E4"
#define HPWMI_SIGNATURE		0x55434553

#define HPWMI_CMD_DEFAULT	0x20008
#define HPWMI_CMD_LIGHTING	0x20009

#define OP_KBD_TYPE		0x2B
#define OP_SUPPORTED		0x01
#define OP_COLOR_GET		0x02
#define OP_COLOR_SET		0x03
#define OP_LIGHT_GET		0x04
#define OP_LIGHT_SET		0x05

#define TABLE_SIZE		128
#define COLOR_OFFSET		25
/* The table addresses this many zones before it runs out of room; the
 * keyboard type says how many of them are really wired up. */
#define MAX_ZONES		((TABLE_SIZE - COLOR_OFFSET) / 3)

#define BACKLIGHT_ON		0x80
/* The level bits do nothing on every board measured -- the vendor software
 * writes 100 and dims by scaling the colours, and so does hypr-util. */
#define BACKLIGHT_LEVEL		100

/* Keyboard type byte from 0x20008/0x2B. */
enum kbd_kind {
	KIND_NONE,
	KIND_ZONES,
	KIND_PERKEY,
};

static const char * const kind_names[] = {
	[KIND_NONE]   = "none",
	[KIND_ZONES]  = "zones",
	[KIND_PERKEY] = "perkey",
};

static const char * const type_names[] = {
	"standard layout",
	"four zones with numpad",
	"four zones",
	"per-key RGB",
	"one zone with numpad",
	"one zone",
};

struct bios_args {
	u32 signature;
	u32 command;
	u32 commandtype;
	u32 datasize;
	u8 data[];
};

struct bios_return {
	u32 sigpass;
	u32 return_code;
};

static struct platform_device *hyprkbd_pdev;

static DEFINE_MUTEX(hyprkbd_lock);	/* guards everything below */
static u8 color_table[TABLE_SIZE];
static bool table_valid;
static int kbd_type = -1;
static int kbd_zones;
static enum kbd_kind kbd_kind;
static bool kbd_declared;
/*
 * Which output size this firmware accepts for a command that only writes.
 * The vendor software asks for 4; some boards answer AE_AML_OPERAND_VALUE to
 * that and want 128. Found once, then remembered.
 */
static int write_outsize;

static int encode_outsize_for_pvsz(int outsize)
{
	if (outsize > 4096)
		return -EINVAL;
	if (outsize > 1024)
		return 5;
	if (outsize > 128)
		return 4;
	if (outsize > 4)
		return 3;
	if (outsize > 0)
		return 2;
	return 1;
}

/*
 * One mailbox round trip. @buffer carries @insize bytes in and receives
 * @outsize bytes back. Returns 0, a negative errno, or the firmware's own
 * positive return code.
 */
static int hyprkbd_query(u32 command, u32 commandtype, void *buffer,
			 int insize, int outsize)
{
	struct acpi_buffer input, output = { ACPI_ALLOCATE_BUFFER, NULL };
	struct bios_return *bios_return;
	union acpi_object *obj;
	struct bios_args *args;
	int mid, actual_insize, actual_outsize;
	acpi_status status;
	size_t args_size;
	int ret;

	mid = encode_outsize_for_pvsz(outsize);
	if (mid < 0)
		return mid;

	/* Padded to 128 however small the payload is: a shorter block makes
	 * this firmware fail the call outright. */
	actual_insize = max(insize, TABLE_SIZE);
	args_size = struct_size(args, data, actual_insize);
	args = kzalloc(args_size, GFP_KERNEL);
	if (!args)
		return -ENOMEM;

	args->signature = HPWMI_SIGNATURE;
	args->command = command;
	args->commandtype = commandtype;
	args->datasize = insize;
	if (insize)
		memcpy(args->data, buffer, insize);

	input.length = args_size;
	input.pointer = args;

	status = wmi_evaluate_method(HPWMI_BIOS_GUID, 0, mid, &input, &output);
	kfree(args);
	if (ACPI_FAILURE(status))
		return -EIO;

	obj = output.pointer;
	if (!obj)
		return -EINVAL;
	if (obj->type != ACPI_TYPE_BUFFER ||
	    !obj->buffer.pointer ||
	    obj->buffer.length < sizeof(*bios_return)) {
		kfree(obj);
		return -EINVAL;
	}

	bios_return = (struct bios_return *)obj->buffer.pointer;
	ret = bios_return->return_code;
	if (ret || !outsize) {
		kfree(obj);
		return ret;
	}

	/* A firmware that answers short is not an error: zero the rest and
	 * let the caller decide, the way hp-wmi does. */
	actual_outsize = min_t(int, outsize,
			       obj->buffer.length - sizeof(*bios_return));
	memcpy(buffer, obj->buffer.pointer + sizeof(*bios_return),
	       actual_outsize);
	memset((u8 *)buffer + actual_outsize, 0, outsize - actual_outsize);
	kfree(obj);
	return 0;
}

/*
 * A command that only writes, at whatever output size this firmware takes.
 *
 * The scratch copy is not free but it is necessary: hyprkbd_query returns the
 * firmware's answer in the same buffer it was given, so handing it
 * color_table directly would overwrite our cached table with whatever a
 * write command happens to answer.
 */
static int hyprkbd_write_query(u32 commandtype, void *buffer, int insize)
{
	static const int sizes[] = { 4, TABLE_SIZE };
	int ret = -EIO;
	int i;

	if (write_outsize) {
		u8 scratch[TABLE_SIZE] = {};

		memcpy(scratch, buffer, min(insize, TABLE_SIZE));
		return hyprkbd_query(HPWMI_CMD_LIGHTING, commandtype, scratch,
				     insize, write_outsize);
	}

	for (i = 0; i < (int)ARRAY_SIZE(sizes); i++) {
		u8 scratch[TABLE_SIZE] = {};

		memcpy(scratch, buffer, min(insize, TABLE_SIZE));
		ret = hyprkbd_query(HPWMI_CMD_LIGHTING, commandtype, scratch,
				    insize, sizes[i]);
		if (!ret) {
			write_outsize = sizes[i];
			pr_info("writes use a %d-byte answer on this firmware\n",
				write_outsize);
			return 0;
		}
	}
	return ret;
}

/* -- hardware, all called with hyprkbd_lock held -- */

static int read_table(void)
{
	u8 table[TABLE_SIZE] = {};
	int ret;

	ret = hyprkbd_query(HPWMI_CMD_LIGHTING, OP_COLOR_GET, table,
			    1, TABLE_SIZE);
	if (ret)
		return ret > 0 ? -EIO : ret;

	memcpy(color_table, table, TABLE_SIZE);
	table_valid = true;
	return 0;
}

/* The cached table, read once if we have never seen it. */
static int ensure_table(void)
{
	if (table_valid)
		return 0;
	return read_table();
}

static int write_table(void)
{
	int ret = hyprkbd_write_query(OP_COLOR_SET, color_table, TABLE_SIZE);

	return ret > 0 ? -EIO : ret;
}

static int read_backlight(u8 *value)
{
	u8 data[TABLE_SIZE] = {};
	int ret;

	ret = hyprkbd_query(HPWMI_CMD_LIGHTING, OP_LIGHT_GET, data,
			    1, TABLE_SIZE);
	if (ret)
		return ret > 0 ? -EIO : ret;
	*value = data[0];
	return 0;
}

static int write_backlight(bool on)
{
	u8 data[4] = { BACKLIGHT_LEVEL | (on ? BACKLIGHT_ON : 0), 0, 0, 0 };
	int ret = hyprkbd_write_query(OP_LIGHT_SET, data, sizeof(data));

	return ret > 0 ? -EIO : ret;
}

/*
 * What keyboard is fitted. Two independent questions, and answering only one
 * of them is the mistake to avoid: 0x20009/0x01 says whether there is a
 * controllable backlight at all, 0x20008/0x2B says what the layout is. Type
 * 0 means "standard layout", not "no lighting", so bailing out on it would
 * drop every board that reports a plain layout -- and boards exist (8BCD
 * among them) that refuse every 0x20008 command while answering every
 * lighting one. The colour table reading back is the real proof.
 */
static void detect_keyboard(void)
{
	u8 data[TABLE_SIZE] = {};
	int type = 0;

	kbd_declared = false;
	if (!hyprkbd_query(HPWMI_CMD_LIGHTING, OP_SUPPORTED, data, 1, TABLE_SIZE))
		kbd_declared = data[0] & 1;

	memset(data, 0, sizeof(data));
	if (!hyprkbd_query(HPWMI_CMD_DEFAULT, OP_KBD_TYPE, data, 0, 4)) {
		type = data[0];
	} else {
		memset(data, 0, sizeof(data));
		if (!hyprkbd_query(HPWMI_CMD_DEFAULT, OP_KBD_TYPE, data, 0,
				   TABLE_SIZE))
			type = data[0];
		else
			pr_info("no answer to the keyboard type query; assuming a standard layout\n");
	}
	if (type == 0xFF)	/* -1 from the firmware means the same as 0 */
		type = 0;
	kbd_type = type;

	if (!table_valid) {
		kbd_kind = KIND_NONE;
		kbd_zones = 0;
	} else if (type == 3) {
		/* The firmware answers every lighting call on these boards and
		 * none of it reaches the LEDs: 128 bytes cannot address 176
		 * keys. Reported as such, so userspace offers no dead controls. */
		kbd_kind = KIND_PERKEY;
		kbd_zones = 4;
	} else if (type == 4 || type == 5) {
		kbd_kind = KIND_ZONES;
		kbd_zones = 1;
	} else {
		kbd_kind = KIND_ZONES;
		kbd_zones = 4;
	}
}

/* -- sysfs -- */

static ssize_t keyboard_type_show(struct device *dev,
				  struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%d\n", kbd_type);
}
static DEVICE_ATTR_RO(keyboard_type);

static ssize_t type_name_show(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	if (kbd_type >= 0 && kbd_type < (int)ARRAY_SIZE(type_names))
		return sysfs_emit(buf, "%s\n", type_names[kbd_type]);
	return sysfs_emit(buf, "unknown (%d)\n", kbd_type);
}
static DEVICE_ATTR_RO(type_name);

static ssize_t zones_show(struct device *dev, struct device_attribute *attr,
			  char *buf)
{
	return sysfs_emit(buf, "%d\n", kbd_zones);
}
static DEVICE_ATTR_RO(zones);

static ssize_t kind_show(struct device *dev, struct device_attribute *attr,
			 char *buf)
{
	return sysfs_emit(buf, "%s\n", kind_names[kbd_kind]);
}
static DEVICE_ATTR_RO(kind);

static ssize_t supported_show(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%d\n", kbd_declared);
}
static DEVICE_ATTR_RO(supported);

/*
 * Every zone at once: "rrggbb rrggbb rrggbb rrggbb". This is the attribute an
 * animation writes, and it is one WMI call however many zones change, which
 * is the whole reason it exists next to the per-zone files.
 */
static ssize_t colors_show(struct device *dev, struct device_attribute *attr,
			   char *buf)
{
	int len = 0;
	int i, ret;

	mutex_lock(&hyprkbd_lock);
	ret = ensure_table();
	if (!ret) {
		for (i = 0; i < kbd_zones; i++) {
			const u8 *c = &color_table[COLOR_OFFSET + 3 * i];

			len += sysfs_emit_at(buf, len, "%s%02x%02x%02x",
					     i ? " " : "", c[0], c[1], c[2]);
		}
		len += sysfs_emit_at(buf, len, "\n");
	}
	mutex_unlock(&hyprkbd_lock);
	return ret ? ret : len;
}

static ssize_t colors_store(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	u8 parsed[MAX_ZONES][3];
	const char *p = buf;
	int n = 0;
	int ret, i;

	/* Space- or comma-separated "rrggbb", optionally "#rrggbb". Parsed in
	 * full before anything is written, so a typo in the last zone cannot
	 * leave the first three changed. */
	while (*p && n < MAX_ZONES) {
		unsigned int value;
		char hex[7];

		while (*p == ' ' || *p == ',' || *p == '\t' || *p == '\n')
			p++;
		if (!*p)
			break;
		if (*p == '#')
			p++;
		for (i = 0; i < 6; i++) {
			if (!isxdigit(p[i]))
				return -EINVAL;
			hex[i] = p[i];
		}
		hex[6] = '\0';
		if (kstrtouint(hex, 16, &value))
			return -EINVAL;
		parsed[n][0] = value >> 16;
		parsed[n][1] = value >> 8;
		parsed[n][2] = value;
		n++;
		p += 6;
		if (*p && *p != ' ' && *p != ',' && *p != '\t' && *p != '\n')
			return -EINVAL;
	}
	if (!n)
		return -EINVAL;

	mutex_lock(&hyprkbd_lock);
	ret = ensure_table();
	if (!ret) {
		for (i = 0; i < n; i++)
			memcpy(&color_table[COLOR_OFFSET + 3 * i], parsed[i], 3);
		ret = write_table();
	}
	mutex_unlock(&hyprkbd_lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_RW(colors);

static int zone_index(struct device_attribute *attr)
{
	int zone;

	if (kstrtoint(attr->attr.name + 4, 10, &zone) ||
	    zone < 0 || zone >= MAX_ZONES)
		return -EINVAL;
	return zone;
}

static ssize_t zone_show(struct device *dev, struct device_attribute *attr,
			 char *buf)
{
	int zone = zone_index(attr);
	const u8 *c;
	int ret;

	if (zone < 0)
		return zone;

	mutex_lock(&hyprkbd_lock);
	ret = ensure_table();
	if (!ret) {
		c = &color_table[COLOR_OFFSET + 3 * zone];
		ret = sysfs_emit(buf, "%02x%02x%02x\n", c[0], c[1], c[2]);
	}
	mutex_unlock(&hyprkbd_lock);
	return ret;
}

static ssize_t zone_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	int zone = zone_index(attr);
	unsigned int r, g, b, value;
	int ret;

	if (zone < 0)
		return zone;

	/* "rrggbb", "#rrggbb" or "r g b" -- the last one is what a shell
	 * script that already has three numbers will reach for. */
	if (sscanf(buf, "%u %u %u", &r, &g, &b) == 3) {
		if (r > 255 || g > 255 || b > 255)
			return -EINVAL;
	} else {
		char hex[7];
		int i;
		const char *p = buf + (buf[0] == '#');

		for (i = 0; i < 6; i++) {
			if (!isxdigit(p[i]))
				return -EINVAL;
			hex[i] = p[i];
		}
		hex[6] = '\0';
		if (kstrtouint(hex, 16, &value))
			return -EINVAL;
		r = value >> 16;
		g = (value >> 8) & 0xFF;
		b = value & 0xFF;
	}

	mutex_lock(&hyprkbd_lock);
	ret = ensure_table();
	if (!ret) {
		color_table[COLOR_OFFSET + 3 * zone] = r;
		color_table[COLOR_OFFSET + 3 * zone + 1] = g;
		color_table[COLOR_OFFSET + 3 * zone + 2] = b;
		ret = write_table();
	}
	mutex_unlock(&hyprkbd_lock);
	return ret ? ret : count;
}

static DEVICE_ATTR(zone0, 0644, zone_show, zone_store);
static DEVICE_ATTR(zone1, 0644, zone_show, zone_store);
static DEVICE_ATTR(zone2, 0644, zone_show, zone_store);
static DEVICE_ATTR(zone3, 0644, zone_show, zone_store);

static ssize_t backlight_show(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	u8 value;
	int ret;

	mutex_lock(&hyprkbd_lock);
	ret = read_backlight(&value);
	mutex_unlock(&hyprkbd_lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%d\n", !!(value & BACKLIGHT_ON));
}

static ssize_t backlight_store(struct device *dev,
			       struct device_attribute *attr, const char *buf,
			       size_t count)
{
	bool on;
	int ret;

	if (kstrtobool(buf, &on))
		return -EINVAL;

	mutex_lock(&hyprkbd_lock);
	ret = write_backlight(on);
	mutex_unlock(&hyprkbd_lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_RW(backlight);

static ssize_t backlight_raw_show(struct device *dev,
				  struct device_attribute *attr, char *buf)
{
	u8 value;
	int ret;

	mutex_lock(&hyprkbd_lock);
	ret = read_backlight(&value);
	mutex_unlock(&hyprkbd_lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "0x%02x\n", value);
}
static DEVICE_ATTR_RO(backlight_raw);

/* Drop the cached table and ask the firmware again, and re-detect with it. */
static ssize_t refresh_store(struct device *dev, struct device_attribute *attr,
			     const char *buf, size_t count)
{
	int ret;

	mutex_lock(&hyprkbd_lock);
	table_valid = false;
	ret = read_table();
	detect_keyboard();
	mutex_unlock(&hyprkbd_lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_WO(refresh);

static struct attribute *hyprkbd_attrs[] = {
	&dev_attr_keyboard_type.attr,
	&dev_attr_type_name.attr,
	&dev_attr_zones.attr,
	&dev_attr_kind.attr,
	&dev_attr_supported.attr,
	&dev_attr_colors.attr,
	&dev_attr_zone0.attr,
	&dev_attr_zone1.attr,
	&dev_attr_zone2.attr,
	&dev_attr_zone3.attr,
	&dev_attr_backlight.attr,
	&dev_attr_backlight_raw.attr,
	&dev_attr_refresh.attr,
	NULL,
};

/* Hidden rather than failing: a one-zone board has no zone1..3 to offer. */
static umode_t hyprkbd_attr_visible(struct kobject *kobj,
				    struct attribute *attr, int n)
{
	int zone;

	if (attr == &dev_attr_zone0.attr)
		zone = 0;
	else if (attr == &dev_attr_zone1.attr)
		zone = 1;
	else if (attr == &dev_attr_zone2.attr)
		zone = 2;
	else if (attr == &dev_attr_zone3.attr)
		zone = 3;
	else
		return attr->mode;

	return zone < kbd_zones ? attr->mode : 0;
}

static const struct attribute_group hyprkbd_group = {
	.attrs = hyprkbd_attrs,
	.is_visible = hyprkbd_attr_visible,
};
__ATTRIBUTE_GROUPS(hyprkbd);

static int hyprkbd_probe(struct platform_device *pdev)
{
	return 0;
}

static struct platform_driver hyprkbd_driver = {
	.driver = {
		.name = "hyprkbd",
		/*
		 * dev_groups, not sysfs_create_groups: the driver core creates
		 * these before it sends the ADD uevent, so the udev rule that
		 * hands them to the desktop user cannot race the files into
		 * existence.
		 */
		.dev_groups = hyprkbd_groups,
	},
	.probe = hyprkbd_probe,
};

static int __init hyprkbd_init(void)
{
	int ret;

	if (!wmi_has_guid(HPWMI_BIOS_GUID)) {
		pr_info("no HP BIOS WMI interface here\n");
		return -ENODEV;
	}

	mutex_lock(&hyprkbd_lock);
	ret = read_table();
	if (ret)
		pr_info("the colour table did not read back (%d); reporting no controllable lighting\n",
			ret);
	detect_keyboard();
	mutex_unlock(&hyprkbd_lock);

	ret = platform_driver_register(&hyprkbd_driver);
	if (ret)
		return ret;

	hyprkbd_pdev = platform_device_register_simple("hyprkbd",
						       PLATFORM_DEVID_NONE,
						       NULL, 0);
	if (IS_ERR(hyprkbd_pdev)) {
		platform_driver_unregister(&hyprkbd_driver);
		return PTR_ERR(hyprkbd_pdev);
	}

	pr_info("keyboard type %d (%s), %d zone(s), firmware %s lighting support\n",
		kbd_type, kind_names[kbd_kind], kbd_zones,
		kbd_declared ? "declares" : "declares no");
	return 0;
}

static void __exit hyprkbd_exit(void)
{
	platform_device_unregister(hyprkbd_pdev);
	platform_driver_unregister(&hyprkbd_driver);
}

module_init(hyprkbd_init);
module_exit(hyprkbd_exit);

MODULE_AUTHOR("hypr-util");
MODULE_DESCRIPTION("Four-zone keyboard lighting on HP OMEN and Victus laptops");
MODULE_LICENSE("GPL");
