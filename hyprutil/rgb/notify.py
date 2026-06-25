"""Brief lighting 'flash' notifications: show a color/effect momentarily,
then revert to whatever preset was active before -- used to visually
indicate a power profile change without permanently altering the keyboard's
lighting."""
import time

from . import controller, presets

# Distinct color per profile so the flash also tells you *which* profile it
# switched to, not just that something changed.
PROFILE_FLASH_COLORS = {
    "power-saver": "00ff00",
    "balanced": "ffff00",
    "performance": "ff0000",
}


def flash(effect, color, duration=3.5, color_idx=7, brightness=controller.DEFAULT_BRIGHTNESS):
    """Apply effect/color immediately, wait duration seconds, then revert to
    whichever preset was last active (no-op if none is known yet). Blocks
    for the full duration -- call this from a background thread in any UI
    context.

    Defaults (breathe, color_idx=7, 3.5s) were picked by hand via
    test_flash.py: "static"/color_idx=0 with "breathe" renders a fixed
    white/blue instead of the requested color -- color_idx=7 (cycle mode,
    repeating the same color 7x) is what actually honors the custom color."""
    colors = [color] * 7
    controller.apply(effect, colors, color_idx, brightness)
    time.sleep(duration)
    _revert()


def _revert():
    slot = presets.active_preset()
    if slot is not None:
        presets.apply_preset(slot)


def flash_for_profile(profile, effect="breathe", duration=3.5):
    color = PROFILE_FLASH_COLORS.get(profile, "ffffff")
    flash(effect, color, duration)
