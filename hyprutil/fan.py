"""Shared, toolkit-independent backend logic for the fan daemon and tray/app UIs."""
import subprocess
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "hypr-util"
CURVES_DIR = CONFIG_DIR / "curves"
OVERRIDE_FILE = CONFIG_DIR / "override"
SERVICE = "fancurve.service"

PROFILES = ["power-saver", "balanced", "performance"]
PROFILE_LABELS = {"power-saver": "Eco", "balanced": "Balanced", "performance": "Performance"}
PROFILE_REFRESH_HZ = {"power-saver": 60, "balanced": 165, "performance": 165}
DEFAULT_CURVES = {
    "power-saver": [(35, 0), (45, 100), (60, 150), (70, 200), (80, 255)],
    "balanced": [(35, 0), (40, 150), (50, 180), (65, 220), (75, 255)],
    "performance": [(30, 0), (35, 140), (45, 190), (55, 230), (65, 255)],
}
CURVE_FILENAMES = {"power-saver": "eco", "balanced": "balanced", "performance": "performance"}


def find_hwmon_by_name(name):
    for d in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (d / "name").read_text().strip() == name:
                return d
        except OSError:
            pass
    return None


HP_HWMON = find_hwmon_by_name("hp")
CPU_HWMON = find_hwmon_by_name("k10temp")


def read_int(path, default=None):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return default


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
    curve_path(profile).write_text("\n".join(lines) + "\n")


def read_override():
    if OVERRIDE_FILE.exists():
        return OVERRIDE_FILE.read_text().strip()
    return "auto"


def write_override(value):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OVERRIDE_FILE.write_text(f"{value}\n")


def current_power_profile():
    r = subprocess.run(["powerprofilesctl", "get"], capture_output=True, text=True)
    return r.stdout.strip() or "balanced"


def set_power_profile(profile):
    subprocess.run(["powerprofilesctl", "set", profile])


def service_active():
    r = subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True, text=True)
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
