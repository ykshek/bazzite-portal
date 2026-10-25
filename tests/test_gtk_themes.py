import tempfile
import unittest
from pathlib import Path

from bazzite_portal.core.gtk_themes import (
    find_gtk4_theme_stylesheet,
    list_gtk4_themes,
    should_use_server_side_decorations,
)
from bazzite_portal.core.storage import Store


class GtkThemeTests(unittest.TestCase):
    def test_finds_theme_stylesheet_and_dark_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            theme_dir = root / "themes" / "Breeze" / "gtk-4.0"
            theme_dir.mkdir(parents=True)
            light = theme_dir / "gtk.css"
            dark = theme_dir / "gtk-dark.css"
            light.write_text("/* light */")
            dark.write_text("/* dark */")
            environ = {"XDG_DATA_HOME": str(root), "XDG_DATA_DIRS": ""}

            self.assertEqual(
                dark,
                find_gtk4_theme_stylesheet("Breeze", dark=True, environ=environ),
            )
            self.assertEqual(
                light,
                find_gtk4_theme_stylesheet("Breeze", dark=False, environ=environ),
            )

            dark.unlink()
            self.assertEqual(
                light,
                find_gtk4_theme_stylesheet("Breeze", dark=True, environ=environ),
            )

    def test_server_decorations_require_kde_and_non_adwaita_theme(self):
        kde = {"XDG_CURRENT_DESKTOP": "KDE"}
        plasma = {"XDG_CURRENT_DESKTOP": "KDE:Plasma"}
        gnome = {"XDG_CURRENT_DESKTOP": "GNOME"}

        self.assertTrue(should_use_server_side_decorations("Breeze", kde))
        self.assertTrue(should_use_server_side_decorations("Breeze", plasma))
        self.assertFalse(should_use_server_side_decorations("Adwaita", kde))
        self.assertFalse(should_use_server_side_decorations("Adwaita-dark", kde))
        self.assertFalse(should_use_server_side_decorations("Breeze", gnome))
        self.assertFalse(should_use_server_side_decorations("", kde))

    def test_lists_gtk4_themes_from_xdg_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            data_home = root / "data-home"
            system_data = root / "system-data"
            theme_paths = (
                home / ".themes" / "Orchis",
                data_home / "themes" / "Breeze",
                system_data / "themes" / "Orchis",
                system_data / "themes" / "Legacy",
                system_data / "themes" / "Adwaita",
            )
            for theme_path in theme_paths:
                theme_path.mkdir(parents=True)
            for name in ("Orchis", "Breeze", "Adwaita"):
                for theme_path in theme_paths:
                    if theme_path.name == name:
                        (theme_path / "gtk-4.0").mkdir()

            self.assertEqual(
                ["Breeze", "Orchis"],
                list_gtk4_themes(
                    environ={
                        "XDG_DATA_HOME": str(data_home),
                        "XDG_DATA_DIRS": str(system_data),
                    },
                    home=home,
                ),
            )

    def test_theme_preference_round_trips_and_rejects_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            self.assertTrue(store.set_preference("gtk_theme", "Breeze"))
            self.assertTrue(store.set_preference("color_scheme", "dark"))
            self.assertEqual("Breeze", store.preferences()["gtk_theme"])
            self.assertEqual("dark", store.preferences()["color_scheme"])
            self.assertFalse(store.set_preference("gtk_theme", "../Breeze"))
            self.assertFalse(store.set_preference("gtk_theme", ""))
            self.assertFalse(store.set_preference("color_scheme", "sepia"))


if __name__ == "__main__":
    unittest.main()
