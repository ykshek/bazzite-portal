"""Bounded, versioned preferences. No shell commands are stored here."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import shutil
from bazzite_portal.artwork.appearance import THEMES

MAX_BYTES = 1024 * 1024
DEFAULTS = {"show_indicator": True, "open_at_login": False, "show_logs": False,
            "show_disabled_update_steps": False,
            "show_unsupported_rebase_targets": False,
            "skip_one_shot_confirmation": False,
            "always_show_controller_prompts": False,
            "controller_prompt_style": "auto",
            "notification_icon_theme": "spinning",
            "gtk_theme": "system",
            "color_scheme": "system"}
PROMPT_STYLES = frozenset(("auto", "xbox", "playstation"))
COLOR_SCHEMES = frozenset(("system", "light", "dark"))
_THREAD_LOCK = threading.RLock()


class StorageError(Exception):
    pass


class Store:
    def __init__(self, base=None):
        self.base = Path(base or os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).absolute()
        self.root = self.base / "bazzite-portal"
        self.errors = []

    def _check(self):
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise StorageError("The Bazzite Portal configuration directory is not a safe directory.")
        if self.root.exists() and hasattr(os, "getuid") and self.root.stat().st_uid != os.getuid():
            raise StorageError("The configuration directory belongs to another user.")

    @contextmanager
    def locked(self):
        # OS advisory locks are process scoped on Unix and byte-range locks on
        # Windows. Serialize threads too, before touching the shared lock byte.
        with _THREAD_LOCK:
            with self._locked():
                yield

    @contextmanager
    def _locked(self):
        self._check()
        self.base.mkdir(parents=True, exist_ok=True)
        lock = self.base / ".bazzite-portal.lock"
        if lock.is_symlink():
            raise StorageError("Refusing linked lock file.")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as stream:
            if os.name == "nt":
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _path(self, name):
        if name not in ("preferences", "pins", "dismissed"):
            raise StorageError("Unknown configuration file.")
        self._check()
        path = self.root / (name + ".json")
        if path.is_symlink():
            raise StorageError(f"Refusing linked configuration file: {path.name}")
        return path

    def _read(self, name):
        path = self._path(name)
        if not path.exists():
            return {}
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_BYTES:
            raise StorageError(f"{path.name} is not a supported configuration file.")
        # Bound reads even if another program grows the file between stat and read.
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise StorageError(f"{path.name} is too large.")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            # Keep the broken file in place; a recovery copy is made before a write.
            raise StorageError(f"{path.name} is damaged; defaults are being used.") from error
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise StorageError(f"{path.name} has an unsupported format; defaults are being used.")
        return data

    def read(self, name):
        try:
            return self._read(name)
        except (OSError, StorageError) as error:
            message = str(error)
            if message not in self.errors:
                self.errors.append(message)
            return {}

    def preferences(self):
        data = self.read("preferences")
        result = {}
        for key, default in DEFAULTS.items():
            value = data.get(key)
            if type(default) is bool:
                result[key] = value if type(value) is bool else default
            elif key == "controller_prompt_style":
                result[key] = value if isinstance(value, str) and value in PROMPT_STYLES else default
            elif key == "notification_icon_theme":
                result[key] = value if isinstance(value, str) and value in THEMES else default
            elif key == "gtk_theme":
                result[key] = value if isinstance(value, str) and value and len(value) <= 128
                                        and value not in (".", "..")
                                        and not any(char in value for char in ("/", "\\", "\0"))
                                        and all(ord(char) >= 32 for char in value) else default
            elif key == "color_scheme":
                result[key] = value if isinstance(value, str) and value in COLOR_SCHEMES else default
            else:
                result[key] = default
        return result

    def pins(self):
        pins = self.read("pins").get("items", [])
        if not isinstance(pins, list):
            return []
        result = []
        for pin in pins[:100]:
            if isinstance(pin, dict) and set(pin) <= {"category", "id", "title", "signature"} and all(isinstance(v, str) and len(v) <= 4096 for v in pin.values()) and "category" in pin and ("id" in pin or {"title", "signature"} <= set(pin)) and pin not in result:
                result.append(pin)
        return result

    def dismissed(self):
        data = self.read("dismissed")
        return {key: value for key, value in data.items() if key in ("run", "deployment", "notified_run", "result_viewed_run") and isinstance(value, str) and len(value) <= 4096}

    def _write(self, name, data):
        path = self._path(name)
        self.root.mkdir(mode=0o700, exist_ok=True)
        if path.exists():
            try:
                self._read(name)
            except StorageError:
                # Unknown newer formats must not be overwritten by an older app.
                try:
                    existing = json.loads(path.read_bytes()[:MAX_BYTES])
                except (ValueError, UnicodeError):
                    existing = None
                if isinstance(existing, dict) and existing.get("version") != 1:
                    raise StorageError("Clear local app data before replacing an unsupported configuration version.")
                backup = self.root / (name + ".json.recovery")
                if backup.is_symlink():
                    raise StorageError("Refusing linked recovery file.")
                if not backup.exists():
                    with path.open("rb") as source, backup.open("xb") as target:
                        target.write(source.read(MAX_BYTES))
        payload = json.dumps({**data, "version": 1}, ensure_ascii=False).encode()
        if len(payload) > MAX_BYTES:
            raise StorageError("Configuration is too large to save.")
        fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._path(name)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write(self, name, data):
        try:
            with self.locked():
                self._write(name, data)
            return True
        except (OSError, StorageError) as error:
            self.errors.append(str(error))
            return False

    def set_preference(self, key, value):
        if key not in DEFAULTS:
            return False
        if type(DEFAULTS[key]) is bool and type(value) is not bool:
            return False
        if key == "controller_prompt_style" and (not isinstance(value, str) or value not in PROMPT_STYLES):
            return False
        if key == "notification_icon_theme" and (not isinstance(value, str) or value not in THEMES):
            return False
        if key == "gtk_theme" and (not isinstance(value, str) or not value or len(value) > 128
                                   or value in (".", "..")
                                   or any(char in value for char in ("/", "\\", "\0"))
                                   or any(ord(char) < 32 for char in value)):
            return False
        if key == "color_scheme" and (not isinstance(value, str) or value not in COLOR_SCHEMES):
            return False
        try:
            with self.locked():
                data = self.preferences()
                data[key] = value
                self._write("preferences", data)
            return True
        except (OSError, StorageError) as error:
            self.errors.append(str(error))
            return False

    def dismiss(self, run, deployment):
        return self._update_reminders({"run": run, "deployment": deployment})

    def mark_notified(self, run):
        return self._update_reminders({"notified_run": run})

    def mark_update_viewed(self, run):
        return self._update_reminders({"result_viewed_run": run})

    def _update_reminders(self, changes):
        try:
            with self.locked():
                self._write("dismissed", {**self.dismissed(), **changes})
            return True
        except (OSError, StorageError) as error:
            self.errors.append(str(error))
            return False

    def reset(self):
        try:
            with self.locked():
                if self.root.exists():
                    # unlink symlinks themselves; never descend through them.
                    self._check()
                    if self.root.resolve() != self.base.resolve() / "bazzite-portal":
                        raise StorageError("Unexpected configuration reset target.")
                    if shutil.rmtree.avoids_symlink_attacks:
                        shutil.rmtree(self.root)
                    else:
                        for directory, dirs, files in os.walk(self.root, topdown=False, followlinks=False):
                            for name in files:
                                Path(directory, name).unlink()
                            for name in dirs:
                                child = Path(directory, name)
                                child.unlink() if child.is_symlink() else child.rmdir()
                        self.root.rmdir()
                startup = self.base / "autostart" / "io.github.Xarishark.bazzite_portal-window.desktop"
                if startup.parent.is_symlink():
                    raise StorageError("Refusing linked autostart directory.")
                startup.unlink(missing_ok=True)
                self.errors.clear()
            return True
        except (OSError, StorageError) as error:
            self.errors.append(str(error))
            return False
