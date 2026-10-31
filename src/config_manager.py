"""
==========================================================
FillPac AI
Configuration Manager
==========================================================

Single place responsible for READING and WRITING config.yaml
on behalf of the dashboard's Settings API (dashboard/backend/
server.py).

This is deliberately separate from src.config.Config:

    src.config.Config
        - owned by the running Application/Pipeline objects
        - loads config.yaml ONCE at process startup
        - normalizes global -> per-camera defaults
        - read-only from the pipeline's point of view

    src.config_manager.ConfigManager
        - owned by the dashboard API only
        - reads/writes the RAW config.yaml (no normalization,
          so a save doesn't flatten global defaults into every
          camera block and blow up the file size)
        - never touches a running Pipeline/Detector -- see the
          note in server.py above the /api/settings routes for
          why saved changes require a service restart to apply

Every write is preceded by:

    1. Validate the candidate content by round-tripping it
       through src.config.Config in a temp file. This reuses
       Config's existing structural checks (duplicate camera
       IDs, malformed camera entries, etc.) instead of
       duplicating them here.
    2. Back up the current config.yaml (timestamped copy under
       config_backups/) before writing the new one.
    3. Write to a temp file in the same directory, then
       os-replace it over config.yaml, so a crash mid-write
       never leaves a half-written config.yaml on disk.
==========================================================
"""

import shutil
import tempfile

from datetime import datetime, timezone
from pathlib import Path

import yaml

try:
    # Optional: with ruamel.yaml installed, saving from the Settings
    # page keeps every comment, quote style and blank line in
    # config.yaml (TEST/production notes, commented-out RTSP sources,
    # old camera blocks). Without it, the file is rewritten with
    # PyYAML and comments are lost.   pip install ruamel.yaml
    from ruamel.yaml import YAML as _RuamelYAML
except ImportError:  # pragma: no cover
    _RuamelYAML = None

from src.config import Config


# ==========================================================
# ERRORS
# ==========================================================

class ConfigValidationError(Exception):
    """Raised when a candidate config.yaml fails validation."""


# ==========================================================
# CONFIG MANAGER
# ==========================================================

class ConfigManager:

    # Keep only this many backups so config_backups/ doesn't
    # grow forever on a long-running install.
    MAX_BACKUPS = 50

    def __init__(self, config_path, backup_dir=None):

        self.config_path = Path(config_path)

        self.backup_dir = (
            Path(backup_dir)
            if backup_dir
            else self.config_path.parent / "config_backups"
        )

        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------
    # READ
    # ------------------------------------------------------

    def load(self):
        """Return the raw parsed config.yaml as a dict."""

        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}"
            )

        with open(self.config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        if not isinstance(data, dict):
            raise ConfigValidationError(
                "Root YAML configuration must be a dictionary/object."
            )

        return data

    # ------------------------------------------------------
    # VALIDATE
    # ------------------------------------------------------

    def validate(self, data):
        """
        Raise ConfigValidationError if `data` would not be a
        loadable, valid config.yaml for the AI pipeline.

        Reuses src.config.Config's own validation by writing
        `data` to a throwaway temp file and constructing a
        Config from it -- Config() runs _load() -> _normalize()
        -> validate() and raises on anything it doesn't accept.
        """

        if not isinstance(data, dict):
            raise ConfigValidationError(
                "Root YAML configuration must be a dictionary/object."
            )

        tmp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".yaml",
                encoding="utf-8",
                delete=False,
            ) as tmp:
                yaml.safe_dump(data, tmp, sort_keys=False)
                tmp_path = tmp.name

            Config(config_path=tmp_path)

        except ConfigValidationError:
            raise

        except Exception as error:
            raise ConfigValidationError(str(error)) from error

        finally:
            if tmp_path is not None:
                Path(tmp_path).unlink(missing_ok=True)

    # ------------------------------------------------------
    # DIFF (for the "Configuration Changes" confirmation screen)
    # ------------------------------------------------------

    def diff(self, old_data, new_data):
        """
        Flat list of leaf-level changes between two config
        dicts: [{"path": "cameras.0.bag_spacing.jam_threshold_mm",
                  "old": 655.0, "new": 700.0}, ...]
        """

        changes = []
        self._diff_walk(old_data or {}, new_data or {}, [], changes)
        return changes

    def _diff_walk(self, old, new, path, changes):

        old_is_dict = isinstance(old, dict)
        new_is_dict = isinstance(new, dict)

        if old_is_dict or new_is_dict:
            old_map = old if old_is_dict else {}
            new_map = new if new_is_dict else {}

            for key in sorted(
                set(old_map.keys()) | set(new_map.keys()),
                key=str,
            ):
                self._diff_walk(
                    old_map.get(key),
                    new_map.get(key),
                    path + [str(key)],
                    changes,
                )
            return

        old_is_list = isinstance(old, list)
        new_is_list = isinstance(new, list)

        if old_is_list or new_is_list:
            old_list = old if old_is_list else []
            new_list = new if new_is_list else []

            for index in range(max(len(old_list), len(new_list))):
                old_item = old_list[index] if index < len(old_list) else None
                new_item = new_list[index] if index < len(new_list) else None
                self._diff_walk(
                    old_item,
                    new_item,
                    path + [str(index)],
                    changes,
                )
            return

        if old != new:
            changes.append(
                {
                    "path": ".".join(path),
                    "old": old,
                    "new": new,
                }
            )

    # ------------------------------------------------------
    # BACKUP
    # ------------------------------------------------------

    def _backup(self):

        if not self.config_path.exists():
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = self.backup_dir / f"config-{timestamp}.yaml"

        shutil.copy2(self.config_path, backup_path)

        # Prune anything past MAX_BACKUPS, oldest first.
        backups = sorted(self.backup_dir.glob("config-*.yaml"))
        for old_backup in backups[: -self.MAX_BACKUPS]:
            old_backup.unlink(missing_ok=True)

        return str(backup_path)

    # ------------------------------------------------------
    # WRITE
    # ------------------------------------------------------

    def save(self, new_data):
        """
        Validate, back up, then atomically write `new_data` as
        the new config.yaml.

        Returns {"backup_path": ..., "changes": [...]}.
        """

        old_data = (
            self.load()
            if self.config_path.exists()
            else {}
        )

        self.validate(new_data)

        backup_path = self._backup()

        self._atomic_write(new_data)

        return {
            "backup_path": backup_path,
            "changes": self.diff(old_data, new_data),
            "comments_preserved": self.comments_preserved,
        }

    # True when the last write kept the file's comments.
    comments_preserved = False

    @staticmethod
    def _make_round_trip_yaml():

        round_trip = _RuamelYAML()
        round_trip.preserve_quotes = True
        round_trip.width = 4096
        # Matches the existing file layout ("- id: 1" at the same
        # indent as its parent key).
        round_trip.indent(mapping=2, sequence=2, offset=0)

        return round_trip

    @classmethod
    def _merge_into(cls, target, new):
        """
        Apply `new` (plain dict/list/scalars from the browser) onto
        `target` (ruamel's comment-carrying structure) IN PLACE, so
        comments stay attached to their keys. Returns the object the
        caller should store (the same `target` object when possible).
        """

        if isinstance(target, dict) and isinstance(new, dict):

            for key in list(target.keys()):
                if key not in new:
                    del target[key]

            for key, value in new.items():

                if key in target:
                    merged = cls._merge_into(target[key], value)
                    if merged is not target[key]:
                        target[key] = merged
                else:
                    target[key] = value

            return target

        if isinstance(target, list) and isinstance(new, list):

            for index, value in enumerate(new):

                if index < len(target):
                    merged = cls._merge_into(target[index], value)
                    if merged is not target[index]:
                        target[index] = merged
                else:
                    target.append(value)

            del target[len(new):]

            return target

        # Scalars: keep the original object (and its formatting,
        # e.g. 0.10 or 'quoted') when the value is unchanged.
        if (
            isinstance(target, bool) == isinstance(new, bool)
            and target == new
        ):
            return target

        return new

    def _atomic_write(self, data):

        tmp_path = self.config_path.with_suffix(".yaml.tmp")

        self.comments_preserved = False

        if _RuamelYAML is not None and self.config_path.exists():

            try:

                round_trip = self._make_round_trip_yaml()

                with open(
                    self.config_path, "r", encoding="utf-8"
                ) as fh:
                    document = round_trip.load(fh)

                if isinstance(document, dict):

                    self._merge_into(document, data)

                    with open(tmp_path, "w", encoding="utf-8") as fh:
                        round_trip.dump(document, fh)

                    tmp_path.replace(self.config_path)

                    self.comments_preserved = True

                    return

            except Exception:
                # Fall back to the plain PyYAML writer below rather
                # than failing the save.
                tmp_path.unlink(missing_ok=True)

        with open(tmp_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(
                data,
                fh,
                sort_keys=False,
                default_flow_style=False,
            )

        tmp_path.replace(self.config_path)

    # ------------------------------------------------------
    # BACKUPS / RESET
    # ------------------------------------------------------

    def list_backups(self):
        """Newest first."""

        return sorted(
            (path.name for path in self.backup_dir.glob("config-*.yaml")),
            reverse=True,
        )

    def restore_backup(self, backup_name):
        """
        Restore config.yaml from a previously written backup.
        `backup_name` must be a bare filename returned by
        list_backups() -- no path separators allowed.
        """

        if "/" in backup_name or "\\" in backup_name or ".." in backup_name:
            raise FileNotFoundError(f"Invalid backup name: {backup_name}")

        backup_path = self.backup_dir / backup_name

        if not backup_path.is_file():
            raise FileNotFoundError(f"Backup not found: {backup_name}")

        with open(backup_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        self.validate(data)

        # Back up whatever's currently live before overwriting it,
        # so restoring a backup is itself undoable.
        self._backup()

        self._atomic_write(data)

        return data