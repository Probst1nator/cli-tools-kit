"""Who an installer is, so two orgs' installers can share a host.

Before this module every per-host artifact the engine touched was named after
the first-party tree: ``~/.config/tools-installer/config.json``,
``~/.tools_aliases``, ``ai_tools_manager.desktop``, the WM class
``tools_installer``, and the ``Keywords=probable.work;ai;tool;`` line that
doubles as branding *and* as the marker the orphan sweeper uses to decide
"this shortcut is mine". A second organisation reusing the engine therefore
overwrote the first one's desktop entry, shared its icon overrides and alias
file, and reaped its shortcuts.

:class:`InstallerIdentity` collects those names behind a single ``slug``. Give
it one word and every path, filename and marker derives from it; override any
individual field when a name has to be something else.

    from cli_tools_kit import InstallerIdentity

    ACME = InstallerIdentity(slug="acme-tools", title="Acme Tools")

    # ~/.config/acme-tools/, ~/.acme_tools_aliases, acme-tools.desktop,
    # WM class acme_tools, Keywords=acme-tools;ai;tool;

``LEGACY_IDENTITY`` reproduces the historical first-party names exactly and is
what the engine uses when a wrapper passes no identity, so existing installs
keep their files.
"""

import os
import re
from dataclasses import dataclass
from typing import Optional

# A slug lands in filesystem paths, a .desktop filename and the desktop
# Keywords line, so it stays to characters that are safe in all three.
# \Z, not $: "$" also matches just before a trailing newline, which would
# let "acme\n" through and inject a second key into the .desktop file.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")

DEFAULT_ICON = "system-software-install"


def _home(*parts: str) -> str:
    """A path under the home directory, kept unexpanded.

    Expansion happens in the properties below, at access time. Resolving "~"
    when the identity is *constructed* would freeze whatever HOME held at
    import — which silently sends a sandboxed test (or a process that changes
    HOME) back to the real home directory.
    """
    return os.path.join("~", *parts)


@dataclass(frozen=True)
class InstallerIdentity:
    """The names one organisation's installer claims on a host.

    Only ``slug`` is required. Every other field defaults to something derived
    from it; set one explicitly when you need a specific name (as
    ``LEGACY_IDENTITY`` does to keep the first-party filenames).
    """

    slug: str
    title: Optional[str] = None
    icon: str = DEFAULT_ICON

    # Desktop Keywords token. Both the branding on every shortcut this
    # installer writes and the marker its orphan sweeper matches on, so an org
    # only ever sweeps its own entries. Defaults to the slug.
    marker: Optional[str] = None

    # The manager's own shortcut and window.
    desktop_file: Optional[str] = None
    desktop_name: Optional[str] = None
    wm_class: Optional[str] = None
    notify_app: Optional[str] = None

    # Per-host state. Unnamespaced before 0.2.2; two orgs shared all three.
    aliases_file: Optional[str] = None
    config_dir: Optional[str] = None
    cache_dir: Optional[str] = None

    # Login update-check artifacts.
    check_desktop_name: Optional[str] = None
    check_log_name: Optional[str] = None
    check_state_name: Optional[str] = None

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.slug or ""):
            raise ValueError(
                f"slug must be lowercase letters, digits, '.', '-' or '_' and "
                f"start with a letter or digit, got {self.slug!r}"
            )

    # --- derived names ----------------------------------------------------
    #
    # Properties rather than __post_init__ assignment: the dataclass is frozen
    # (an identity is a value, not mutable config) and this keeps the stored
    # fields to exactly what the caller chose.

    @property
    def under(self) -> str:
        """The slug as a shell/WM-safe identifier: ``acme-tools`` → ``acme_tools``."""
        return re.sub(r"[.\-]", "_", self.slug)

    @property
    def display_title(self) -> str:
        """Window title. Falls back to the slug when no title was given."""
        return self.title or self.slug

    @property
    def desktop_keywords(self) -> str:
        """The full ``Keywords=`` value written into every shortcut."""
        return f"{self.marker or self.slug};ai;tool;"

    @property
    def marker_token(self) -> str:
        """The token :func:`find_orphan_desktop_files` matches to claim a file."""
        return self.marker or self.slug

    @property
    def self_desktop_file(self) -> str:
        return self.desktop_file or f"{self.slug}-installer.desktop"

    @property
    def self_desktop_name(self) -> str:
        return self.desktop_name or self.display_title

    @property
    def self_wm_class(self) -> str:
        return self.wm_class or f"{self.under}_installer"

    @property
    def notify_label(self) -> str:
        return self.notify_app or self.display_title

    @property
    def aliases_path(self) -> str:
        return os.path.expanduser(self.aliases_file or _home(f".{self.under}_aliases"))

    @property
    def config_path(self) -> str:
        return os.path.expanduser(self.config_dir or _home(".config", self.slug))

    @property
    def config_file(self) -> str:
        return os.path.join(self.config_path, "config.json")

    @property
    def icons_dir(self) -> str:
        return os.path.join(self.config_path, "icons")

    @property
    def cache_path(self) -> str:
        return os.path.expanduser(self.cache_dir or _home(".cache", self.slug))

    @property
    def shim_path(self) -> str:
        """Where this installer's Windows launcher scripts go.

        ``%LOCALAPPDATA%\\<slug>\\bin`` — the directory added to the user PATH
        on Windows in place of the alias file. Derived from the slug like every
        other per-host name, so two orgs on one host keep separate shims.
        """
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            _home("AppData", "Local")
        )
        return os.path.join(base, self.slug, "bin")

    @property
    def check_desktop(self) -> str:
        return self.check_desktop_name or f"{self.slug}-check.desktop"

    @property
    def check_log(self) -> str:
        return self.check_log_name or f"{self.slug}-check.log"

    @property
    def check_state(self) -> str:
        return self.check_state_name or f"{self.slug}-check.json"

    # --- interop ----------------------------------------------------------

    ENV_VAR = "CLI_TOOL_KIT_SLUG"

    def env(self) -> dict:
        """Environment additions that let a child process rebuild this identity.

        The engine installs a tool by running that tool's own ``--install`` in a
        subprocess, where the tool's :class:`~cli_tools_kit.ToolInstaller` writes
        the .desktop file and the alias. Passing the slug down means those
        artifacts carry the parent installer's marker and land in its alias
        file, instead of the first-party defaults.
        """
        return {self.ENV_VAR: self.slug}

    @classmethod
    def from_env(cls, default: Optional["InstallerIdentity"] = None) -> "InstallerIdentity":
        """Rebuild the calling installer's identity from the environment.

        Returns ``default`` (or :data:`LEGACY_IDENTITY`) when the variable is
        unset or malformed, so a tool run by hand still installs normally.

        The legacy slug returns :data:`LEGACY_IDENTITY` itself. A slug alone
        cannot rebuild an identity that overrides its names, and
        ``InstallerIdentity(slug="probable.work")`` would write the aliases to
        ``~/.probable_work_aliases`` instead of ``~/.tools_aliases``.
        """
        slug = os.environ.get(cls.ENV_VAR, "").strip()
        fallback = default if default is not None else LEGACY_IDENTITY
        if not slug:
            return fallback
        if slug == LEGACY_IDENTITY.slug:
            return LEGACY_IDENTITY
        try:
            return cls(slug=slug)
        except ValueError:
            return fallback


# The names the engine used before identities existed. Passing no identity
# selects this, so a host that already has first-party shortcuts, aliases and
# icon overrides keeps them.
LEGACY_IDENTITY = InstallerIdentity(
    slug="probable.work",
    title="probable.work - Tools Installer",
    desktop_file="ai_tools_manager.desktop",
    desktop_name="Tools Installer",
    wm_class="tools_installer",
    notify_app="Tools Installer",
    aliases_file=_home(".tools_aliases"),
    config_dir=_home(".config", "tools-installer"),
    cache_dir=_home(".cache", "tools-installer"),
    check_desktop_name="tools-installer-check.desktop",
    check_log_name="tools-installer-check.log",
    check_state_name="tools-installer-check.json",
)
