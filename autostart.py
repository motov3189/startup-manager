#!/usr/bin/python3
"""Discover and edit XDG/GNOME autostart entries.

The module deliberately keeps the original desktop files as text: editing a
delay or a switch must not reorder unrelated keys or discard comments that the
application vendor shipped.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

SECTION = "Desktop Entry"
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
GENERIC_EXECUTABLES = {
    "sh", "bash", "dash", "zsh", "fish", "ksh", "env", "flatpak", "snap",
    "gtk-launch", "systemd-run", "python", "python3", "perl", "ruby", "node",
    "java", "electron",
}
FIELD_CODE = re.compile(r"%[fFuUdDnNickvm]")


# ---------------------------------------------------------------------------
# XDG locations


def config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value).expanduser() if value else Path.home() / ".config"


def config_dirs() -> list[Path]:
    value = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    return [Path(part).expanduser() for part in value.split(":") if part]


def user_autostart_dir() -> Path:
    return config_home() / "autostart"


def system_autostart_dirs() -> list[Path]:
    return [directory / "autostart" for directory in config_dirs()]


def current_desktops() -> set[str]:
    raw = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return {part.strip().lower() for part in re.split(r"[:;]", raw) if part.strip()}


def _locale_candidates() -> list[str]:
    raw = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    )
    raw = raw.split(".", 1)[0]
    values: list[str] = []
    if raw and raw not in {"C", "POSIX"}:
        values.append(raw)
        if "_" in raw:
            values.append(raw.split("_", 1)[0])
    return values


# ---------------------------------------------------------------------------
# Desktop file parsing


def parse_desktop(text: str) -> dict[str, dict[str, str]]:
    """Parse the sections of a desktop file without applying interpolation."""
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in raw_line:
            continue
        key, _, value = raw_line.partition("=")
        key = key.strip()
        if key:
            sections.setdefault(current, {})[key] = value.strip()
    return sections


def desktop_unescape(value: str) -> str:
    result: list[str] = []
    index = 0
    table = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\", ";": ";"}
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            result.append(table.get(following, following))
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def desktop_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def quote_exec_arg(value: str) -> str:
    """Quote one Exec argument according to the desktop-entry rules."""
    value = str(value)
    if not re.search(r'[\s"\'`$\\]', value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def localized_value(data: Mapping[str, str], base: str) -> tuple[str, str]:
    for code in _locale_candidates():
        key = f"{base}[{code}]"
        if key in data:
            return key, desktop_unescape(data[key])
    key = base
    return key, desktop_unescape(data.get(base, ""))


def localized_keys(data: Mapping[str, str], base: str) -> list[str]:
    return [key for key in data if key == base or key.startswith(base + "[")]


def is_true(value: str | None) -> bool:
    return value is not None and value.strip().lower() in TRUE_VALUES


def is_false(value: str | None) -> bool:
    return value is not None and value.strip().lower() in FALSE_VALUES


# ---------------------------------------------------------------------------
# Entry model


@dataclass
class AutostartEntry:
    desktop_id: str
    user_path: Path | None = None
    system_path: Path | None = None
    user_text: str = ""
    system_text: str = ""
    user_data: dict[str, str] = field(default_factory=dict)
    system_data: dict[str, str] = field(default_factory=dict)

    @property
    def data(self) -> dict[str, str]:
        merged = dict(self.system_data)
        # A user override that defines a display field must win as a whole:
        # otherwise a localized Name/Comment from the system file would still
        # outrank the value the user just typed in the editor.
        for base in ("Name", "Comment"):
            if localized_keys(self.user_data, base):
                for key in localized_keys(merged, base):
                    merged.pop(key, None)
        merged.update(self.user_data)
        return merged

    @property
    def name_key(self) -> str:
        key, _ = localized_value(self.data, "Name")
        return key

    @property
    def name(self) -> str:
        _, value = localized_value(self.data, "Name")
        if not value:
            _, value = localized_value(self.data, "GenericName")
        if not value:
            value = self.desktop_id.removesuffix(".desktop")
        return value

    @property
    def comment(self) -> str:
        _, value = localized_value(self.data, "Comment")
        return value

    @property
    def exec_line(self) -> str:
        return desktop_unescape(self.data.get("Exec", ""))

    @property
    def icon(self) -> str:
        return self.data.get("Icon", "")

    @property
    def hidden(self) -> bool:
        return is_true(self.data.get("Hidden"))

    @property
    def enabled(self) -> bool:
        if self.hidden:
            return False
        return not is_false(self.data.get("X-GNOME-Autostart-enabled"))

    @property
    def delay(self) -> int:
        try:
            return max(0, int(float(self.data.get("X-GNOME-Autostart-Delay", "0"))))
        except (TypeError, ValueError):
            return 0

    @property
    def embedded_delay(self) -> float | None:
        return embedded_sleep(self.exec_line)

    @property
    def effective_delay(self) -> float:
        return self.delay + (self.embedded_delay or 0)

    @property
    def is_user(self) -> bool:
        return self.user_path is not None

    @property
    def is_override(self) -> bool:
        return self.user_path is not None and self.system_path is not None

    @property
    def source_label(self) -> str:
        if self.is_override:
            return "用户覆盖"
        if self.is_user:
            return "自定义"
        return "系统"

    @property
    def conditional(self) -> bool:
        return bool(self.data.get("AutostartCondition", "").strip())

    def matches_current_desktop(self) -> bool:
        current = current_desktops()
        if not current:
            return True
        only = {
            part.strip().lower()
            for part in self.data.get("OnlyShowIn", "").split(";")
            if part.strip()
        }
        if only and not current & only:
            return False
        excluded = {
            part.strip().lower()
            for part in self.data.get("NotShowIn", "").split(";")
            if part.strip()
        }
        if excluded and current & excluded:
            return False
        return True


def read_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    except OSError:
        return ""


def scan_entries(
    include_system: bool = True,
    user_dir: Path | None = None,
    system_dirs: Iterable[Path] | None = None,
) -> list[AutostartEntry]:
    """Return effective autostart entries, applying user overrides."""
    user_dir = Path(user_dir) if user_dir is not None else user_autostart_dir()
    directories = (
        [Path(item) for item in system_dirs]
        if system_dirs is not None
        else system_autostart_dirs()
    )
    user_files = {
        path.name: path
        for path in sorted(user_dir.glob("*.desktop"))
        if path.is_file()
    }
    system_files: dict[str, Path] = {}
    for directory in directories:
        for path in sorted(directory.glob("*.desktop")):
            if path.is_file():
                system_files.setdefault(path.name, path)

    entries: list[AutostartEntry] = []
    for desktop_id in sorted(set(user_files) | set(system_files)):
        user_path = user_files.get(desktop_id)
        system_path = system_files.get(desktop_id)
        if not include_system and system_path is not None and user_path is None:
            continue
        user_text = read_text(user_path)
        system_text = read_text(system_path)
        user_data = parse_desktop(user_text).get(SECTION, {})
        system_data = parse_desktop(system_text).get(SECTION, {})
        entry = AutostartEntry(
            desktop_id=desktop_id,
            user_path=user_path,
            system_path=system_path,
            user_text=user_text,
            system_text=system_text,
            user_data=user_data,
            system_data=system_data,
        )
        if entry.data.get("Type", "Application") != "Application":
            continue
        if not entry.matches_current_desktop():
            continue
        # Hidden system files are vendor deltas, not user-visible startup apps.
        if system_path is not None and user_path is None and entry.hidden:
            continue
        if not entry.name and not entry.exec_line:
            continue
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Writing entries


def _desktop_bounds(lines: list[str]) -> tuple[int, int] | None:
    for index, line in enumerate(lines):
        if line.strip() != f"[{SECTION}]":
            continue
        end = len(lines)
        for following in range(index + 1, len(lines)):
            stripped = lines[following].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                end = following
                break
        return index + 1, end
    return None


def update_desktop_text(text: str, updates: Mapping[str, str | None]) -> str:
    """Update/remove keys in [Desktop Entry], keeping the rest of the file."""
    lines = text.splitlines()
    bounds = _desktop_bounds(lines)
    if bounds is None:
        lines = [f"[{SECTION}]", ""] + lines
        bounds = _desktop_bounds(lines)
    assert bounds is not None
    start, end = bounds

    found: set[str] = set()
    section_lines: list[str] = []
    for line in lines[start:end]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.partition("=")[0].strip()
            if key in updates:
                found.add(key)
                value = updates[key]
                if value is not None:
                    section_lines.append(f"{key}={value}")
                continue
        section_lines.append(line)

    additions = [
        f"{key}={value}"
        for key, value in updates.items()
        if value is not None and key not in found
    ]
    insert_at = len(section_lines)
    while insert_at > 0 and not section_lines[insert_at - 1].strip():
        insert_at -= 1
    section_lines[insert_at:insert_at] = additions
    result = lines[:start] + section_lines + lines[end:]
    return "\n".join(result).rstrip("\n") + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o644
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _refresh_entry_text(entry: AutostartEntry, text: str) -> None:
    entry.user_text = text
    entry.user_data = parse_desktop(text).get(SECTION, {})


def _write_user_entry(entry: AutostartEntry, text: str) -> None:
    if entry.user_path is None:
        raise ValueError("该启动项没有用户配置文件")
    atomic_write(entry.user_path, text)
    _refresh_entry_text(entry, text)


def ensure_user_copy(entry: AutostartEntry) -> str:
    """Give a system entry a user-level override and return its text."""
    if entry.user_path is not None:
        text = read_text(entry.user_path)
        _refresh_entry_text(entry, text)
        return text
    if entry.system_path is None:
        raise ValueError("启动项没有可复制的系统文件")
    text = entry.system_text
    if not text.strip():
        text = f"[{SECTION}]\nType=Application\n"
    target = user_autostart_dir() / entry.desktop_id
    if target.exists():
        raise FileExistsError(f"{target} 已存在")
    atomic_write(target, text)
    entry.user_path = target
    entry.user_text = text
    entry.user_data = parse_desktop(text).get(SECTION, {})
    return text


def set_enabled(entry: AutostartEntry, enabled: bool) -> None:
    text = ensure_user_copy(entry)
    updates: dict[str, str | None] = {
        "X-GNOME-Autostart-enabled": "true" if enabled else "false"
    }
    if enabled:
        updates["Hidden"] = None
    _write_user_entry(entry, update_desktop_text(text, updates))


def set_delay(entry: AutostartEntry, seconds: int) -> None:
    text = ensure_user_copy(entry)
    seconds = max(0, int(seconds))
    # Write 0 explicitly: an override that simply removes the key would fall
    # back to a delay declared in the system-wide file.
    _write_user_entry(
        entry,
        update_desktop_text(text, {"X-GNOME-Autostart-Delay": str(seconds)}),
    )


def update_fields(
    entry: AutostartEntry,
    *,
    name: str,
    command: str,
    comment: str = "",
    icon: str = "",
    delay: int = 0,
    enabled: bool = True,
) -> None:
    text = ensure_user_copy(entry)
    data = entry.data
    updates: dict[str, str | None] = {
        "Name": desktop_escape(name.strip()),
        "Exec": desktop_escape(command.strip()),
        "X-GNOME-Autostart-Delay": str(max(0, int(delay))),
        "X-GNOME-Autostart-enabled": "true" if enabled else "false",
    }
    # The editor displays one name/comment. Writing the base key and dropping
    # localized variants keeps the next launch showing exactly that value.
    for key in localized_keys(data, "Name"):
        if key != "Name":
            updates[key] = None
    if enabled:
        updates["Hidden"] = None
    # Empty values need different handling for a system override: deleting
    # the key would fall back to the system-wide value. An explicit empty key
    # masks it and makes "remove the icon/comment" stick.
    empty_mask = "" if entry.system_path is not None else None
    if icon.strip():
        updates["Icon"] = desktop_escape(icon.strip())
    else:
        updates["Icon"] = empty_mask
    if comment.strip():
        updates["Comment"] = desktop_escape(comment.strip())
    else:
        updates["Comment"] = empty_mask
    for key in localized_keys(data, "Comment"):
        if key != "Comment":
            updates[key] = None
    _write_user_entry(entry, update_desktop_text(text, updates))


def create_entry(
    *,
    name: str,
    command: str,
    comment: str = "",
    icon: str = "",
    delay: int = 0,
    enabled: bool = True,
    directory: Path | None = None,
) -> Path:
    directory = Path(directory) if directory is not None else user_autostart_dir()
    directory.mkdir(parents=True, exist_ok=True)
    base = "custom-" + time.strftime("%Y%m%d-%H%M%S")
    desktop_id = base
    counter = 2
    while (directory / f"{desktop_id}.desktop").exists():
        desktop_id = f"{base}-{counter}"
        counter += 1
    lines = [
        f"[{SECTION}]",
        "Type=Application",
        f"Name={desktop_escape(name.strip())}",
    ]
    if comment.strip():
        lines.append(f"Comment={desktop_escape(comment.strip())}")
    lines.append(f"Exec={desktop_escape(command.strip())}")
    if icon.strip():
        lines.append(f"Icon={desktop_escape(icon.strip())}")
    lines.extend(
        [
            "Terminal=false",
            "X-GNOME-Autostart-enabled=" + ("true" if enabled else "false"),
        ]
    )
    if int(delay) > 0:
        lines.append(f"X-GNOME-Autostart-Delay={max(0, int(delay))}")
    path = directory / f"{desktop_id}.desktop"
    atomic_write(path, "\n".join(lines) + "\n")
    return path


def delete_entry(entry: AutostartEntry) -> None:
    if entry.user_path is None:
        raise ValueError("系统启动项不能直接删除")
    try:
        entry.user_path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Command inspection (delay hint + "is it running?")


_SLEEP = re.compile(
    r"(?:^|[;&|(\s])sleep\s+([0-9]+(?:\.[0-9]+)?)s?(?=[\s;&|)]|$)"
)


def embedded_sleep(command: str) -> float | None:
    match = _SLEEP.search(command or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def exec_tokens(command: str) -> list[str]:
    cleaned = FIELD_CODE.sub("", command or "").strip()
    if not cleaned:
        return []
    try:
        return shlex.split(cleaned, posix=True)
    except ValueError:
        return cleaned.split()


def _script_target(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("exec "):
            continue
        try:
            tokens = shlex.split(stripped[5:])
        except ValueError:
            continue
        if tokens:
            return " ".join(shlex.quote(token) for token in tokens)
    return None


def _shell_exec_targets(command: str) -> set[str]:
    targets: set[str] = set()
    for match in re.finditer(r"(?:^|[;&|]\s*)exec\s+([^\s;&|]+)", command or ""):
        target = match.group(1).strip("()")
        if target:
            targets.add(target)
            targets.update(command_candidates(target))
    return targets


def command_candidates(command: str, _depth: int = 0) -> set[str]:
    """Best-effort list of executable names/paths that identify a process."""
    if _depth > 4:
        return set()
    tokens = exec_tokens(command)
    while tokens and tokens[0] == "env":
        tokens.pop(0)
        while tokens and (tokens[0].startswith("-") or "=" in tokens[0]):
            tokens.pop(0)
    if not tokens:
        return set()
    first = tokens[0]
    basename = os.path.basename(first).lower()

    if basename in {"sh", "bash", "dash", "zsh", "fish", "ksh"}:
        candidates: set[str] = set()
        if "-c" in tokens:
            index = tokens.index("-c")
            if index + 1 < len(tokens):
                candidates.update(command_candidates(tokens[index + 1], _depth + 1))
                candidates.update(_shell_exec_targets(tokens[index + 1]))
        return candidates

    if basename == "flatpak":
        if "run" in tokens:
            index = tokens.index("run")
            if index + 1 < len(tokens):
                return {tokens[index + 1]}
        return set()

    if basename == "snap":
        if len(tokens) > 2 and tokens[1] == "run":
            return {tokens[2]}
        return {tokens[1]} if len(tokens) > 1 else set()

    if basename.startswith("python") or basename in {"perl", "ruby", "node"}:
        candidates = set()
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            candidates.add(token)
            break
        return candidates

    candidates = {first}
    if first.startswith(("/", "~", ".")):
        expanded = Path(os.path.expanduser(first))
        target = _script_target(expanded)
        if target:
            candidates.update(command_candidates(target, _depth + 1))
    return candidates


def process_table() -> list[tuple[str, list[str], str]]:
    processes: list[tuple[str, list[str], str]] = []
    proc = Path("/proc")
    try:
        directories = list(proc.iterdir())
    except OSError:
        return processes
    for directory in directories:
        if not directory.name.isdigit():
            continue
        try:
            comm = (directory / "comm").read_text(errors="replace").strip()
            raw = (directory / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if not argv:
            continue
        processes.append((comm, argv, " ".join(argv)))
    return processes


def entry_is_running(
    entry: AutostartEntry, processes: Iterable[tuple[str, list[str], str]]
) -> bool:
    candidates = command_candidates(entry.exec_line)
    wm_class = entry.data.get("StartupWMClass", "").strip()
    if wm_class:
        candidates.add(wm_class)
    names: set[str] = set()
    paths: set[str] = set()
    for candidate in candidates:
        expanded = os.path.expanduser(candidate)
        if expanded.startswith("/"):
            paths.add(expanded)
        name = os.path.basename(expanded).strip()
        if name and name.lower() not in GENERIC_EXECUTABLES:
            names.add(name.lower())
    if not names and not paths:
        return False

    for comm, argv, arguments in processes:
        comm_lower = comm.lower()
        for name in names:
            if comm_lower == name[:15]:
                return True
            if argv and os.path.basename(argv[0]).lower() == name:
                return True
        for path in paths:
            if path in argv or path in arguments:
                return True
        for argument in argv[1:4]:
            if os.path.basename(argument).lower() in names:
                return True
    return False
