"""Shared installer utilities for all tools.

Provides a reusable ToolInstaller class that handles:
- Desktop shortcut creation/removal
- Dependency installation from requirements.txt
- Consistent styling for terminal output
"""

import os
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Union, List

from . import host
from .identity import InstallerIdentity


_DESKTOP_FORBIDDEN = ("\n", "\r", "\x00")

_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def _check_desktop_field(value: str, field: str) -> None:
    """Reject embedded newlines/NUL — they let a crafted value inject extra
    .desktop keys (e.g. an attacker-controlled Exec= line) since the writer
    serialises fields with f-strings rather than the freedesktop quoting spec.
    """
    if any(c in value for c in _DESKTOP_FORBIDDEN):
        raise ValueError(
            f"ToolMetadata.{field} must not contain newline or NUL characters"
        )

try:
    from termcolor import colored
except ImportError:
    def colored(text: str, *args, **kwargs) -> str:
        """Fallback when termcolor is not available."""
        return text


@dataclass
class ToolMetadata:
    """Metadata for a tool's desktop entry and advertise output."""
    name: str
    desktop_file: str
    icon: str
    desc: str
    terminal: bool = False
    args: Optional[list] = None
    categories: str = "Utility;"
    tags: Optional[list] = None  # Capability tags: GUI, CLI, Icon (default: ["GUI", "Icon"])
    alias: Optional[str] = None  # Custom alias name for non-Icon tools (defaults to desktop_file stem)
    alias_args: Optional[list] = None  # Override args used in the bash alias only; falls back to `args` if unset. Lets a tool's alias auto-inject a flag (e.g. --clip) without leaking it into --install invocations.
    # Taxonomy (PROTOCOL.md § Taxonomy). `capability` is the one controlled word a
    # parent installer groups rows by; `domain` a free distinguisher inside it;
    # `category` legacy provenance (the folder of origin). All optional.
    capability: Optional[str] = None
    domain: Optional[str] = None
    category: Optional[str] = None
    # Claude Code skill registration (PROTOCOL.md § Optional skill_name).
    # `skill_status` is "absent" | "current" | "stale"; compute it with
    # cli_tools_kit.skill_status() at advertise time and pass it in.
    skill_name: Optional[str] = None
    skill_status: Optional[str] = None
    # Conditional autostart (PROTOCOL.md § Conditional autostart). Names the
    # conditions this tool's autostart supports — "time_window", "network" —
    # and nothing more: the values behind them are the user's, and the
    # installer stores them per host. See cli_tools_kit.autostart_gate.
    autostart_conditions: Optional[list] = None


class ToolInstaller:
    """Handles installation and removal of tool desktop shortcuts.

    Provides a consistent interface for:
    - Installing pip dependencies from requirements.txt
    - Creating .desktop files in ~/.local/share/applications/
    - Removing desktop shortcuts
    - Generating --advertise JSON output

    Example usage:
        installer = ToolInstaller(
            script_path=__file__,
            metadata=ToolMetadata(
                name="My Tool",
                desktop_file="my_tool.desktop",
                icon="utilities-terminal",
                desc="A helpful tool"
            )
        )

        # In argument handling:
        if args.install:
            installer.install()
        elif args.remove:
            installer.remove()
    """

    def __init__(
        self,
        script_path: str,
        metadata: Union[ToolMetadata, List[ToolMetadata]],
        identity: Optional[InstallerIdentity] = None,
    ):
        """Initialize the installer.

        Args:
            script_path: Path to the tool's main script (usually __file__).
            metadata: One ToolMetadata, or a list for tools that expose
                multiple variants (e.g. private and public launchers from
                the same script with different --args).
            identity: Which installer these artifacts belong to — it decides
                the .desktop Keywords marker and which alias file the tool
                writes into. Defaults to the identity of the parent installer
                that spawned this --install (read from the environment), and
                to the historical first-party names when run by hand.
        """
        self.identity = identity if identity is not None else InstallerIdentity.from_env()
        self.script_path = os.path.abspath(script_path)
        self.script_dir = os.path.dirname(self.script_path)
        self.script_name = os.path.splitext(os.path.basename(self.script_path))[0]
        self.metadatas: List[ToolMetadata] = (
            list(metadata) if isinstance(metadata, (list, tuple)) else [metadata]
        )
        # Back-compat alias: tools that read `.metadata` get the first entry.
        self.metadata = self.metadatas[0]
        self.apps_dir = os.path.join(
            os.path.expanduser("~"), ".local", "share", "applications"
        )

    def _select(self, desktop_file: Optional[str]) -> List[ToolMetadata]:
        """Return metadata entries matching desktop_file, or all if None."""
        if desktop_file is None:
            return list(self.metadatas)
        matches = [m for m in self.metadatas if m.desktop_file == desktop_file]
        if not matches:
            raise ValueError(
                f"No variant with desktop_file={desktop_file!r}. "
                f"Available: {[m.desktop_file for m in self.metadatas]}"
            )
        return matches

    def _ensure_apps_dir(self) -> None:
        """Create applications directory if it doesn't exist."""
        if host.IS_WINDOWS:
            return  # no freedesktop applications directory here
        if not os.path.exists(self.apps_dir):
            os.makedirs(self.apps_dir)

    def _get_requirements_path(self) -> str:
        """Get path to requirements file."""
        return os.path.join(self.script_dir, "requirements.txt")

    def install_dependencies(self) -> bool:
        """Install dependencies from requirements file.

        Returns:
            True if successful or no requirements file, False on error.

        If the environment variable ``TOOLS_INSTALLER_SKIP_DEPS=1`` is set, pip
        is not invoked at all. The tools installer sets this for a shortcuts-only
        "Update all" so a refresh stays local and never reaches PyPI.
        """
        if os.environ.get("TOOLS_INSTALLER_SKIP_DEPS") == "1":
            print(colored("Skipping dependency install (shortcuts-only update).", "cyan"))
            return True

        req_file = self._get_requirements_path()
        if not os.path.exists(req_file):
            return True

        print(colored("Installing dependencies...", "cyan"))
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file],
                check=True
            )
            print(colored("Dependencies installed successfully.", "green"))
            return True
        except subprocess.CalledProcessError as e:
            print(colored(f"Warning: Failed to install dependencies: {e}", "yellow"))
            return False

    def _get_tags(self, metadata: Optional[ToolMetadata] = None) -> list:
        """Get tags with default fallback."""
        m = metadata if metadata is not None else self.metadata
        return m.tags if m.tags else ["GUI", "Icon"]

    def _has_icon(self, metadata: Optional[ToolMetadata] = None) -> bool:
        """Check if this tool should have a desktop icon."""
        return "Icon" in self._get_tags(metadata)

    def _wants_alias(self, metadata: Optional[ToolMetadata] = None) -> bool:
        """Tool wants a bash alias either because it's CLI-only (no Icon),
        or because an explicit alias name is set alongside the Icon."""
        m = metadata if metadata is not None else self.metadata
        return (not self._has_icon(m)) or bool(m.alias)

    def install(self, desktop_file: Optional[str] = None) -> None:
        """Install one or all variants of the tool.

        Args:
            desktop_file: Restrict to the variant whose ``desktop_file`` matches.
                If None, installs every variant registered on this installer.
        """
        self.install_dependencies()
        for m in self._select(desktop_file):
            self._install_one(m)

    def remove(self, desktop_file: Optional[str] = None) -> None:
        """Remove one or all variants of the tool."""
        for m in self._select(desktop_file):
            self._remove_one(m)

    def _install_one(self, metadata: ToolMetadata) -> None:
        """Install a single variant."""
        if not self._has_icon(metadata):
            self._install_cli_alias(metadata)
            return

        self._ensure_apps_dir()

        for field_name in ("name", "desc", "categories", "icon", "desktop_file"):
            _check_desktop_field(getattr(metadata, field_name), field_name)
        for i, arg in enumerate(metadata.args or []):
            _check_desktop_field(arg, f"args[{i}]")

        if host.IS_WINDOWS:
            self._install_windows_shortcut(metadata)
            if metadata.alias:
                self._install_cli_alias(metadata)
            return

        desktop_path = os.path.join(self.apps_dir, metadata.desktop_file)

        exec_line = f"{shlex.quote(sys.executable)} {shlex.quote(self.script_path)}"
        if metadata.args:
            exec_line += " " + " ".join(shlex.quote(a) for a in metadata.args)

        if metadata.terminal:
            exec_line = f"konsole -e {exec_line}"

        wm_class = os.path.splitext(metadata.desktop_file)[0]

        content = f"""[Desktop Entry]
Type=Application
Name={metadata.name}
Comment={metadata.desc}
Exec={exec_line}
Path={self.script_dir}
Icon={metadata.icon}
Terminal=false
Categories={metadata.categories}
Keywords={self.identity.desktop_keywords}
StartupNotify=true
StartupWMClass={wm_class}
"""

        with open(desktop_path, "w") as f:
            f.write(content)

        # Deliberately NOT executable. A .desktop in the applications dir needs
        # no exec bit, and an executable one makes systemd-xdg-autostart-generator
        # warn on every login once the entry is symlinked into ~/.config/autostart.
        os.chmod(desktop_path, os.stat(desktop_path).st_mode & ~_EXEC_BITS)

        print(colored(f"Installed: {desktop_path}", "green"))
        self._refresh_desktop_database()

        if metadata.alias:
            self._install_cli_alias(metadata)

    def _remove_one(self, metadata: ToolMetadata) -> None:
        """Remove a single variant."""
        if not self._has_icon(metadata):
            self._remove_cli_alias(metadata)
            return

        if host.IS_WINDOWS:
            self._remove_windows_shortcut(metadata)
            if metadata.alias:
                self._remove_cli_alias(metadata)
            return

        desktop_path = os.path.join(self.apps_dir, metadata.desktop_file)

        if os.path.exists(desktop_path):
            os.remove(desktop_path)
            print(colored(f"Removed: {desktop_path}", "green"))
            self._refresh_desktop_database()
        else:
            print(colored(f"Shortcut not found: {desktop_path}", "yellow"))

        if metadata.alias:
            self._remove_cli_alias(metadata)

    def _refresh_desktop_database(self) -> None:
        """Refresh desktop database caches."""
        if host.IS_WINDOWS:
            return
        for cmd in ["update-desktop-database", "kbuildsycoca5"]:
            try:
                args = [cmd]
                if cmd == "update-desktop-database":
                    args.append(self.apps_dir)
                subprocess.run(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except FileNotFoundError:
                pass

    def get_advertise_data(self) -> list:
        """Get JSON-serializable data for --advertise output.

        Returns one dict per registered variant.
        """
        out = []
        for m in self.metadatas:
            tags = self._get_tags(m)
            data = {
                "name": m.name,
                "desktop_file": m.desktop_file,
                "icon": m.icon,
                "desc": m.desc,
                "terminal": m.terminal,
                "args": m.args or [],
                "tags": tags,
            }
            if self._wants_alias(m):
                data["alias"] = self._get_alias_name(m)
                if m.alias_args is not None:
                    data["alias_args"] = list(m.alias_args)
            out.append(data)
        return out

    def _get_alias_name(self, metadata: Optional[ToolMetadata] = None) -> str:
        """Get the alias name for a variant."""
        m = metadata if metadata is not None else self.metadata
        if m.alias:
            return m.alias
        # Default: derive from desktop_file (e.g., "git_commit.desktop" -> "git_commit")
        return os.path.splitext(m.desktop_file)[0]

    def _get_aliases_file(self) -> str:
        """Path to this installer's alias file (namespaced by identity)."""
        return self.identity.aliases_path

    def _load_aliases(self) -> dict:
        """Load existing aliases from file. Returns dict of alias_name -> command."""
        aliases_file = self._get_aliases_file()
        aliases = {}
        if os.path.exists(aliases_file):
            with open(aliases_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("alias ") and "=" in line:
                        # Parse: alias name='command'
                        parts = line[6:].split("=", 1)
                        if len(parts) == 2:
                            name = parts[0].strip()
                            cmd = parts[1].strip()
                            # Strip a trailing unquoted comment (e.g. a tool's
                            # own `  # marker` tag) before unwrapping. Without
                            # this, a line like `alias n='cmd'  # tag` fails the
                            # closing-quote test below, so the inner quotes are
                            # kept and then double-wrapped on the next save —
                            # corrupting the alias. Only strip the comment when
                            # it sits outside a balanced quote (the quote count
                            # before the `#` is even).
                            hash_idx = cmd.find("#")
                            if hash_idx > 0:
                                before = cmd[:hash_idx]
                                if before.count("'") % 2 == 0 and \
                                   before.count('"') % 2 == 0:
                                    cmd = before.strip()
                            # Only strip outer wrapping quotes (not inner quotes in the command)
                            if (cmd.startswith("'") and cmd.endswith("'")) or \
                               (cmd.startswith('"') and cmd.endswith('"')):
                                cmd = cmd[1:-1]
                            aliases[name] = cmd
        return aliases

    def _save_aliases(self, aliases: dict) -> None:
        """Save aliases to file.

        Uses shlex.quote on the command so a `'` (or other shell metachar) in
        the script path or in metadata.alias_args can't break out of the alias
        and execute arbitrary shell on the next `source ~/.tools_aliases`.
        """
        aliases_file = self._get_aliases_file()
        with open(aliases_file, "w") as f:
            f.write("# Auto-generated by tools installer - do not edit manually\n")
            f.write("# Source this file in your .bashrc/.zshrc:\n")
            f.write(f"#   [ -f {aliases_file} ] && source {aliases_file}\n\n")
            for name, cmd in sorted(aliases.items()):
                f.write(f"alias {name}={shlex.quote(cmd)}\n")

    def _ensure_bashrc_sources_aliases(self) -> bool:
        """Ensure .bashrc sources the tools aliases file.

        Returns:
            True if bashrc was modified, False if already configured.
        """
        aliases_file = self._get_aliases_file()
        marker = os.path.basename(aliases_file)
        bashrc_path = os.path.join(os.path.expanduser("~"), ".bashrc")
        source_line = f'[ -f "{aliases_file}" ] && source "{aliases_file}"'

        # Check if already sourced. Matched on THIS identity's alias filename,
        # so a second org's installer adds its own line instead of seeing the
        # first one's and concluding it is done.
        if os.path.exists(bashrc_path):
            with open(bashrc_path, "r") as f:
                content = f.read()
                # Check for various forms of the source line
                if marker in content:
                    return False

        # Add source line to bashrc
        with open(bashrc_path, "a") as f:
            f.write(f"\n{self._bashrc_comment()}\n")
            f.write(f"{source_line}\n")

        return True

    def _bashrc_comment(self) -> str:
        """The comment line that labels this installer's block in ~/.bashrc."""
        return f"# {self.identity.notify_label} aliases (auto-added by installer)"

    def _remove_bashrc_source_if_empty(self) -> None:
        """Remove the source line from .bashrc if no aliases remain."""
        aliases_file = self._get_aliases_file()

        # Check if aliases file is empty or only has comments
        if os.path.exists(aliases_file):
            aliases = self._load_aliases()
            if aliases:
                return  # Still have aliases, keep the source line

        # Remove from bashrc
        bashrc_path = os.path.join(os.path.expanduser("~"), ".bashrc")
        if not os.path.exists(bashrc_path):
            return

        with open(bashrc_path, "r") as f:
            lines = f.readlines()

        # Filter out this installer's alias block. Both the current comment
        # and the pre-0.2.2 first-party one are recognised, so an old ~/.bashrc
        # still gets cleaned up.
        marker = os.path.basename(aliases_file)
        comments = {
            self._bashrc_comment(),
            "# Tools aliases (auto-added by tools installer)",
        }
        new_lines = []
        skip_next = False
        for line in lines:
            if any(c in line for c in comments):
                skip_next = True
                continue
            if skip_next and marker in line:
                skip_next = False
                continue
            skip_next = False
            new_lines.append(line)

        # Remove trailing empty lines
        while new_lines and new_lines[-1].strip() == "":
            new_lines.pop()

        with open(bashrc_path, "w") as f:
            f.writelines(new_lines)
            if new_lines and not new_lines[-1].endswith("\n"):
                f.write("\n")

    def _install_cli_alias(self, metadata: Optional[ToolMetadata] = None) -> None:
        """Install a bash alias for CLI-only tools (or for an Icon tool with alias=...)."""
        m = metadata if metadata is not None else self.metadata
        alias_name = self._get_alias_name(m)

        # alias_args takes precedence over args for the alias command, letting
        # a tool's alias auto-inject flags (e.g. --clip) that should NOT be
        # passed through during --install / .desktop launches.
        alias_cmd_args = m.alias_args if m.alias_args is not None else m.args

        if host.IS_WINDOWS:
            self._install_windows_shims(alias_name, alias_cmd_args or [])
            return

        cmd = f'{sys.executable} "{self.script_path}"'
        if alias_cmd_args:
            cmd += " " + " ".join(alias_cmd_args)

        aliases = self._load_aliases()
        aliases[alias_name] = cmd
        self._save_aliases(aliases)

        bashrc_modified = self._ensure_bashrc_sources_aliases()

        aliases_file = self._get_aliases_file()
        print(colored(f"Alias '{alias_name}' added to {aliases_file}", "green"))
        if bashrc_modified:
            print(colored("Added source line to ~/.bashrc", "green"))
        print(colored(host.shell_hint(), "cyan"))

    def _remove_cli_alias(self, metadata: Optional[ToolMetadata] = None) -> None:
        """Remove a bash alias for a variant."""
        m = metadata if metadata is not None else self.metadata
        alias_name = self._get_alias_name(m)

        if host.IS_WINDOWS:
            shim_dir = host.shim_dir(self.identity)
            removed = host.remove_shims(shim_dir, alias_name)
            if removed:
                print(colored(f"Command '{alias_name}' removed from {shim_dir}", "green"))
            else:
                print(colored(f"Command '{alias_name}' not found.", "yellow"))
            return

        aliases = self._load_aliases()

        if alias_name in aliases:
            del aliases[alias_name]
            self._save_aliases(aliases)
            print(colored(f"Alias '{alias_name}' removed.", "green"))

            self._remove_bashrc_source_if_empty()
        else:
            print(colored(f"Alias '{alias_name}' not found.", "yellow"))

    # --- Windows ---------------------------------------------------------

    def _install_windows_shims(self, alias_name: str, args: list) -> None:
        """Write the two launcher scripts and put their directory on PATH."""
        shim_dir = host.shim_dir(self.identity)
        host.write_shims(shim_dir, alias_name, sys.executable, self.script_path, args)
        changed = host.ensure_user_path(shim_dir)
        print(colored(f"Command '{alias_name}' installed in {shim_dir}", "green"))
        if changed:
            print(colored(f"Added {shim_dir} to your PATH", "green"))
        print(colored(host.shell_hint(), "cyan"))

    def _windows_shortcut_path(self, metadata: ToolMetadata) -> str:
        stem = os.path.splitext(metadata.desktop_file)[0]
        return os.path.join(host.start_menu_dir(), stem + ".lnk")

    def _install_windows_shortcut(self, metadata: ToolMetadata) -> None:
        """A Start Menu .lnk in place of the .desktop file."""
        lnk_path = self._windows_shortcut_path(metadata)
        args = list(metadata.args or [])
        if metadata.terminal:
            target = os.environ.get("COMSPEC", "cmd.exe")
            arg_line = " ".join(
                f'"{a}"' for a in ["/k", sys.executable, self.script_path] + args
            )
        else:
            target = sys.executable
            arg_line = " ".join(f'"{a}"' for a in [self.script_path] + args)
        ok = host.write_shortcut(lnk_path, target, arg_line,
                                 terminal=metadata.terminal, workdir=self.script_dir)
        if ok:
            print(colored(f"Installed: {lnk_path}", "green"))
        else:
            print(colored(f"Warning: could not write {lnk_path}", "yellow"))

    def _remove_windows_shortcut(self, metadata: ToolMetadata) -> None:
        lnk_path = self._windows_shortcut_path(metadata)
        if host.remove_shortcut(lnk_path):
            print(colored(f"Removed: {lnk_path}", "green"))
        else:
            print(colored(f"Shortcut not found: {lnk_path}", "yellow"))
