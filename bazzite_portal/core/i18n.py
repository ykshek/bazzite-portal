"""Gettext presentation, without localizing parser input or agent state."""
from functools import lru_cache
import gettext as _gettext
import json
import locale
from pathlib import Path
import re
from string import Formatter
import sys

DOMAIN = "bazzite-portal"
_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_LOCALE = _ROOT / "build" / "locale"
LOCALE_DIR = (_SOURCE_LOCALE if _SOURCE_LOCALE.is_dir()
              else Path(sys.prefix) / "share" / "locale")
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    # A missing OS locale must not prevent the app from opening.
    pass
_translation = None
_translation_language = None
_active_language = None
_FIXED_NAMES = frozenset({
    "Bazzite Portal", "Bazzite", "Steam", "Homebrew", "Flatpak", "Universal Blue",
    "GitHub", "GNOME", "KDE Plasma", "Xbox", "PlayStation", "RPM-OSTree", "UUPD",
    "Discord", "AppImages", "Stable", "Testing", "Unstable",
    "Proton", "Bootc", "bootc", "Distrobox", "HTTPS",
})
_CHANNEL_NAMES = {"stable": "Stable", "testing": "Testing", "unstable": "Unstable"}


def channel_name(track):
    """Official Bazzite channel names are identifiers, not translatable prose."""
    return _CHANNEL_NAMES[track]


def gettext(message):
    if message in _FIXED_NAMES:
        return message
    return _get_translation().gettext(message) if message else message


_ = gettext


def ngettext(singular, plural, count):
    return _get_translation().ngettext(singular, plural, count)


def pgettext(context, message):
    return _get_translation().pgettext(context, message)


def N_(message):
    """Mark an English wire/diagnostic value for translation at display time."""
    return message


LANGUAGE_OPTIONS = (
    ("system", N_("System default")),
    ("en", "English"),
    ("de", "Deutsch"),
    ("el", "Ελληνικά"),
    ("it", "Italiano"),
    ("pt_BR", "Português (Brasil)"),
    ("zh_Hant", "繁體中文"),
)
SUPPORTED_LANGUAGES = frozenset(language for language, _name in LANGUAGE_OPTIONS)


def C_(message, **values):
    """Format an English wire message; never translate shared backend state."""
    return message.format(**values)


def _get_translation():
    global _translation, _translation_language, _active_language
    if _active_language is None:
        from bazzite_portal.core.storage import Store
        _active_language = Store().preferences()["language"]
    if _translation is None or _translation_language != _active_language:
        arguments = {} if _active_language == "system" else {
            "languages": [_active_language],
        }
        _translation = _gettext.translation(
            DOMAIN, localedir=LOCALE_DIR, fallback=True, **arguments)
        _translation_language = _active_language
    return _translation


def set_language(language):
    """Refresh translations for the selected preference in this process."""
    global _active_language, _translation
    selected = (language if isinstance(language, str)
                and language in SUPPORTED_LANGUAGES else "system")
    if selected != _active_language:
        _active_language = selected
        _translation = None


@lru_cache(maxsize=1)
def _message_patterns():
    # Generated from literal C_ calls, shared by the UI and the independent agent.
    path = Path(__file__).with_name("message_templates.json")
    try:
        templates = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    result = []
    for template in templates:
        pieces, names = [], []
        for literal, name, spec, conversion in Formatter().parse(template):
            pieces.append(re.escape(literal))
            if name is not None:
                if name in names:
                    pieces.append(f"(?P={name})")
                else:
                    pieces.append(f"(?P<{name}>.*?)")
                    names.append(name)
        result.append((re.compile("".join(pieces), re.DOTALL), template))
    return tuple(result)


def format_message(message):
    """Translate known app messages only; external output remains verbatim.

    Full-message matching avoids translating fragments of logs or commands.
    This is a presentation boundary, never a parser or persisted-state API.
    """
    if not isinstance(message, str) or not message:
        return message
    translated = gettext(message)
    if translated != message:
        return translated
    # With no installed catalog there is nothing to translate or compile.
    if type(_get_translation()) is _gettext.NullTranslations:
        return message
    # Compose Brew progress subtitles from existing independently translated
    # labels. Package names stay verbatim, as they do in installation progress.
    progress = re.fullmatch(r"(Downloading|Updating) · ([0-9]+) of ([0-9]+)(?: - ([\w+@./-]+))?", message)
    if progress:
        counter = format_message(C_("{current} of {total}", current=progress[2], total=progress[3]))
        package = f" - {progress[4]}" if progress[4] else ""
        return f"{gettext(progress[1])} · {counter}{package}"
    for pattern, template in _message_patterns():
        match = pattern.fullmatch(message)
        if match is not None:
            translated = gettext(template)
            if translated != template:
                try:
                    return translated.format(**match.groupdict())
                except (KeyError, ValueError, IndexError):
                    # A broken community translation must not break operations.
                    return message
    return message
