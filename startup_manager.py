#!/usr/bin/python3
"""Startup Manager: a small GNOME/GTK autostart control panel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autostart  # noqa: E402

APP_ID = "org.alex.StartupManager"
APP_NAME = "启动管理"
APP_VERSION = "1.0"

DEFAULT_SETTINGS = {
    "show_system": False,
    "show_running": True,
    "refresh_seconds": 5,
    "default_delay": 0,
    "sort": "delay",
}

CSS = """
.entry-title { font-weight: 700; font-size: 1.03em; }
.field-label { font-size: 0.78em; }
.dim { color: alpha(@theme_fg_color, 0.62); }
.mono { font-family: monospace; font-size: 0.88em; }
.error-label { color: #c01c28; }
.card {
    background-color: @theme_base_color;
    border: 1px solid @borders;
    border-radius: 10px;
    padding: 10px 12px;
}
.card:hover { border-color: alpha(@theme_selected_bg_color, 0.55); }
.entry-list { background-color: transparent; }
.entry-list row { background-color: transparent; padding: 0; }
.section-title { font-weight: 700; font-size: 1.04em; }
.pill {
    border-radius: 999px;
    padding: 1px 8px;
    font-size: 0.76em;
    font-weight: 600;
}
.pill-system {
    background-color: alpha(@theme_fg_color, 0.09);
    color: alpha(@theme_fg_color, 0.72);
}
.pill-override { background-color: alpha(#f6d32d, 0.24); }
.pill-custom { background-color: alpha(@theme_selected_bg_color, 0.18); }
.pill-running { background-color: alpha(#2ec27e, 0.24); }
.pill-embedded { background-color: alpha(#f6d32d, 0.24); }
.pill-conditional {
    background-color: alpha(@theme_fg_color, 0.09);
    color: alpha(@theme_fg_color, 0.72);
}
.footer {
    border-top: 1px solid @borders;
    padding: 7px 14px;
}
"""


def config_path() -> Path:
    return autostart.config_home() / "startup-manager" / "settings.json"


class Settings:
    def __init__(self):
        self.path = config_path()
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(loaded, dict):
            return
        if isinstance(loaded.get("show_system"), bool):
            self.data["show_system"] = loaded["show_system"]
        if isinstance(loaded.get("show_running"), bool):
            self.data["show_running"] = loaded["show_running"]
        if loaded.get("refresh_seconds") in (2, 5, 10, 30):
            self.data["refresh_seconds"] = int(loaded["refresh_seconds"])
        if isinstance(loaded.get("default_delay"), (int, float)):
            self.data["default_delay"] = max(0, min(600, int(loaded["default_delay"])))
        if loaded.get("sort") in ("delay", "name"):
            self.data["sort"] = loaded["sort"]

    def save(self) -> None:
        autostart.atomic_write(
            self.path,
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
        )


def icon_image(icon_name: str, size: int = 32) -> Gtk.Image:
    expanded = os.path.expanduser(icon_name.strip()) if icon_name else ""
    if expanded and os.path.isabs(expanded):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                expanded, size, size, True
            )
            return Gtk.Image.new_from_pixbuf(pixbuf)
        except GLib.Error:
            pass
    theme = Gtk.IconTheme.get_default()
    if icon_name and theme.has_icon(icon_name):
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DND)
    else:
        image = Gtk.Image.new_from_icon_name(
            "application-x-executable-symbolic", Gtk.IconSize.DND
        )
    image.set_pixel_size(size)
    return image


def pill(text: str, css_class: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.get_style_context().add_class("pill")
    label.get_style_context().add_class(css_class)
    label.set_valign(Gtk.Align.CENTER)
    return label


def dim_label(text: str = "") -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.get_style_context().add_class("dim")
    return label


def menu_item(label: str, callback, *, destructive: bool = False) -> Gtk.Button:
    item = Gtk.Button()
    item.set_relief(Gtk.ReliefStyle.NONE)
    item.get_style_context().add_class("flat")
    if destructive:
        item.get_style_context().add_class("destructive-action")
    text = Gtk.Label(label=label)
    text.set_xalign(0)
    item.add(text)
    item.set_size_request(196, -1)
    item.connect("clicked", lambda *_: callback())
    return item


@dataclass
class Section:
    box: Gtk.Box
    listbox: Gtk.ListBox
    count_label: Gtk.Label
    rows: list["EntryRow"] = field(default_factory=list)


class EntryRow(Gtk.ListBoxRow):
    def __init__(self, window: "StartupManagerWindow", entry: autostart.AutostartEntry):
        super().__init__()
        self.owner = window
        self.entry = entry
        self._updating = False
        self.search_text = " ".join(
            [entry.name, entry.comment, entry.exec_line, entry.desktop_id]
        ).casefold()
        self._build()
        self.set_enabled_visual(entry.enabled)
        self.set_running(False)
        self.update_source()

    def _build(self) -> None:
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.get_style_context().add_class("card")
        card.set_margin_start(4)
        card.set_margin_end(4)
        card.set_margin_top(4)
        card.set_margin_bottom(4)
        self.add(card)

        icon = icon_image(self.entry.icon, 32)
        icon.set_valign(Gtk.Align.START)
        icon.set_margin_top(2)
        card.pack_start(icon, False, False, 0)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        info.set_hexpand(True)
        info.set_valign(Gtk.Align.CENTER)
        card.pack_start(info, True, True, 0)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        name = Gtk.Label(label=self.entry.name)
        name.get_style_context().add_class("entry-title")
        name.set_xalign(0)
        name.set_hexpand(True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(48)
        title_row.pack_start(name, True, True, 0)

        self.source_pill = pill("", self._source_class())
        self.source_pill.set_no_show_all(True)
        title_row.pack_start(self.source_pill, False, False, 0)

        self.running_pill = pill("● 运行中", "pill-running")
        self.running_pill.set_no_show_all(True)
        self.running_pill.set_tooltip_text("在进程列表中找到匹配的程序")
        title_row.pack_start(self.running_pill, False, False, 0)

        if self.entry.conditional:
            conditional = pill("按条件", "pill-conditional")
            conditional.set_tooltip_text(
                "该启动项带有 AutostartCondition，桌面可能根据条件决定是否启动"
            )
            title_row.pack_start(conditional, False, False, 0)

        if self.entry.embedded_delay:
            embedded = pill(
                f"命令内延迟 {self.entry.embedded_delay:g} 秒", "pill-embedded"
            )
            embedded.set_tooltip_text(
                "启动命令自身包含 sleep；此处设置的原生延迟会与它叠加"
            )
            title_row.pack_start(embedded, False, False, 0)

        info.pack_start(title_row, False, False, 0)

        if self.entry.comment:
            comment = dim_label(self.entry.comment)
            comment.set_ellipsize(Pango.EllipsizeMode.END)
            comment.set_max_width_chars(80)
            info.pack_start(comment, False, False, 0)

        command = Gtk.Label(label=self.entry.exec_line or "（未设置启动命令）")
        command.get_style_context().add_class("dim")
        command.get_style_context().add_class("mono")
        command.set_xalign(0)
        command.set_ellipsize(Pango.EllipsizeMode.END)
        command.set_max_width_chars(80)
        command.set_tooltip_text(self.entry.exec_line or "该启动项没有 Exec 字段")
        info.pack_start(command, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        controls.set_valign(Gtk.Align.CENTER)
        card.pack_start(controls, False, False, 0)

        delay_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        delay_box.set_valign(Gtk.Align.CENTER)
        delay_label = Gtk.Label(label="延迟")
        delay_label.get_style_context().add_class("field-label")
        delay_label.get_style_context().add_class("dim")
        delay_label.set_xalign(0.5)
        delay_box.pack_start(delay_label, False, False, 0)
        delay_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        self.delay_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.delay_spin.set_numeric(True)
        self.delay_spin.set_value(self.entry.delay)
        self.delay_spin.set_width_chars(3)
        self.delay_spin.set_max_width_chars(4)
        delay_tip = "登录后延迟多少秒启动；0 表示立即。"
        if self.entry.embedded_delay:
            delay_tip += (
                f" 命令内另有 {self.entry.embedded_delay:g} 秒 sleep，"
                f"总计约 {self.entry.effective_delay:g} 秒。"
            )
        self.delay_spin.set_tooltip_text(delay_tip)
        self.delay_spin.connect("value-changed", self._on_delay_changed)
        delay_line.pack_start(self.delay_spin, False, False, 0)
        delay_line.pack_start(dim_label("秒"), False, False, 0)
        delay_box.pack_start(delay_line, False, False, 0)
        controls.pack_start(delay_box, False, False, 0)

        self.switch = Gtk.Switch()
        self.switch.set_active(self.entry.enabled)
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.set_tooltip_text("开启或关闭这个登录启动项")
        self.switch.connect("state-set", self._on_switch_state_set)
        controls.pack_start(self.switch, False, False, 0)

        controls.pack_start(self._build_menu(), False, False, 0)

    def _source_class(self) -> str:
        if self.entry.is_override:
            return "pill-override"
        if self.entry.is_user:
            return "pill-custom"
        return "pill-system"

    def _build_menu(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton()
        button.set_image(
            Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON)
        )
        button.set_tooltip_text("更多操作")
        button.set_valign(Gtk.Align.CENTER)
        button.get_style_context().add_class("flat")

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        popover.add(box)

        def close_and(callback):
            popover.popdown()
            callback()

        if self.entry.is_user:
            box.pack_start(
                menu_item("编辑…", lambda: close_and(self.owner.open_editor(self.entry))),
                False,
                False,
                0,
            )
        else:
            box.pack_start(
                menu_item(
                    "自定义覆盖并编辑…",
                    lambda: close_and(self.owner.open_editor(self.entry)),
                ),
                False,
                False,
                0,
            )
        box.pack_start(
            menu_item(
                "复制启动命令",
                lambda: close_and(self.owner.copy_command(self.entry)),
            ),
            False,
            False,
            0,
        )
        box.pack_start(
            menu_item(
                "在文件夹中显示",
                lambda: close_and(self.owner.show_in_folder(self.entry)),
            ),
            False,
            False,
            0,
        )
        if self.entry.is_user:
            box.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 3
            )
            label = "移除用户覆盖" if self.entry.is_override else "删除启动项"
            box.pack_start(
                menu_item(
                    label,
                    lambda: close_and(self.owner.confirm_delete(self.entry)),
                    destructive=True,
                ),
                False,
                False,
                0,
            )
        box.show_all()
        button.set_popover(popover)
        return button

    def _on_switch_state_set(self, _switch: Gtk.Switch, state: bool) -> bool:
        if self._updating:
            return False
        try:
            self.owner.change_enabled(self.entry, state)
        except Exception as exc:  # noqa: BLE001 - surfaced in a dialog
            self.owner.show_error("无法修改启动项", str(exc))
            return True
        self.set_enabled_visual(state)
        self.update_source()
        return False

    def _on_delay_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating:
            return
        try:
            self.owner.change_delay(self.entry, int(spin.get_value()))
        except Exception as exc:  # noqa: BLE001 - surfaced in a dialog
            self.owner.show_error("无法保存延迟", str(exc))
            self._updating = True
            spin.set_value(self.entry.delay)
            self._updating = False

    def update_source(self) -> None:
        ctx = self.source_pill.get_style_context()
        for name in ("pill-system", "pill-override", "pill-custom"):
            ctx.remove_class(name)
        ctx.add_class(self._source_class())
        self.source_pill.set_text(self.entry.source_label)
        self.source_pill.set_visible(self.entry.is_override or not self.entry.is_user)

    def set_enabled_visual(self, enabled: bool) -> None:
        self._updating = True
        self.switch.set_active(enabled)
        self._updating = False
        self.set_opacity(1.0 if enabled else 0.58)

    def set_running(self, running: bool) -> None:
        self.running_pill.set_visible(bool(running))

    def focus_delay(self) -> None:
        self.delay_spin.grab_focus()


class EntryEditor(Gtk.Dialog):
    def __init__(
        self,
        window: "StartupManagerWindow",
        entry: autostart.AutostartEntry | None = None,
    ):
        title = "编辑启动项" if entry is not None else "添加启动项"
        super().__init__(
            title=title,
            transient_for=window,
            modal=True,
        )
        self.owner = window
        self.entry = entry
        self.set_default_size(620, -1)
        self.set_resizable(True)
        self.add_button("取消", Gtk.ResponseType.CANCEL)
        save = self.add_button("保存", Gtk.ResponseType.OK)
        save.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self._build()
        self.connect("response", self._on_response)

    def _build(self) -> None:
        content = self.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_size_request(560, -1)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(20)
        box.set_margin_end(20)
        content.add(box)

        if self.entry is not None and not self.entry.is_user:
            note = Gtk.Label(
                label=(
                    "这是一个系统启动项。保存后会创建用户级覆盖，"
                    "不会修改 /etc/xdg/autostart 中的系统文件。"
                )
            )
            note.set_xalign(0)
            note.set_line_wrap(True)
            note.get_style_context().add_class("dim")
            box.pack_start(note, False, False, 0)

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        grid.set_hexpand(True)
        box.pack_start(grid, False, False, 0)

        def label(text: str, row: int) -> None:
            item = Gtk.Label(label=text)
            item.set_xalign(0)
            item.set_valign(Gtk.Align.CENTER)
            grid.attach(item, 0, row, 1, 1)

        def field_row(widget: Gtk.Widget, row: int) -> None:
            grid.attach(widget, 1, row, 1, 1)

        label("名称", 0)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_text(self.entry.name if self.entry else "")
        self.name_entry.set_activates_default(True)
        field_row(self.name_entry, 0)

        label("命令", 1)
        command_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        command_box.set_hexpand(True)
        self.command_entry = Gtk.Entry()
        self.command_entry.set_hexpand(True)
        if self.entry is not None:
            self.command_entry.set_text(self.entry.exec_line)
        self.command_entry.set_activates_default(True)
        command_box.pack_start(self.command_entry, True, True, 0)
        browse = Gtk.Button(label="浏览…")
        browse.connect("clicked", self._browse_command)
        command_box.pack_start(browse, False, False, 0)
        field_row(command_box, 1)

        label("备注", 2)
        self.comment_entry = Gtk.Entry()
        self.comment_entry.set_hexpand(True)
        self.comment_entry.set_text(self.entry.comment if self.entry else "")
        self.comment_entry.set_activates_default(True)
        field_row(self.comment_entry, 2)

        label("图标", 3)
        icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon_box.set_hexpand(True)
        self.icon_entry = Gtk.Entry()
        self.icon_entry.set_hexpand(True)
        self.icon_entry.set_placeholder_text("图标名称，或图片的绝对路径（可选）")
        if self.entry is not None:
            self.icon_entry.set_text(self.entry.icon)
        self.icon_entry.connect("changed", self._update_icon_preview)
        icon_box.pack_start(self.icon_entry, True, True, 0)
        icon_browse = Gtk.Button(label="选择…")
        icon_browse.connect("clicked", self._browse_icon)
        icon_box.pack_start(icon_browse, False, False, 0)
        self.icon_preview_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4
        )
        self.icon_preview = Gtk.Image()
        self.icon_preview_box.pack_start(self.icon_preview, False, False, 0)
        icon_box.pack_start(self.icon_preview_box, False, False, 0)
        field_row(icon_box, 3)

        label("延迟", 4)
        delay_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.delay_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.delay_spin.set_numeric(True)
        default_delay = (
            self.entry.delay
            if self.entry is not None
            else int(self.owner.settings.data["default_delay"])
        )
        self.delay_spin.set_value(default_delay)
        delay_box.pack_start(self.delay_spin, False, False, 0)
        delay_box.pack_start(dim_label("秒"), False, False, 0)
        delay_box.pack_start(
            dim_label("0 表示立即启动；命令内的 sleep 会额外叠加"), False, False, 0
        )
        field_row(delay_box, 4)

        label("启用", 5)
        enabled_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.enabled_switch = Gtk.Switch()
        self.enabled_switch.set_active(
            self.entry.enabled if self.entry is not None else True
        )
        enabled_box.pack_start(self.enabled_switch, False, False, 0)
        enabled_box.pack_start(dim_label("登录时自动启动"), False, False, 0)
        field_row(enabled_box, 5)

        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_line_wrap(True)
        self.error_label.get_style_context().add_class("error-label")
        self.error_label.set_no_show_all(True)
        box.pack_start(self.error_label, False, False, 0)

        self._update_icon_preview()
        self.set_focus(self.name_entry)

    def _update_icon_preview(self, *_args) -> None:
        old_parent = self.icon_preview.get_parent()
        if old_parent is not None:
            old_parent.remove(self.icon_preview)
        self.icon_preview = icon_image(self.icon_entry.get_text(), 24)
        self.icon_preview_box.pack_start(self.icon_preview, False, False, 0)
        self.icon_preview.show()

    def _choose_file(self, title: str, patterns: list[str] | None = None) -> str | None:
        chooser = Gtk.FileChooserDialog(
            title=title,
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_buttons(
            "取消",
            Gtk.ResponseType.CANCEL,
            "选择",
            Gtk.ResponseType.OK,
        )
        if patterns:
            file_filter = Gtk.FileFilter()
            file_filter.set_name("图片")
            for pattern in patterns:
                file_filter.add_pattern(pattern)
            chooser.add_filter(file_filter)
        response = chooser.run()
        path = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        return path

    def _browse_command(self, _button: Gtk.Button) -> None:
        path = self._choose_file("选择要启动的程序")
        if path:
            self.command_entry.set_text(autostart.quote_exec_arg(path))
            self.command_entry.set_position(-1)

    def _browse_icon(self, _button: Gtk.Button) -> None:
        path = self._choose_file(
            "选择图标图片", ["*.png", "*.svg", "*.jpg", "*.jpeg", "*.xpm"]
        )
        if path:
            self.icon_entry.set_text(path)

    def _show_error(self, message: str) -> None:
        self.error_label.set_text(message)
        self.error_label.set_visible(True)

    def _values(self) -> dict:
        return {
            "name": self.name_entry.get_text().strip(),
            "command": self.command_entry.get_text().strip(),
            "comment": self.comment_entry.get_text().strip(),
            "icon": self.icon_entry.get_text().strip(),
            "delay": int(self.delay_spin.get_value()),
            "enabled": self.enabled_switch.get_active(),
        }

    def _on_response(self, _dialog: Gtk.Dialog, response: int) -> None:
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        values = self._values()
        if not values["name"]:
            self._show_error("请填写启动项名称。")
            self.name_entry.grab_focus()
            return
        if not values["command"]:
            self._show_error("请填写启动命令，或点击“浏览…”选择程序。")
            self.command_entry.grab_focus()
            return
        try:
            self.owner.save_entry(self.entry, values)
        except Exception as exc:  # noqa: BLE001 - keep the dialog open
            self._show_error(f"保存失败：{exc}")
            return
        self.destroy()


class SettingsDialog(Gtk.Dialog):
    RESPONSE_RESET = 1

    def __init__(self, window: "StartupManagerWindow"):
        super().__init__(
            title="设置",
            transient_for=window,
            modal=True,
        )
        self.owner = window
        self.set_default_size(560, -1)
        self.add_button("恢复默认", self.RESPONSE_RESET)
        self.add_button("取消", Gtk.ResponseType.CANCEL)
        save = self.add_button("保存", Gtk.ResponseType.OK)
        save.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self._build()
        self.connect("response", self._on_response)

    def _build(self) -> None:
        content = self.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_size_request(500, -1)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(22)
        box.set_margin_end(22)
        content.add(box)

        def setting_row(title: str, description: str, control: Gtk.Widget) -> Gtk.Box:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.set_hexpand(True)
            heading = Gtk.Label(label=title)
            heading.set_xalign(0)
            description_label = Gtk.Label(label=description)
            description_label.set_xalign(0)
            description_label.set_line_wrap(True)
            description_label.set_max_width_chars(46)
            description_label.get_style_context().add_class("dim")
            text.pack_start(heading, False, False, 0)
            text.pack_start(description_label, False, False, 0)
            row.pack_start(text, True, True, 0)
            control.set_valign(Gtk.Align.CENTER)
            row.pack_end(control, False, False, 0)
            return row

        self.system_switch = Gtk.Switch()
        self.system_switch.set_active(bool(self.owner.settings.data["show_system"]))
        box.pack_start(
            setting_row(
                "显示系统启动项",
                "显示 /etc/xdg/autostart 中的系统组件。默认隐藏。",
                self.system_switch,
            ),
            False,
            False,
            0,
        )

        self.running_switch = Gtk.Switch()
        self.running_switch.set_active(bool(self.owner.settings.data["show_running"]))
        box.pack_start(
            setting_row(
                "显示运行状态",
                "在列表中标记当前正在运行的启动程序。",
                self.running_switch,
            ),
            False,
            False,
            0,
        )

        self.refresh_combo = Gtk.ComboBoxText()
        for seconds in (2, 5, 10, 30):
            self.refresh_combo.append(str(seconds), f"{seconds} 秒")
        self.refresh_combo.set_active_id(
            str(self.owner.settings.data["refresh_seconds"])
        )
        self.refresh_combo.set_size_request(130, -1)
        self.refresh_combo.set_sensitive(self.running_switch.get_active())
        self.running_switch.connect(
            "notify::active",
            lambda switch, _param: self.refresh_combo.set_sensitive(
                switch.get_active()
            ),
        )
        box.pack_start(
            setting_row(
                "状态刷新间隔",
                "多久重新检查一次运行状态。",
                self.refresh_combo,
            ),
            False,
            False,
            0,
        )

        self.default_delay_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.default_delay_spin.set_numeric(True)
        self.default_delay_spin.set_value(
            int(self.owner.settings.data["default_delay"])
        )
        delay_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        delay_box.pack_start(self.default_delay_spin, False, False, 0)
        delay_box.pack_start(dim_label("秒"), False, False, 0)
        box.pack_start(
            setting_row(
                "新启动项默认延迟",
                "添加启动项时预填的延迟秒数。",
                delay_box,
            ),
            False,
            False,
            0,
        )

        self.sort_combo = Gtk.ComboBoxText()
        self.sort_combo.append("delay", "按实际延迟")
        self.sort_combo.append("name", "按名称")
        self.sort_combo.set_active_id(self.owner.settings.data["sort"])
        self.sort_combo.set_size_request(150, -1)
        box.pack_start(
            setting_row(
                "列表排序",
                "已启用的项目排在前面，可按实际延迟或名称排列。",
                self.sort_combo,
            ),
            False,
            False,
            0,
        )

        box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2
        )
        note = Gtk.Label(
            label=(
                "延迟写入 X-GNOME-Autostart-Delay，由 GNOME 会话在登录时执行。"
                "KDE、XFCE 等桌面可能忽略该字段；命令自身包含的 sleep 会在列表中单独标注。\n"
                f"设置保存在 {config_path()}"
            )
        )
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim")
        box.pack_start(note, False, False, 0)

        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_line_wrap(True)
        self.error_label.get_style_context().add_class("error-label")
        self.error_label.set_no_show_all(True)
        box.pack_start(self.error_label, False, False, 0)

    def _reset_defaults(self) -> None:
        self.system_switch.set_active(DEFAULT_SETTINGS["show_system"])
        self.running_switch.set_active(DEFAULT_SETTINGS["show_running"])
        self.refresh_combo.set_active_id(str(DEFAULT_SETTINGS["refresh_seconds"]))
        self.default_delay_spin.set_value(DEFAULT_SETTINGS["default_delay"])
        self.sort_combo.set_active_id(DEFAULT_SETTINGS["sort"])
        self.error_label.set_visible(False)

    def _on_response(self, _dialog: Gtk.Dialog, response: int) -> None:
        if response == self.RESPONSE_RESET:
            self._reset_defaults()
            return
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        try:
            refreshed = int(self.refresh_combo.get_active_id() or 5)
            data = {
                "show_system": self.system_switch.get_active(),
                "show_running": self.running_switch.get_active(),
                "refresh_seconds": refreshed,
                "default_delay": int(self.default_delay_spin.get_value()),
                "sort": self.sort_combo.get_active_id() or "delay",
            }
        except (TypeError, ValueError) as exc:
            self.error_label.set_text(f"设置值无效：{exc}")
            self.error_label.set_visible(True)
            return
        try:
            self.owner.apply_settings(data)
        except Exception as exc:  # noqa: BLE001 - keep the dialog open
            self.error_label.set_text(f"保存设置失败：{exc}")
            self.error_label.set_visible(True)
            return
        self.destroy()


class StartupManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(1020, 700)
        self.set_size_request(780, 480)
        self.settings = Settings()
        self.entries: list[autostart.AutostartEntry] = []
        self.sections: list[Section] = []
        self.rows: list[EntryRow] = []
        self.filter_text = ""
        self._visible_count = 0
        self._running_count = 0
        self._modal_count = 0
        self._dialogs: set[Gtk.Dialog] = set()
        self._monitors: list[Gio.FileMonitor] = []
        self._monitor_reload_source = 0
        self._delay_reload_source = 0
        self._status_source = 0
        self._flash_source = 0
        self._ignore_monitor_until = 0.0
        self._reload_pending = False
        self._scroll_position = 0.0
        self._destroyed = False
        self._build_ui()
        self.connect("destroy", self._on_destroy)
        self._start_monitors()
        self.reload()
        self._restart_status_timer()
        self.show_all()
        self.apply_filter()
        self.refresh_running()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label()
        title.set_markup(f"<b>{GLib.markup_escape_text(APP_NAME)}</b>")
        title.set_xalign(0)
        subtitle = Gtk.Label(label="管理登录时自动启动的程序")
        subtitle.set_xalign(0)
        subtitle.get_style_context().add_class("dim")
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(subtitle, False, False, 0)
        header.set_custom_title(title_box)
        self.set_titlebar(header)

        menu_button = Gtk.MenuButton()
        menu_button.set_image(
            Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
        )
        menu_button.set_tooltip_text("菜单")
        menu_popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)

        def popover_item(label: str, callback) -> None:
            menu_box.pack_start(
                menu_item(
                    label,
                    lambda: (menu_popover.popdown(), callback()),
                ),
                False,
                False,
                0,
            )

        popover_item("刷新列表", self.reload)
        popover_item("打开启动项文件夹", self.open_startup_folder)
        menu_box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 3
        )
        popover_item("设置…", self.open_settings)
        popover_item("关于", self.show_about)
        menu_box.show_all()
        menu_popover.add(menu_box)
        menu_button.set_popover(menu_popover)
        header.pack_end(menu_button)

        add_button = Gtk.Button()
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_box.pack_start(
            Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )
        add_box.pack_start(Gtk.Label(label="添加"), False, False, 0)
        add_button.add(add_box)
        add_button.get_style_context().add_class("suggested-action")
        add_button.set_tooltip_text("添加一个登录启动项")
        add_button.connect("clicked", lambda *_: self.open_editor(None))
        header.pack_end(add_button)

        refresh_button = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON
        )
        refresh_button.set_tooltip_text("重新读取启动项")
        refresh_button.connect("clicked", lambda *_: self.reload())
        header.pack_end(refresh_button)

        self.search_toggle = Gtk.ToggleButton()
        self.search_toggle.set_image(
            Gtk.Image.new_from_icon_name("system-search-symbolic", Gtk.IconSize.BUTTON)
        )
        self.search_toggle.set_tooltip_text("搜索启动项")
        header.pack_end(self.search_toggle)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_show_close_button(True)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("搜索名称、备注或命令…")
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.add(self.search_entry)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)
        self.search_toggle.connect(
            "toggled",
            lambda button: self.search_bar.set_search_mode(button.get_active()),
        )
        root.pack_start(self.search_bar, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_vexpand(True)
        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.content.set_margin_top(12)
        self.content.set_margin_bottom(16)
        self.content.set_margin_start(16)
        self.content.set_margin_end(16)
        viewport.add(self.content)
        self.scroll.add(viewport)
        root.pack_start(self.scroll, True, True, 0)

        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.footer.get_style_context().add_class("footer")
        self.footer_left = dim_label()
        self.footer_left.set_hexpand(True)
        self.footer_right = dim_label()
        self.footer_info = Gtk.Image.new_from_icon_name(
            "dialog-information-symbolic", Gtk.IconSize.MENU
        )
        self.footer_info.set_tooltip_text(
            "延迟使用 X-GNOME-Autostart-Delay，由 GNOME 会话执行。"
            "命令自身包含的 sleep 会额外叠加。"
        )
        self.footer.pack_start(self.footer_left, True, True, 0)
        self.footer.pack_end(self.footer_right, False, False, 0)
        self.footer.pack_end(self.footer_info, False, False, 0)
        root.pack_start(self.footer, False, False, 0)

        self._build_empty_state()

    def _build_empty_state(self) -> None:
        self.empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.empty_box.set_halign(Gtk.Align.CENTER)
        self.empty_box.set_valign(Gtk.Align.CENTER)
        self.empty_box.set_margin_top(60)
        icon = Gtk.Image.new_from_icon_name(
            "system-run-symbolic", Gtk.IconSize.DIALOG
        )
        icon.set_pixel_size(64)
        self.empty_box.pack_start(icon, False, False, 0)
        self.empty_title = Gtk.Label()
        self.empty_title.set_xalign(0.5)
        self.empty_title.get_style_context().add_class("entry-title")
        self.empty_box.pack_start(self.empty_title, False, False, 0)
        self.empty_subtitle = Gtk.Label()
        self.empty_subtitle.set_xalign(0.5)
        self.empty_subtitle.get_style_context().add_class("dim")
        self.empty_subtitle.set_line_wrap(True)
        self.empty_subtitle.set_max_width_chars(50)
        self.empty_box.pack_start(self.empty_subtitle, False, False, 0)
        add = Gtk.Button(label="添加启动项")
        add.get_style_context().add_class("suggested-action")
        add.connect("clicked", lambda *_: self.open_editor(None))
        self.empty_box.pack_start(add, False, False, 0)
        self.empty_box.show_all()
        self.empty_box.set_no_show_all(True)

    # --------------------------------------------------------------- reload

    def reload(self) -> None:
        self._cancel_source("_monitor_reload_source")
        self._cancel_source("_delay_reload_source")
        adjustment = self.scroll.get_vadjustment()
        if adjustment is not None:
            self._scroll_position = adjustment.get_value()
        self.entries = autostart.scan_entries(
            include_system=bool(self.settings.data["show_system"])
        )
        user_entries = [entry for entry in self.entries if entry.is_user]
        system_entries = [entry for entry in self.entries if not entry.is_user]
        self._sort_entries(user_entries)
        self._sort_entries(system_entries)

        for child in list(self.content.get_children()):
            self.content.remove(child)
        self.sections = []
        self.rows = []

        if user_entries:
            self._add_section(
                "我的启动项",
                user_entries,
                "登录后按延迟依次启动。列表中的修改会立即写入用户启动项。",
            )
        if self.settings.data["show_system"] and system_entries:
            self._add_section(
                "系统启动项",
                system_entries,
                "来自 /etc/xdg/autostart；调整会创建用户覆盖，不修改系统文件。",
            )
        self.content.pack_start(self.empty_box, False, False, 0)
        self.apply_filter()
        self.refresh_running()
        GLib.idle_add(self._restore_scroll)

    def _add_section(self, title: str, entries: list, description: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_label = Gtk.Label(label=title)
        title_label.get_style_context().add_class("section-title")
        title_label.set_xalign(0)
        count_label = dim_label(str(len(entries)))
        title_row.pack_start(title_label, False, False, 0)
        title_row.pack_start(count_label, False, False, 0)
        title_row.set_margin_start(6)
        box.pack_start(title_row, False, False, 0)
        description_label = dim_label(description)
        description_label.set_line_wrap(True)
        description_label.set_max_width_chars(90)
        description_label.set_margin_start(6)
        description_label.set_margin_bottom(2)
        box.pack_start(description_label, False, False, 0)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.set_activate_on_single_click(False)
        listbox.get_style_context().add_class("entry-list")
        section = Section(box, listbox, count_label)
        for entry in entries:
            row = EntryRow(self, entry)
            row.show_all()
            listbox.add(row)
            section.rows.append(row)
            self.rows.append(row)
        box.pack_start(listbox, False, False, 0)
        box.show_all()
        self.sections.append(section)
        self.content.pack_start(box, False, False, 0)

    def _sort_entries(self, entries: list) -> None:
        mode = self.settings.data["sort"]
        if mode == "name":
            entries.sort(
                key=lambda entry: (
                    0 if entry.enabled else 1,
                    entry.name.casefold(),
                )
            )
        else:
            entries.sort(
                key=lambda entry: (
                    0 if entry.enabled else 1,
                    entry.effective_delay,
                    entry.name.casefold(),
                )
            )

    def _restore_scroll(self) -> bool:
        if self._destroyed:
            return GLib.SOURCE_REMOVE
        adjustment = self.scroll.get_vadjustment()
        if adjustment is not None:
            adjustment.set_value(self._scroll_position)
        return GLib.SOURCE_REMOVE

    # --------------------------------------------------------------- filter

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self.filter_text = entry.get_text().strip().casefold()
        self.apply_filter()

    def _on_search_mode(self, bar: Gtk.SearchBar, _param) -> None:
        active = bar.get_search_mode()
        if self.search_toggle.get_active() != active:
            self.search_toggle.set_active(active)
        if not active and self.search_entry.get_text():
            self.search_entry.set_text("")

    def apply_filter(self) -> None:
        query = self.filter_text
        visible_total = 0
        for section in self.sections:
            visible_in_section = 0
            for row in section.rows:
                visible = not query or query in row.search_text
                row.set_visible(visible)
                if visible:
                    visible_in_section += 1
            section.box.set_visible(
                bool(section.rows) and (not query or visible_in_section > 0)
            )
            if query:
                section.count_label.set_text(
                    f"{visible_in_section}/{len(section.rows)} 项"
                )
            else:
                section.count_label.set_text(f"{len(section.rows)} 项")
            visible_total += visible_in_section
        self._visible_count = visible_total
        self.empty_box.set_visible(visible_total == 0)
        if query:
            self.empty_title.set_text("没有匹配的启动项")
            self.empty_subtitle.set_text("试试其他关键词，或添加一个新的启动项。")
        else:
            self.empty_title.set_text("还没有登录启动项")
            self.empty_subtitle.set_text(
                "添加一个程序，让它登录后自动运行。也可以点击右上角的“添加”。"
            )
        self._update_footer()

    def _update_footer(self) -> None:
        total = len(self.entries)
        enabled = sum(1 for entry in self.entries if entry.enabled)
        parts = [f"共 {total} 项", f"{enabled} 项已启用"]
        if self.filter_text:
            parts.append(f"筛选出 {self._visible_count} 项")
        if self.settings.data["show_running"] and self._running_count:
            parts.append(f"{self._running_count} 个运行中")
        self.footer_left.set_text(" · ".join(parts))
        sort_text = (
            "按名称排序"
            if self.settings.data["sort"] == "name"
            else "按实际延迟排序"
        )
        if not self.settings.data["show_system"]:
            sort_text += " · 系统项已隐藏"
        self.footer_right.set_text(sort_text)

    # -------------------------------------------------------------- actions

    def change_enabled(self, entry: autostart.AutostartEntry, enabled: bool) -> None:
        autostart.set_enabled(entry, enabled)
        self._ignore_monitor_until = time.monotonic() + 1.5
        row = self._row_for(entry)
        if row is not None:
            row.update_source()
            row.set_enabled_visual(enabled)
        self.schedule_reload(350)

    def change_delay(self, entry: autostart.AutostartEntry, seconds: int) -> None:
        autostart.set_delay(entry, seconds)
        self._ignore_monitor_until = time.monotonic() + 1.5
        row = self._row_for(entry)
        if row is not None:
            row.update_source()
        if self._delay_reload_source:
            GLib.source_remove(self._delay_reload_source)
        self._delay_reload_source = GLib.timeout_add(1400, self._reorder_after_delay)

    def _reorder_after_delay(self) -> bool:
        self._delay_reload_source = 0
        if self._destroyed:
            return GLib.SOURCE_REMOVE
        if self._modal_count == 0:
            self.reload()
        else:
            self._reload_pending = True
        return GLib.SOURCE_REMOVE

    def _row_for(self, entry: autostart.AutostartEntry) -> EntryRow | None:
        for row in self.rows:
            if row.entry is entry:
                return row
        return None

    def save_entry(self, entry, values: dict) -> None:
        if entry is None:
            autostart.create_entry(**values)
        else:
            autostart.update_fields(entry, **values)
        self._ignore_monitor_until = time.monotonic() + 1.5
        self.schedule_reload(120)

    def open_editor(self, entry: autostart.AutostartEntry | None = None) -> None:
        editor = EntryEditor(self, entry)
        self._show_dialog(editor)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        self._show_dialog(dialog)

    def confirm_delete(self, entry: autostart.AutostartEntry) -> None:
        is_override = entry.is_override
        if is_override:
            title = "移除用户覆盖？"
            detail = (
                f"“{entry.name}”将恢复为系统默认设置，"
                "用户配置的延迟和开关会被删除。"
            )
            action = "移除覆盖"
        else:
            title = "删除启动项？"
            detail = (
                f"删除后，“{entry.name}”不会再随登录启动。"
                "这个桌面文件会被直接删除。"
            )
            action = "删除"
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(detail)
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        delete_button = dialog.add_button(action, Gtk.ResponseType.OK)
        delete_button.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        def on_response(dialog: Gtk.MessageDialog, response: int) -> None:
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
            try:
                autostart.delete_entry(entry)
            except Exception as exc:  # noqa: BLE001
                self.show_error("无法删除启动项", str(exc))
                return
            self._ignore_monitor_until = time.monotonic() + 1.5
            self.reload()

        dialog.connect("response", on_response)
        self._show_dialog(dialog)

    def copy_command(self, entry: autostart.AutostartEntry) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(entry.exec_line, -1)
        clipboard.store()
        if self._flash_source:
            GLib.source_remove(self._flash_source)
        self.footer_left.set_text("已复制启动命令")
        self._flash_source = GLib.timeout_add_seconds(2, self._clear_flash)

    def _clear_flash(self) -> bool:
        self._flash_source = 0
        if not self._destroyed:
            self._update_footer()
        return GLib.SOURCE_REMOVE

    def show_in_folder(self, entry: autostart.AutostartEntry) -> None:
        path = entry.user_path or entry.system_path
        if path is None:
            return
        try:
            if shutil.which("nautilus"):
                subprocess.Popen(["nautilus", "--select", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError as exc:
            self.show_error("无法打开文件夹", str(exc))

    def open_startup_folder(self) -> None:
        directory = autostart.user_autostart_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(directory)])
        except OSError as exc:
            self.show_error("无法打开启动项文件夹", str(exc))

    def show_about(self) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_NAME)
        dialog.set_version(APP_VERSION)
        dialog.set_comments(
            "查看登录时自动启动的程序，并调整开关与启动延迟。\n"
            "零依赖，直接管理 XDG autostart 文件。"
        )
        dialog.set_authors(["Alex"])
        dialog.set_license_type(Gtk.License.MIT_X11)
        icon_path = Path(__file__).with_name("icon.svg")
        try:
            dialog.set_logo(
                GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(icon_path), 96, 96, True
                )
            )
        except GLib.Error:
            dialog.set_logo_icon_name(APP_ID)
        dialog.connect(
            "response", lambda about, _response: about.destroy()
        )
        self._show_dialog(dialog)

    # -------------------------------------------------------------- dialogs

    def _show_dialog(self, dialog: Gtk.Dialog) -> None:
        self._modal_count += 1
        self._dialogs.add(dialog)
        dialog.connect("destroy", self._dialog_destroyed)
        dialog.show_all()
        dialog.present()

    def _dialog_destroyed(self, dialog: Gtk.Dialog) -> None:
        self._dialogs.discard(dialog)
        self._modal_count = max(0, self._modal_count - 1)
        if self._modal_count == 0 and self._reload_pending:
            self._reload_pending = False
            GLib.idle_add(self._reload_idle)

    def _reload_idle(self) -> bool:
        if not self._destroyed:
            self.reload()
        return GLib.SOURCE_REMOVE

    def show_error(self, title: str, detail: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(detail or "未知错误")
        dialog.connect(
            "response", lambda message, _response: message.destroy()
        )
        self._show_dialog(dialog)

    # --------------------------------------------------------------- status

    def _restart_status_timer(self) -> None:
        self._cancel_source("_status_source")
        if not self.settings.data["show_running"]:
            self._running_count = 0
            for row in self.rows:
                row.set_running(False)
            self._update_footer()
            return
        seconds = max(2, int(self.settings.data["refresh_seconds"]))
        self._status_source = GLib.timeout_add_seconds(
            seconds, self._status_tick
        )

    def _status_tick(self) -> bool:
        if self._destroyed:
            return GLib.SOURCE_REMOVE
        self.refresh_running()
        return GLib.SOURCE_CONTINUE

    def refresh_running(self) -> None:
        if self._destroyed:
            return
        if not self.settings.data["show_running"]:
            for row in self.rows:
                row.set_running(False)
            self._running_count = 0
            self._update_footer()
            return
        processes = autostart.process_table()
        running = 0
        for row in self.rows:
            is_running = autostart.entry_is_running(row.entry, processes)
            row.set_running(is_running)
            if is_running:
                running += 1
        self._running_count = running
        self._update_footer()

    # --------------------------------------------------------------- settings

    def apply_settings(self, data: dict) -> None:
        previous = dict(self.settings.data)
        self.settings.data.update(data)
        try:
            self.settings.save()
        except Exception:
            self.settings.data = previous
            raise
        self._ignore_monitor_until = time.monotonic() + 1.5
        self.reload()
        self._restart_status_timer()
        self.refresh_running()

    # --------------------------------------------------------------- monitor

    def _start_monitors(self) -> None:
        user_dir = autostart.user_autostart_dir()
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for directory in [user_dir] + autostart.system_autostart_dirs():
            try:
                monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None
                )
            except GLib.Error:
                continue
            monitor.connect("changed", self._on_file_changed)
            self._monitors.append(monitor)

    def _on_file_changed(self, *_args) -> None:
        if time.monotonic() < self._ignore_monitor_until:
            return
        if self._modal_count:
            self._reload_pending = True
            return
        if self._monitor_reload_source:
            GLib.source_remove(self._monitor_reload_source)
        self._monitor_reload_source = GLib.timeout_add(
            350, self._monitor_reload
        )

    def _monitor_reload(self) -> bool:
        self._monitor_reload_source = 0
        if not self._destroyed:
            self.reload()
        return GLib.SOURCE_REMOVE

    # --------------------------------------------------------------- cleanup

    def schedule_reload(self, delay: int = 250) -> None:
        if self._destroyed:
            return
        if self._modal_count:
            self._reload_pending = True
            return
        self._cancel_source("_monitor_reload_source")
        self._monitor_reload_source = GLib.timeout_add(delay, self._monitor_reload)

    def _cancel_source(self, attribute: str) -> None:
        source = getattr(self, attribute, 0)
        if source:
            GLib.source_remove(source)
            setattr(self, attribute, 0)

    def _on_destroy(self, *_args) -> None:
        self._destroyed = True
        for attribute in (
            "_monitor_reload_source",
            "_delay_reload_source",
            "_status_source",
            "_flash_source",
        ):
            self._cancel_source(attribute)
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors.clear()


class StartupManagerApp(Gtk.Application):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("application_id", APP_ID)
        super().__init__(*args, **kwargs)

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        icon_path = Path(__file__).with_name("icon.svg")
        try:
            Gtk.Window.set_default_icon_from_file(str(icon_path))
        except GLib.Error:
            Gtk.Window.set_default_icon_name(APP_ID)
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS.encode("utf-8"))
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except GLib.Error:
            pass

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = StartupManagerWindow(self)
        window.present()


def main() -> int:
    app = StartupManagerApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
