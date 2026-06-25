"""Event-driven presence detection for the Firefly RGB keyboard.

Uses udev (netlink uevents) instead of polling -- the OS tells us the moment
the device is plugged or unplugged, so there's no recurring check, no
subprocess spawning, and no wasted work while it's absent.
"""
import pyudev

VENDOR_ID = 0x04D9
PRODUCT_ID = 0xA1CD


def _matches(device):
    # Only one event per physical device (not per-interface): the "PRODUCT"
    # property is the uevent's own embedded data, unlike device.attributes
    # which reads live sysfs and is already gone by the time "remove" fires.
    if device.properties.get("DEVTYPE") != "usb_device":
        return False
    product = device.properties.get("PRODUCT")
    if not product:
        return False
    parts = product.split("/")
    if len(parts) < 2:
        return False
    try:
        vendor, prod = int(parts[0], 16), int(parts[1], 16)
    except ValueError:
        return False
    return vendor == VENDOR_ID and prod == PRODUCT_ID


def is_connected_now():
    context = pyudev.Context()
    for device in context.list_devices(subsystem="usb"):
        if _matches(device):
            return True
    return False


class KeyboardWatcher:
    """Tracks connection state, calling on_change(bool) from a background
    thread whenever it changes. Callers must hop back to their own toolkit's
    main loop inside on_change before touching UI (e.g. GLib.idle_add,
    QMetaObject.invokeMethod) -- this runs on udev's monitor thread.
    """

    def __init__(self, on_change=None):
        self.on_change = on_change
        self.connected = is_connected_now()
        self._context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._context)
        self._monitor.filter_by(subsystem="usb")
        self._observer = pyudev.MonitorObserver(self._monitor, self._handle_event)

    def start(self):
        self._observer.start()

    def stop(self):
        self._observer.stop()

    def _handle_event(self, action, device):
        if action not in ("add", "remove"):
            return
        if not _matches(device):
            return
        new_state = action == "add"
        if new_state != self.connected:
            self.connected = new_state
            if self.on_change:
                self.on_change(new_state)
