"""Atomic state snapshots; writers are serialized by the CLI file lock."""

import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from city_folders.models import CITIES, PilotError, ProjectState


class StateStore:
    """Persist small snapshots after each completed external operation."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ProjectState:
        if not self.path.exists():
            state = ProjectState()
            self.save(state)
            return state
        try:
            state = ProjectState.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
            CITIES.update(state.cities)
            from city_folders.weather import LOCATIONS

            for city in state.cities.values():
                LOCATIONS[city.id] = (
                    city.latitude,
                    city.longitude,
                    city.timezone,
                )
            return state
        except (ValidationError, ValueError) as exc:
            raise PilotError(
                "Файл состояния повреждён. Восстанови резервную копию; "
                "не удаляй его для повторного создания каналов."
            ) from exc

    def save(self, state: ProjectState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=self.path.parent
        )
        temporary_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            if os.name == "posix":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary_path.unlink(missing_ok=True)
