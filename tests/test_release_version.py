"""One release version everywhere: the install script's default tag, the package, the plugin
manifest, the marketplace entry and the README's install command (FR2.2, a release is a
semantic-version tag)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import telegram_mcp

ROOT = Path(__file__).resolve().parents[1]
SEMVER = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
RAW_SCRIPT = "https://raw.githubusercontent.com/maksymskuibida/telegram-mcp/{tag}/install.sh"


def pyproject_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert match is not None
    return match[1]


def default_tag() -> str:
    match = re.search(r'^DEFAULT_TAG="([^"]+)"$', (ROOT / "install.sh").read_text(), re.M)
    assert match is not None
    return match[1]


def test_the_version_is_a_semantic_version() -> None:
    assert re.fullmatch(SEMVER, pyproject_version())


def test_every_version_field_equals_the_package_version() -> None:
    version = pyproject_version()
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert telegram_mcp.__version__ == version
    assert plugin["version"] == version
    assert [entry["version"] for entry in marketplace["plugins"]] == [version]


def test_the_install_script_installs_the_tag_of_this_version() -> None:
    assert default_tag() == "v" + pyproject_version()


def test_the_readme_install_command_names_the_same_tag() -> None:
    readme = (ROOT / "README.md").read_text()
    tag = default_tag()
    assert f"curl -fsSL {RAW_SCRIPT.format(tag=tag)} | sh" in readme
    named = set(
        re.findall(r"raw\.githubusercontent\.com/maksymskuibida/telegram-mcp/([^/]+)/", readme)
    )
    assert named == {tag}
    pinned = set(re.findall(r"maksymskuibida/telegram-mcp#(v\d[^\s`]*)", readme))
    assert pinned == {tag}
