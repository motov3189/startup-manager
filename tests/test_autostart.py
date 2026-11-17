from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autostart  # noqa: E402


class AutostartTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.user_dir = self.root / "config" / "autostart"
        self.system_dir = self.root / "etc" / "autostart"
        self.user_dir.mkdir(parents=True)
        self.system_dir.mkdir(parents=True)
        self.saved_env = {
            key: os.environ.get(key)
            for key in ("XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "XDG_CURRENT_DESKTOP", "LANG")
        }
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        os.environ["XDG_CONFIG_DIRS"] = str(self.root / "etc")
        os.environ["XDG_CURRENT_DESKTOP"] = "ubuntu:GNOME"
        os.environ["LANG"] = "zh_CN.UTF-8"

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def write(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def scan(self, include_system: bool = True):
        return autostart.scan_entries(
            include_system=include_system,
            user_dir=self.user_dir,
            system_dirs=[self.system_dir],
        )

    def test_reads_delay_and_embedded_sleep(self):
        self.write(
            self.user_dir,
            "nutstore.desktop",
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Nutstore\n"
            "Exec=sh -c \"(sleep 30 && exec ~/.nutstore/dist/bin/nutstore-pydaemon.py)\"\n",
        )
        entries = self.scan(include_system=False)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.name, "Nutstore")
        self.assertEqual(entry.delay, 0)
        self.assertEqual(entry.embedded_delay, 30.0)
        self.assertTrue(entry.enabled)

    def test_enabled_defaults_to_true_and_explicit_false_wins(self):
        self.write(
            self.user_dir,
            "a.desktop",
            "[Desktop Entry]\nType=Application\nName=A\nExec=a\n",
        )
        self.write(
            self.user_dir,
            "b.desktop",
            "[Desktop Entry]\nType=Application\nName=B\nExec=b\n"
            "X-GNOME-Autostart-enabled=false\n",
        )
        entries = {entry.name: entry for entry in self.scan(include_system=False)}
        self.assertTrue(entries["A"].enabled)
        self.assertFalse(entries["B"].enabled)

    def test_system_entries_are_hidden_by_default_and_hidden_files_are_skipped(self):
        self.write(
            self.system_dir,
            "daemon.desktop",
            "[Desktop Entry]\nType=Application\nName=Daemon\nExec=daemon\n",
        )
        self.write(
            self.system_dir,
            "gone.desktop",
            "[Desktop Entry]\nType=Application\nName=Gone\nExec=gone\nHidden=true\n",
        )
        self.assertEqual(self.scan(include_system=False), [])
        names = [entry.name for entry in self.scan(include_system=True)]
        self.assertEqual(names, ["Daemon"])

    def test_localized_user_name_overrides_system_name(self):
        self.write(
            self.system_dir,
            "nutstore.desktop",
            "[Desktop Entry]\nType=Application\nName=Nutstore\n"
            "Name[zh_CN]=坚果云\nExec=nutstore\n",
        )
        self.write(
            self.user_dir,
            "nutstore.desktop",
            "[Desktop Entry]\nType=Application\nName=我的坚果云\n",
        )
        entry = self.scan(include_system=True)[0]
        self.assertEqual(entry.name, "我的坚果云")
        self.assertTrue(entry.is_override)

    def test_minimal_hidden_override_keeps_system_name(self):
        self.write(
            self.system_dir,
            "app.desktop",
            "[Desktop Entry]\nType=Application\nName=App\nExec=app\n",
        )
        self.write(
            self.user_dir,
            "app.desktop",
            "[Desktop Entry]\nHidden=true\n",
        )
        entry = self.scan(include_system=True)[0]
        self.assertEqual(entry.name, "App")
        self.assertFalse(entry.enabled)
        self.assertTrue(entry.is_override)

    def test_toggle_system_entry_creates_full_override_and_can_be_removed(self):
        system_path = self.write(
            self.system_dir,
            "app.desktop",
            "[Desktop Entry]\nType=Application\nName=App\nExec=app --flag\n"
            "# keep this comment\n",
        )
        entry = self.scan(include_system=True)[0]
        autostart.set_enabled(entry, False)
        override = self.user_dir / "app.desktop"
        self.assertTrue(override.exists())
        text = override.read_text(encoding="utf-8")
        self.assertIn("Exec=app --flag", text)
        self.assertIn("# keep this comment", text)
        self.assertIn("X-GNOME-Autostart-enabled=false", text)
        entry = self.scan(include_system=True)[0]
        self.assertFalse(entry.enabled)
        self.assertTrue(entry.is_override)
        autostart.delete_entry(entry)
        self.assertFalse(override.exists())
        entry = self.scan(include_system=True)[0]
        self.assertTrue(entry.enabled)
        self.assertEqual(entry.system_path, system_path)

    def test_delay_zero_is_written_explicitly_to_override_system_delay(self):
        self.write(
            self.system_dir,
            "app.desktop",
            "[Desktop Entry]\nType=Application\nName=App\nExec=app\n"
            "X-GNOME-Autostart-Delay=12\n",
        )
        entry = self.scan(include_system=True)[0]
        autostart.set_delay(entry, 0)
        text = (self.user_dir / "app.desktop").read_text(encoding="utf-8")
        self.assertIn("X-GNOME-Autostart-Delay=0", text)
        entry = self.scan(include_system=True)[0]
        self.assertEqual(entry.delay, 0)

    def test_update_fields_replaces_localized_name_and_comment(self):
        self.write(
            self.system_dir,
            "app.desktop",
            "[Desktop Entry]\nType=Application\nName=App\n"
            "Name[zh_CN]=应用\nComment=Old\nComment[zh_CN]=旧备注\n"
            "Icon=old-icon\nExec=app\nX-GNOME-Autostart-Delay=1\n",
        )
        entry = self.scan(include_system=True)[0]
        autostart.update_fields(
            entry,
            name="新名字",
            command="/usr/bin/app --new",
            comment="新备注",
            icon="",
            delay=7,
            enabled=False,
        )
        text = (self.user_dir / "app.desktop").read_text(encoding="utf-8")
        self.assertIn("Name=新名字", text)
        self.assertNotIn("Name[zh_CN]", text)
        self.assertIn("Comment=新备注", text)
        self.assertNotIn("Comment[zh_CN]", text)
        self.assertIn("Icon=\n", text)
        self.assertIn("Exec=/usr/bin/app --new", text)
        self.assertIn("X-GNOME-Autostart-Delay=7", text)
        self.assertIn("X-GNOME-Autostart-enabled=false", text)
        entry = self.scan(include_system=True)[0]
        self.assertEqual(entry.name, "新名字")
        self.assertEqual(entry.comment, "新备注")
        self.assertEqual(entry.delay, 7)
        self.assertFalse(entry.enabled)

    def test_clearing_comment_and_icon_masks_system_values(self):
        self.write(
            self.system_dir,
            "app.desktop",
            "[Desktop Entry]\nType=Application\nName=App\nExec=app\n"
            "Comment=System comment\nIcon=system-icon\n",
        )
        entry = self.scan(include_system=True)[0]
        autostart.update_fields(
            entry,
            name="App",
            command="app",
            comment="",
            icon="",
            delay=0,
            enabled=True,
        )
        text = (self.user_dir / "app.desktop").read_text(encoding="utf-8")
        self.assertIn("Comment=\n", text)
        self.assertIn("Icon=\n", text)
        entry = self.scan(include_system=True)[0]
        self.assertEqual(entry.comment, "")
        self.assertEqual(entry.icon, "")

    def test_create_entry_creates_unique_ids_and_desktop_escapes(self):
        first = autostart.create_entry(
            name="Clock\nLine",
            command="/usr/bin/clock --label=a=b",
            comment="测试",
            delay=4,
            enabled=False,
            directory=self.user_dir,
        )
        second = autostart.create_entry(
            name="Second",
            command="second",
            directory=self.user_dir,
        )
        self.assertNotEqual(first, second)
        text = first.read_text(encoding="utf-8")
        self.assertIn("Name=Clock\\nLine", text)
        self.assertIn("X-GNOME-Autostart-Delay=4", text)
        self.assertIn("X-GNOME-Autostart-enabled=false", text)
        entries = {entry.name: entry for entry in self.scan(include_system=False)}
        self.assertIn("Clock\nLine", entries)
        self.assertFalse(entries["Clock\nLine"].enabled)

    def test_only_show_in_and_not_show_in(self):
        self.write(
            self.system_dir,
            "kde.desktop",
            "[Desktop Entry]\nType=Application\nName=KDE\nExec=kde\n"
            "OnlyShowIn=KDE;\n",
        )
        self.write(
            self.system_dir,
            "not-gnome.desktop",
            "[Desktop Entry]\nType=Application\nName=NoGNOME\nExec=x\n"
            "NotShowIn=GNOME;\n",
        )
        self.assertEqual(self.scan(include_system=True), [])

    def test_update_desktop_text_adds_missing_section_and_keys(self):
        text = autostart.update_desktop_text(
            "# comment\n[Desktop Entry]\nName=App\n", {"Exec": "app", "Hidden": None}
        )
        self.assertIn("# comment", text)
        self.assertIn("Name=App", text)
        self.assertIn("Exec=app", text)
        self.assertNotIn("Hidden=", text)
        created = autostart.update_desktop_text("", {"Name": "App", "Exec": "app"})
        self.assertTrue(created.startswith("[Desktop Entry]\n"))

    def test_command_candidates_understand_shell_and_scripts(self):
        candidates = autostart.command_candidates(
            "sh -c \"(sleep 30 && exec ~/.nutstore/dist/bin/nutstore-pydaemon.py)\""
        )
        self.assertTrue(
            any(item.endswith("nutstore-pydaemon.py") for item in candidates)
        )
        script = self.root / "launcher"
        script.write_text(
            '#!/bin/sh\nexec /usr/bin/python3 /tmp/example-app.py "$@"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        candidates = autostart.command_candidates(str(script))
        self.assertTrue(any(item.endswith("example-app.py") for item in candidates))

    def test_entry_is_running_matches_command_and_script(self):
        entry = autostart.AutostartEntry(
            desktop_id="telegram.desktop",
            system_data={
                "Type": "Application",
                "Name": "Telegram",
                "Exec": "/opt/Telegram/Telegram",
            },
        )
        self.assertTrue(
            autostart.entry_is_running(
                entry,
                [("telegram", ["/opt/Telegram/Telegram"], "/opt/Telegram/Telegram")],
            )
        )
        self.assertFalse(
            autostart.entry_is_running(entry, [("bash", ["/bin/bash"], "/bin/bash")])
        )

    def test_scan_applies_user_override_to_system_entry(self):
        self.write(
            self.system_dir,
            "app.desktop",
            "[Desktop Entry]\nType=Application\nName=App\nExec=app\n"
            "X-GNOME-Autostart-Delay=3\n",
        )
        self.write(
            self.user_dir,
            "app.desktop",
            "[Desktop Entry]\nType=Application\nName=My App\n"
            "X-GNOME-Autostart-enabled=false\n",
        )
        entry = self.scan(include_system=True)[0]
        self.assertEqual(entry.name, "My App")
        self.assertEqual(entry.delay, 3)
        self.assertFalse(entry.enabled)
        self.assertTrue(entry.is_override)


if __name__ == "__main__":
    unittest.main()
