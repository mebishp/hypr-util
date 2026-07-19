"""Small helpers shared across the fan and RGB backends."""
import os
import tempfile
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "hypr-util"


def atomic_write_text(path, text):
    """Write text to path without ever leaving a truncated file behind.

    Multiple processes (tray, GUI, automation daemon) read/write these same
    config files; a plain write_text() truncates the file before writing the
    new contents, so a reader -- or a writer racing with another writer --
    can observe a half-written or empty file. Writing to a temp file in the
    same directory and renaming over the target is atomic on POSIX (rename
    within a filesystem), so readers only ever see the old or the new
    content, never a partial one.
    """
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
