"""cli-tools-kit — installer protocol + helpers for self-installing Python CLI/GUI tools.

Public API:

    from cli_tools_kit import (
        ToolInstaller, ToolMetadata,   # desktop file / bash alias install/remove
        CronInstaller,                 # idempotent cron-line management
        advertise,                     # --advertise JSON helper
        skill_status,                  # is an installed Claude skill stale?
        InstallerIdentity,             # which names your installer claims on a host
    )

The GUI installer engine is a submodule, since importing it pulls in tkinter:

    from cli_tools_kit.gui_installer import run

    run(identity=InstallerIdentity(slug="acme-tools"), root_dir=HERE,
        entry_script=__file__)

Tools that live in several repos are listed in an installer.toml and resolved
by cli_tools_kit.sources:

    from cli_tools_kit.sources import run_installer

    run_installer("installer.toml", identity=InstallerIdentity(slug="acme-tools"),
                  entry_script=__file__)

See README.md § Reusing the installer in your org for the full run() signature,
README.md § Sources for the TOML format, and `python3 -m cli_tools_kit` to be
walked through the setup.

See PROTOCOL.md for the full --advertise specification.
"""

from .tool_installer import ToolInstaller, ToolMetadata
from .identity import InstallerIdentity, LEGACY_IDENTITY
from .cron_installer import CronInstaller
from .advertise import advertise
from .skills import (
    installed_skill_hash,
    read_installed_skill,
    skill_payload_hash,
    skill_status,
)

__all__ = [
    "ToolInstaller",
    "ToolMetadata",
    "InstallerIdentity",
    "LEGACY_IDENTITY",
    "CronInstaller",
    "advertise",
    "skill_status",
    "skill_payload_hash",
    "installed_skill_hash",
    "read_installed_skill",
]

__version__ = "0.6.4"
