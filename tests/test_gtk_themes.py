import tempfile
import unittest
from pathlib import Path

from bazzite_portal.core.gtk_themes import list_gtk4_themes
from bazzite_portal.core.storage import Store


class GtkThemeTests(unittest.TestCase):
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
