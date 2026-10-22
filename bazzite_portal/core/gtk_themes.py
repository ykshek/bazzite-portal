"""Discovery of installed GTK 4 themes."""
import os
from pathlib import Path


_EXCLUDED_THEMES = {
    "Adwaita", "Adwaita-dark", "Adwaita-empty", "Default", "Empty",
    "HighContrast", "HighContrastInverse",
}


def list_gtk4_themes(environ=None, home=None):
    """Return installed theme directory names that provide GTK 4 styles."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    data_home = Path(environ.get("XDG_DATA_HOME") or home / ".local/share")
    data_dirs = (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(os.pathsep)
    search_dirs = [home / ".themes", data_home / "themes"]
    search_dirs.extend(Path(directory) / "themes" for directory in data_dirs if directory)

    names = set()
    for themes_dir in search_dirs:
        try:
            entries = themes_dir.iterdir()
            for entry in entries:
                if entry.name not in _EXCLUDED_THEMES and (entry / "gtk-4.0").is_dir():
                    names.add(entry.name)
        except OSError:
            continue
    return sorted(names, key=str.casefold)
