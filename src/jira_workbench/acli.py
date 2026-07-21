from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AcliError(RuntimeError):
    pass


@dataclass(frozen=True)
class AcliRunner:
    executable: str = "acli"

    def run(self, args: list[str], *, allow_failure: bool = False) -> str:
        command = [self.executable, *args]
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except FileNotFoundError as exc:
            if allow_failure:
                return ""
            executable = Path(self.executable).name
            raise AcliError(
                f"Atlassian CLI executable '{executable}' was not found. "
                "Install and authenticate Atlassian CLI, or pass --acli /path/to/acli."
            ) from exc
        except OSError as exc:
            if allow_failure:
                return ""
            raise AcliError(f"Could not run Atlassian CLI '{self.executable}': {exc}") from exc

        if result.returncode != 0:
            if allow_failure:
                return result.stdout.strip()
            stderr = result.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            raise AcliError(f"Atlassian CLI failed with exit {result.returncode}{detail}")
        return result.stdout.strip()

    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        output = self.run(args, allow_failure=allow_failure)
        if not output:
            return [] if allow_failure else None
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            if allow_failure:
                return []
            raise AcliError("Atlassian CLI returned invalid JSON.") from exc
