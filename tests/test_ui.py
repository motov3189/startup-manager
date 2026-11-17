from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import startup_manager as sm  # noqa: E402


def pump() -> None:
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


@unittest.skipUnless(os.environ.get("DISPLAY"), "GTK test needs a display")
class StartupManagerUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.user_dir = self.root / "config" / "autostart"
        self.system_dir = self.root / "etc" / "autostart"
        self.user_dir.mkdir(parents=True)
        self.system_dir.mkdir(parents=True)
        (self.user_dir / "alpha.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Alpha\n"
            "Comment=First app\nExec=alpha --start\n"
            "X-GNOME-Autostart-Delay=2\n",
            encoding="utf-8",
        )
        (self.user_dir / "beta.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Beta\n"
            "Comment=Second app\nExec=beta\n"
            "X-GNOME-Autostart-enabled=false\n",
            encoding="utf-8",
        )
        (self.system_dir / "system.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=System App\n"
            "Exec=system-app\n",
            encoding="utf-8",
        )
        self.saved_env = {
            key: os.environ.get(key)
            for key in ("XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "XDG_CURRENT_DESKTOP")
        }
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        os.environ["XDG_CONFIG_DIRS"] = str(self.root / "etc")
        os.environ["XDG_CURRENT_DESKTOP"] = "ubuntu:GNOME"
        self.window = sm.StartupManagerWindow(None)
        self.window.show_all()
        pump()

    def tearDown(self):
        self.window.destroy()
        pump()
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_lists_user_entries_and_hides_system_by_default(self):
        self.assertEqual(len(self.window.entries), 2)
        self.assertEqual(len(self.window.rows), 2)
        self.assertIn("共 2 项", self.window.footer_left.get_text())
        self.assertIn("系统项已隐藏", self.window.footer_right.get_text())

    def test_search_filters_rows(self):
        self.window.search_entry.set_text("alpha")
        self.window._on_search_changed(self.window.search_entry)
        pump()
        visible = [row for row in self.window.rows if row.get_visible()]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].entry.name, "Alpha")
        self.window.search_entry.set_text("missing")
        self.window._on_search_changed(self.window.search_entry)
        pump()
        self.assertEqual(
            [row for row in self.window.rows if row.get_visible()], []
        )
        self.assertTrue(self.window.empty_box.get_visible())

    def test_toggle_and_delay_write_desktop_files(self):
        entry = next(item for item in self.window.entries if item.name == "Alpha")
        self.window.change_enabled(entry, False)
        text = (self.user_dir / "alpha.desktop").read_text(encoding="utf-8")
        self.assertIn("X-GNOME-Autostart-enabled=false", text)
        self.window.change_delay(entry, 11)
        text = (self.user_dir / "alpha.desktop").read_text(encoding="utf-8")
        self.assertIn("X-GNOME-Autostart-Delay=11", text)
        self.window.reload()
        entry = next(item for item in self.window.entries if item.name == "Alpha")
        self.assertFalse(entry.enabled)
        self.assertEqual(entry.delay, 11)

    def test_enabling_system_entry_creates_override(self):
        self.window.apply_settings(
            {**self.window.settings.data, "show_system": True}
        )
        entry = next(
            item for item in self.window.entries if item.name == "System App"
        )
        self.assertFalse(entry.is_user)
        self.window.change_enabled(entry, False)
        override = self.user_dir / "system.desktop"
        self.assertTrue(override.exists())
        self.assertIn(
            "X-GNOME-Autostart-enabled=false",
            override.read_text(encoding="utf-8"),
        )
        self.window.reload()
        entry = next(
            item for item in self.window.entries if item.name == "System App"
        )
        self.assertTrue(entry.is_override)
        self.assertFalse(entry.enabled)

    def test_settings_are_persisted(self):
        data = {
            **self.window.settings.data,
            "show_system": True,
            "sort": "name",
            "refresh_seconds": 10,
        }
        self.window.apply_settings(data)
        saved = json.loads(
            (self.root / "config" / "startup-manager" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(saved["show_system"])
        self.assertEqual(saved["sort"], "name")
        self.assertEqual(saved["refresh_seconds"], 10)
        self.assertEqual(len(self.window.entries), 3)

    def test_dialogs_construct_without_touching_files(self):
        entry = next(item for item in self.window.entries if item.name == "Alpha")
        editor = sm.EntryEditor(self.window, entry)
        editor.show_all()
        pump()
        self.assertEqual(editor.name_entry.get_text(), "Alpha")
        editor.destroy()
        pump()
        settings = sm.SettingsDialog(self.window)
        settings.show_all()
        pump()
        settings.destroy()
        pump()

    def test_new_entry_dialog_creates_desktop_file(self):
        editor = sm.EntryEditor(self.window, None)
        editor.show_all()
        editor.name_entry.set_text("Gamma")
        editor.command_entry.set_text("gamma --tray")
        editor.comment_entry.set_text("Third app")
        editor.delay_spin.set_value(6)
        editor.emit("response", Gtk.ResponseType.OK)
        pump()
        files = list(self.user_dir.glob("custom-*.desktop"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("Name=Gamma", text)
        self.assertIn("Exec=gamma --tray", text)
        self.assertIn("X-GNOME-Autostart-Delay=6", text)
        self.window.reload()
        names = [entry.name for entry in self.window.entries]
        self.assertIn("Gamma", names)

    def test_editing_system_entry_creates_user_override(self):
        self.window.apply_settings(
            {**self.window.settings.data, "show_system": True}
        )
        entry = next(
            item for item in self.window.entries if item.name == "System App"
        )
        original = (self.system_dir / "system.desktop").read_text(encoding="utf-8")
        editor = sm.EntryEditor(self.window, entry)
        editor.show_all()
        editor.name_entry.set_text("My System App")
        editor.command_entry.set_text("system-app --custom")
        editor.emit("response", Gtk.ResponseType.OK)
        pump()
        override = self.user_dir / "system.desktop"
        self.assertTrue(override.exists())
        text = override.read_text(encoding="utf-8")
        self.assertIn("Name=My System App", text)
        self.assertIn("Exec=system-app --custom", text)
        self.assertEqual(
            (self.system_dir / "system.desktop").read_text(encoding="utf-8"),
            original,
        )
        self.window.reload()
        entry = next(
            item for item in self.window.entries if item.name == "My System App"
        )
        self.assertTrue(entry.is_override)

    def test_settings_dialog_saves_and_reloads(self):
        dialog = sm.SettingsDialog(self.window)
        dialog.show_all()
        dialog.system_switch.set_active(True)
        dialog.sort_combo.set_active_id("name")
        dialog.refresh_combo.set_active_id("10")
        dialog.emit("response", Gtk.ResponseType.OK)
        pump()
        saved = json.loads(
            (self.root / "config" / "startup-manager" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(saved["show_system"])
        self.assertEqual(saved["sort"], "name")
        self.assertEqual(saved["refresh_seconds"], 10)
        self.assertEqual(len(self.window.entries), 3)


if __name__ == "__main__":
    unittest.main()
