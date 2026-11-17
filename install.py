#!/usr/bin/python3
"""Install Startup Manager into the current user's application menu."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from pathlib import Path


def desktop_quote(value: str | Path) -> str:
    value = str(value)
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        + '"'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="安装启动管理到当前用户的应用菜单")
    parser.add_argument(
        "--prefix",
        type=Path,
        default=Path.home() / ".local",
        help="安装前缀，默认 ~/.local",
    )
    args = parser.parse_args()

    project = Path(__file__).resolve().parent
    prefix = args.prefix.expanduser().resolve()
    launcher = prefix / "bin" / "startup-manager"
    desktop = prefix / "share" / "applications" / "org.alex.StartupManager.desktop"
    icon = (
        prefix
        / "share"
        / "icons"
        / "hicolor"
        / "scalable"
        / "apps"
        / "org.alex.StartupManager.svg"
    )

    if launcher.exists():
        old = launcher.read_text(encoding="utf-8", errors="replace")
        if "# Startup Manager user launcher" not in old and str(project / "startup_manager.py") not in old:
            parser.error(f"{launcher} 已存在且不属于此项目，请先检查该文件。")

    for destination in (launcher, desktop, icon):
        destination.parent.mkdir(parents=True, exist_ok=True)

    launcher.write_text(
        "#!/bin/sh\n# Startup Manager user launcher\nexec /usr/bin/python3 "
        + shlex.quote(str(project / "startup_manager.py"))
        + ' "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    shutil.copyfile(project / "icon.svg", icon)
    desktop.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=启动管理\n"
        "Name[en]=Startup Manager\n"
        "Comment=查看并调整登录时自动启动的程序\n"
        "Comment[en]=Review and adjust programs that start at login\n"
        f"Exec={desktop_quote(launcher)}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "Categories=Settings;\n"
        "Keywords=autostart;startup;session;启动;自启动;开机;\n",
        encoding="utf-8",
    )
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(desktop.parent)], check=False
        )
    print("已安装：", desktop)
    print("启动器：", launcher)
    print("源码保留在：", project, "（移动源码目录后请重新运行安装脚本）")


if __name__ == "__main__":
    main()
