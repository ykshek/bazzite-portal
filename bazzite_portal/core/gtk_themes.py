"""Discovery of installed GTK 4 themes."""
import os
import re
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


def find_gtk4_theme_stylesheet(theme_name, dark=False, environ=None, home=None):
    """Find an installed GTK 4 theme stylesheet, preferring the dark variant."""
    if not theme_name:
        return None

    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    data_home = Path(environ.get("XDG_DATA_HOME") or home / ".local/share")
    data_dirs = (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(os.pathsep)
    themes_dirs = [home / ".themes", data_home / "themes"]
    themes_dirs.extend(Path(directory) / "themes" for directory in data_dirs if directory)
    candidates = ("gtk-dark.css", "gtk.css") if dark else ("gtk.css",)

    for themes_dir in themes_dirs:
        theme_dir = themes_dir / theme_name / "gtk-4.0"
        for filename in candidates:
            stylesheet = theme_dir / filename
            if stylesheet.is_file():
                return stylesheet
    return None


def should_use_server_side_decorations(theme_name, environ=None):
    """Use KDE's native titlebar for GTK themes other than Adwaita."""
    environ = os.environ if environ is None else environ
    desktop = (environ.get("XDG_CURRENT_DESKTOP", "") + ":" +
               environ.get("DESKTOP_SESSION", "")).casefold()
    tokens = set(re.findall(r"[a-z0-9]+", desktop))
    is_kde = bool(tokens & {"kde", "plasma", "plasmawayland", "plasmax11"})
    theme = str(theme_name or "").strip().casefold()
    is_adwaita = theme == "adwaita" or theme.startswith("adwaita-")
    return is_kde and bool(theme) and not is_adwaita
