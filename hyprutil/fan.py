"""Shared, toolkit-independent backend logic for the fan daemon and tray/app UIs."""
import subprocess
from pathlib import Path

from .util import CONFIG_DIR, atomic_write_text

SUBPROCESS_TIMEOUT = 3

CURVES_DIR = CONFIG_DIR / "curves"
OVERRIDE_FILE = CONFIG_DIR / "override"
SERVICE = "hypr-util-fancurve.service"

PROFILES = ["power-saver", "balanced", "performance"]
PROFILE_LABELS = {"power-saver": "Eco", "balanced": "Balanced", "performance": "Performance"}
PROFILE_REFRESH_HZ = {"power-saver": 60, "balanced": 165, "performance": 165}
DEFAULT_CURVES = {
    "power-saver": [(35, 0), (45, 100), (60, 150), (70, 200), (80, 255)],
    "balanced": [(35, 0), (40, 150), (50, 180), (65, 220), (75, 255)],
    "performance": [(30, 0), (35, 140), (45, 190), (55, 230), (65, 255)],
}
CURVE_FILENAMES = {"power-saver": "eco", "balanced": "balanced", "performance": "performance"}


# CPU package sensors, best first: k10temp for AMD, zenpower for the
# out-of-tree Zen module some people run instead, coretemp for Intel (whose
# temp1 is "Package id 0", as k10temp's is Tctl). Nothing generic goes here --
# the daemon takes pwm1 out of firmware auto mode, so a sensor like acpitz
# that can read flat under load would hold the fan down over a hot CPU.
CPU_HWMON_NAMES = ("k10temp", "zenpower", "coretemp")


def find_hwmon_by_name(name):
    for d in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (d / "name").read_text().strip() == name:
                return d
        except OSError:
            pass
    return None


def read_int(path, default=None):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return default


def find_cpu_hwmon():
    """The first CPU package sensor that is present and reads a temperature."""
    for name in CPU_HWMON_NAMES:
        hwmon = find_hwmon_by_name(name)
        if hwmon and read_int(hwmon / "temp1_input") is not None:
            return hwmon
    return None


HP_HWMON = find_hwmon_by_name("hp")
CPU_HWMON = find_cpu_hwmon()


def read_status():
    temp = read_int(CPU_HWMON / "temp1_input", 0) / 1000 if CPU_HWMON else None
    pwm = read_int(HP_HWMON / "pwm1", 0) if HP_HWMON else None
    fan1 = read_int(HP_HWMON / "fan1_input", 0) if HP_HWMON else None
    fan2 = read_int(HP_HWMON / "fan2_input", 0) if HP_HWMON else None
    return {"temp": temp, "pwm": pwm, "fan1": fan1, "fan2": fan2}


def curve_path(profile):
    return CURVES_DIR / f"{CURVE_FILENAMES[profile]}.conf"


def read_curve(profile):
    path = curve_path(profile)
    points = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t, p = line.split()
                points.append((int(t), int(p)))
            except ValueError:
                continue
    return points or DEFAULT_CURVES[profile]


def write_curve(profile, points):
    CURVES_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"{t} {p}" for t, p in sorted(points)]
    atomic_write_text(curve_path(profile), "\n".join(lines) + "\n")


def read_override():
    if OVERRIDE_FILE.exists():
        return OVERRIDE_FILE.read_text().strip()
    return "auto"


def write_override(value):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(OVERRIDE_FILE, f"{value}\n")


def current_power_profile():
    try:
        r = subprocess.run(
            ["powerprofilesctl", "get"], capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError):
        return "balanced"
    return r.stdout.strip() or "balanced"


def set_power_profile(profile):
    try:
        subprocess.run(["powerprofilesctl", "set", profile], timeout=SUBPROCESS_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        pass


def service_active():
    try:
        r = subprocess.run(
            ["systemctl", "is-active", SERVICE], capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.stdout.strip() == "active"


def service_action(action):
    subprocess.Popen(["pkexec", "systemctl", action, SERVICE])


def ensure_config_defaults():
    CURVES_DIR.mkdir(parents=True, exist_ok=True)
    for profile in PROFILES:
        if not curve_path(profile).exists():
            write_curve(profile, DEFAULT_CURVES[profile])
    if not OVERRIDE_FILE.exists():
        write_override("auto")
