from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jira_workbench.config import secure_config_permissions


@pytest.mark.skipif(os.name != "posix", reason="file permission bits are POSIX-specific")
def test_secure_config_permissions_tightens_group_and_other_readable(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('jira_api_token = "secret"\n')
    path.chmod(0o644)

    changed = secure_config_permissions(path)

    assert changed is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="file permission bits are POSIX-specific")
def test_secure_config_permissions_noop_when_already_restrictive(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('jira_api_token = "secret"\n')
    path.chmod(0o600)

    changed = secure_config_permissions(path)

    assert changed is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
