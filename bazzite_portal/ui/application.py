"""GTK window. System operations and the session agent outlive this window."""
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from bazzite_portal import APP_ID
from bazzite_portal import VERSION
from bazzite_portal.ui.maintenance import STATE_LABELS, SystemManagementUI
from bazzite_portal.ui.widgets import (
    button, content_page, group, label, ControllerLegend, ControllerPrompt,
    make_button_prompt, make_context_prompt, set_window_content,
)
from bazzite_portal.integration import tools as portal
from bazzite_portal.integration import community
from bazzite_portal.artwork.appearance import gaming_mode
from bazzite_portal.integration import announcements
from bazzite_portal.integration import favicons
from bazzite_portal.integration import diagnostics
from bazzite_portal.integration import steam as steam_reset
from bazzite_portal.integration import steam as steam_shortcut
from bazzite_portal.artwork.icons import icon as app_icon
from bazzite_portal.input.controller import (
    SDLGamepad,
    ControllerDriver,
    ControllerPriority,
    controller_button_glyph,
)
from bazzite_portal.core.storage import Store
from bazzite_portal.core.gtk_themes import list_gtk4_themes
from bazzite_portal.core.i18n import C_, N_, _, channel_name, format_message
from bazzite_portal.core.errors import error_text, public_error
from bazzite_portal.input.prompts import set_prompt_visible
from bazzite_portal.ui import system_info as system_presentation
from bazzite_portal.updates import monitor as system
from bazzite_portal.updates.model import Snapshot
from bazzite_portal.updates.release_notes import fetch_release_notes

NOTIFICATION_THEMES_ENABLED = True
log = logging.getLogger(__name__)
PAGES = (N_("Home"), N_("Tools"), N_("Updates"), N_("System"), "Management", N_("Troubleshooting"), N_("Community"), N_("Announcements"), "Settings")
PAGE_LABELS = {
    "Home": 'Bazzite Portal', "Tools": _("Tools"), "Updates": _("Updates"),
    "System": _("System information"), "Management": _("System Management"),
    "Troubleshooting": _("Troubleshooting"), "Community": _("Community"),
    "Announcements": _("Announcements"), "Settings": _("Options"),
}
ICONS = ("go-home-symbolic", "applications-utilities-symbolic", "view-refresh-symbolic", "computer-symbolic", "mail-unread-symbolic", "emblem-system-symbolic")


def choice_label(options, selected):
    """Reserve the widest rendered option without stretching the selector."""
    choice = Gtk.Label(label=options[selected], xalign=0.5)
    sizing = Gtk.Stack(hhomogeneous=True, vhomogeneous=True, hexpand=False,
                       halign=Gtk.Align.CENTER)
    sizing.add_named(choice, "current")
    for index, text in enumerate(options):
        sizing.add_named(Gtk.Label(label=text, xalign=0.5), str(index))
    sizing.set_visible_child(choice)
    return choice, sizing


class UpdateActionButton(Gtk.Button):
    """CTA with a centered label and a contextual prompt inside its trailing edge."""

    def __init__(self, text, callback):
        super().__init__()
        text = format_message(text)
        self.action_label = Gtk.Label(label=text, halign=Gtk.Align.CENTER)
        self.action_content = Gtk.CenterBox()
        self.action_content.set_center_widget(self.action_label)
        self.prompt_slot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self.prompt_slot.set_size_request(42, 24)
        self.action_content.set_end_widget(self.prompt_slot)
        self.action_content.set_start_widget(Gtk.Box(width_request=42))
        self.set_child(self.action_content)
        self.controller_label = text
        self.update_property([Gtk.AccessibleProperty.LABEL], [text])
        self.connect("clicked", lambda *_: callback())

    def get_label(self):
        return self.action_label.get_text()

    def set_label(self, text):
        self.action_label.set_text(str(text))
        self.controller_label = str(text)
        self.update_property([Gtk.AccessibleProperty.LABEL], [str(text)])

    def set_prompt(self, prompt):
        prompt.set_size_request(24, 24)
        prompt.set_margin_start(8)
        prompt.set_margin_end(10)
        if (old := self.prompt_slot.get_first_child()) is not None:
            self.prompt_slot.remove(old)
        self.prompt_slot.append(prompt)
        self.action_prompt = prompt


class ModalOptionButton(Gtk.Button):
    """Retain a readable label when an overlay holds the moving prompt."""
    def get_label(self):
        return getattr(self, "controller_label", super().get_label())


def install_controller_focus_style():
    display = Gdk.Display.get_default()
    if display is None:
        return None
    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"toolbarview undershoot.bottom { background: none; box-shadow: none; } "
        b".bazzite-controller-focus { background-color: alpha(@accent_bg_color, 0.24); "
        b"box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.72); border-radius: 8px; } "
        b".bazzite-controller-immediate { transition: none; } "
        b"button.confirmation-choice:hover { background-color: alpha(@accent_bg_color, 0.20); "
        b"box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.55); } "
        b".bazzite-favorite { color: @warning_color; } "
        b"row.destructive-action .title { color: @error_color; } "
        b"button.bazzite-update-cta { background-color: #8A2BE2; color: #ffffff; } "
        b"button.bazzite-update-cta:hover { background-color: #7424C5; color: #ffffff; } "
        b"box.controller-style-pill { min-height: 36px; padding: 2px 6px; "
        b"background-color: alpha(@theme_fg_color, 0.06); border: 1px solid alpha(@theme_fg_color, 0.16); border-radius: 9px; } "
        b".layer-selection-row { min-height: 36px; padding: 2px 8px; border-radius: 8px; } "
        b".layer-selection-row:hover { background-color: alpha(@theme_fg_color, 0.055); } "
        b".layer-selection-footer { padding-top: 2px; } "
        b"box.community-favicon-tile { background-color: #f5f5f5; "
        b"border: 1px solid #dedede; border-radius: 8px; padding: 0; } "
        b"button.controller-style-arrow { background: transparent; box-shadow: none; padding: 3px; min-width: 28px; min-height: 28px; } "
        b"row.bazzite-update-active-row { background-color: alpha(#8A2BE2, 0.10); } "
        b"row.bazzite-update-step-row .title { font-size: 1.12em; font-weight: 600; } "
        b"row.bazzite-update-step-row .subtitle { font-size: 1.02em; } "
        b".release-table .table-cell, .release-table .table-header { padding: 8px; border: 1px solid alpha(@theme_fg_color, 0.15); } "
        b".release-table .table-header { background: alpha(@theme_fg_color, 0.08); } "
        b".release-table .alternate-row { background: alpha(@theme_fg_color, 0.04); } "
        b"textview.bazzite-update-log, textview.bazzite-update-log text { background-color: transparent; color: @theme_fg_color; }"
    )
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    return provider


def controller_focus_targets(root):
    """Return visible interactive controls in widget order, skipping text labels."""
    target_types = (Gtk.Button, Gtk.Switch, Gtk.SearchEntry, Gtk.Entry, Gtk.DropDown,
                    Gtk.SpinButton, Gtk.CheckButton, Gtk.Scale, Gtk.Expander)
    targets = []

    def visit(widget):
        if (not widget.get_visible() or not widget.get_mapped()
                or (not widget.get_sensitive() and not getattr(widget, "controller_read_only_focus", False))):
            return
        if getattr(widget, "controller_exclude_focus", False):
            return
        if isinstance(widget, Adw.NavigationView):
            page = widget.get_visible_page()
            if page is not None:
                visit(page)
            return
        if isinstance(widget, Gtk.Stack):
            current = widget.get_visible_child()
            child = widget.get_first_child()
            while child:
                if child is current:
                    visit(child)
                child = child.get_next_sibling()
            return
        if getattr(widget, "controller_focus_row", False):
            widget.set_focusable(True)
            targets.append(widget)
            return
        is_sidebar_row = (isinstance(widget, Gtk.ListBoxRow) and not isinstance(widget, Adw.ActionRow)
                          and not getattr(widget, "controller_skip_self", False))
        # GtkPopoverMenu creates GtkModelButton internally. Its accessible role
        # varies across GTK versions, so recognize the focusable menu item by
        # its GTK type as well as by the semantic role.
        is_menu_item = (
            widget.get_accessible_role() == Gtk.AccessibleRole.MENU_ITEM
            or type(widget).__name__ == "GtkModelButton"
        )
        is_control = isinstance(widget, target_types) or is_sidebar_row or is_menu_item
        if isinstance(widget, Gtk.Button) and not widget.get_label() and not getattr(widget, "controller_label", None):
            is_control = False
        if is_control:
            widget.set_focusable(True)
            targets.append(widget)
        child = widget.get_first_child()
        while child:
            visit(child)
            child = child.get_next_sibling()

    visit(root)
    search = getattr(root, "search", None)
    if getattr(root, "tools_search_active", False) and search in targets:
        targets.remove(search)
        targets.insert(0, search)
    return targets


def portal_status_presentation(token):
    """Match YAFTI's status icon/label mapping for a provided status token."""
    status = token.strip() if isinstance(token, str) else ""
    normalized = status.casefold()
    if normalized in {"install", "active", "enable", "add", "upgraded"}:
        icon = "🟢"
    elif normalized in {"uninstall", "inactive", "disable", "disabled", "remove", "unset", "mismatch"}:
        icon = "🟠"
    elif not normalized or normalized == "unknown":
        return "⚪", _("Unknown")
    else:
        icon = "🔵"
    labels = {"install": _("Installed"), "enable": _("Enabled"), "uninstall": _("Not installed"),
              "disable": _("Disabled"), "remove": _("Removed")}
    return icon, labels.get(normalized, status.capitalize())


def controller_focused_target(root):
    """Map GTK's focused inner text/cell widget to its interactive parent."""
    targets = controller_focus_targets(root)
    logical = getattr(root, "controller_focus_widget", None)
    if (getattr(root, "controller_navigation", False) and logical in targets
            and getattr(logical, "controller_read_only_focus", False)):
        return logical
    focus = root.get_focus()
    while focus:
        if any(focus is target for target in targets) or isinstance(focus, Gtk.SearchEntry):
            return focus
        focus = focus.get_parent()
    return None


def system_summary(info):
    status = info.get("deployment_status")
    deployment = None
    if isinstance(status, dict):
        deployments = status.get("deployments")
        if isinstance(deployments, list):
            deployment = next((item for item in deployments if isinstance(item, dict) and item.get("booted") is True), None)
        if deployment is None:
            state = status.get("status")
            booted = state.get("booted") if isinstance(state, dict) else None
            deployment = booted if isinstance(booted, dict) else None

    def value(*values):
        return next((str(item) for item in values if isinstance(item, (str, int, float)) and str(item).strip()), N_("Unavailable"))

    image = N_("Unavailable")
    deployment_version = None
    packages = N_("Unavailable")
    if deployment:
        image_data = deployment.get("image")
        image_data = image_data if isinstance(image_data, dict) else {}
        nested_image = image_data.get("image")
        nested_image = nested_image if isinstance(nested_image, dict) else {}
        image = value(deployment.get("container-image-reference"), deployment.get("origin"),
                      image_data.get("image") if isinstance(image_data.get("image"), str) else image_data.get("imageDigest"),
                      nested_image.get("image"))
        deployment_version = value(deployment.get("version"), image_data.get("version"), nested_image.get("version"))
        package_list = deployment.get("requested-packages", deployment.get("packages"))
        if isinstance(package_list, list):
            names = [str(item) for item in package_list if isinstance(item, str) and item]
            packages = ", ".join(names) if names else N_("None recorded")
    return {
        "Operating system": value(info.get("PRETTY_NAME"), info.get("NAME")),
        "Version": value(deployment_version if deployment else None, info.get("VERSION"), info.get("VERSION_ID")),
        "Image": image,
        "Layered packages": packages,
    }


class Window(Adw.ApplicationWindow, SystemManagementUI):
    def __init__(self, app, config_path):
        super().__init__(application=app, title='Bazzite Portal', default_width=1080, default_height=740)
        self.config_path = Path(config_path)
        self.status_received = False
        self.store = Store()
        self._pins = self.store.pins()
        self._update_console_open = bool(self.store.preferences().get("show_logs", False))
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="bazzite-portal")
        self.portal_status_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="bazzite-portal-status")
        self.snapshot = Snapshot()
        self.items = []
        self._dirty_tools = {}
        self._tool_status_requests = {}
        self.modal = None
        self.controller = None
        self.controller_navigation = False
        self.controller_connected = False
        self.controller_priority = ControllerPriority()
        self._native_focus_timeout = None
        self._controller_focus_sync_source = None
        self._last_pointer_position = {}
        self.controller_focus_widget = None
        self._controller_focus_established = False
        self.controller_focus_style = install_controller_focus_style()
        self.closing = False
        self.log_text = ""
        self.client = None
        self.notices = Adw.ToastOverlay()
        set_window_content(self, self.notices)
        toolbar = Adw.ToolbarView()
        self.toolbar = toolbar
        self.notices.set_child(toolbar)
        header = Adw.HeaderBar()
        self.header = header
        self.header_title = Adw.WindowTitle(title='Bazzite Portal')
        header.set_title_widget(self.header_title)
        self.back_button = Gtk.Button()
        self.back_button.set_icon_name("go-previous-symbolic")
        self.back_button.controller_label = _("Back")
        self.back_button.controller_exclude_focus = True
        self.back_button.set_focusable(True)
        self.back_button.set_size_request(34, 34)
        self.back_button.set_tooltip_text(_("Back"))
        self.back_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Back")])
        self.back_button.connect("clicked", lambda *_: self._header_back())
        header.pack_start(self.back_button)
        toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self.stack)
        try:
            self.items = portal.load_config(config_path)
        except Exception as error:
            self.config_error = str(error)
            log.warning("Tools configuration could not be loaded", exc_info=True)
        else:
            self.config_error = ""
        self._build_home()
        self._build_tools()
        self._build_updates()
        self._build_system()
        self._build_troubleshooting()
        GLib.idle_add(self._start_system_prefetch)
        self._build_announcements()
        # Fetch Community artwork in the background as the window starts so
        # opening that page never initiates network work.
        GLib.idle_add(self._ensure_community_favicons)
        self._start_announcements_load()
        self._build_settings()
        self.show_page(N_("Home"))
        self.legend = ControllerLegend()
        self.legend.set_margin_start(18)
        self.legend.set_margin_end(18)
        self.legend.set_margin_top(4)
        self.legend.set_margin_bottom(4)
        set_prompt_visible(self.legend, False)
        toolbar.add_bottom_bar(self.legend)
        self._install_input_mode_controllers(self)
        self.connect("close-request", self._close)
        from bazzite_portal.services.agent import Client
        self.client = Client(self._snapshot_changed, self.toast)
        self.connect("map", lambda *_: self.client.set_visible(True) if not self.closing else None)
        self.connect("unmap", lambda *_: self.client.set_visible(False) if not self.closing else None)
        try:
            backend = SDLGamepad()
            self.controller = ControllerDriver(backend, self._controller_context, self._dispatch,
                                               on_connections_changed=self._controller_connections_changed,
                                               on_activity=self._controller_activity, active=False,
                                               has_focus=self._controller_has_focus,
                                               release_delay_ms=100)
            self._schedule_controller_focus_sync()
        except Exception as error:
            self.toast(public_error(error, N_("Controller input is unavailable. Reconnect your controller and try again."), context="Controller initialization"))
        self._render_status()
        self._sync_clear_cache_ui()
        self._refresh_all_prompts()
        set_prompt_visible(self.legend, self._prompts_visible())
        if self.store.errors:
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))

    def toast(self, message):
        if not self.closing:
            self.notices.add_toast(Adw.Toast(title=format_message(message)[:500]))

    def _install_input_mode_controllers(self, window):
        for signal in ("notify::is-active", "map", "unmap"):
            window.connect(signal, lambda *_: self._schedule_controller_focus_sync())
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _controller, x, y, root=window: self._pointer_moved(root, x, y))
        motion.connect("leave", lambda _controller, root=window: self._pointer_left(root))
        window.add_controller(motion)
        window.pointer_motion_controller = motion
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_args: self._native_input_mode())
        window.add_controller(click)
        window.pointer_click_controller = click
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._native_scrolled)
        window.add_controller(scroll)
        window.pointer_scroll_controller = scroll
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._native_key_pressed)
        window.add_controller(keys)
        window.native_keyboard_controller = keys

    def _native_input_mode(self):
        if not self.controller_priority.request_native_takeover():
            self._schedule_native_focus_clear()
            return
        if (not self.controller_navigation and not self._controller_focus_established
                and self.controller_focus_widget is None):
            return  # Native motion/keys must not repeatedly walk every hidden page.
        self.controller_navigation = False
        self._sync_controller_style_mode()
        self._controller_focus_established = False
        self._clear_controller_focus_visuals()

    def _native_scrolled(self, _controller, _dx, _dy):
        self._stop_tool_category_scroll()
        self._native_input_mode()
        return False  # Leave wheel/touchpad scrolling to the scrolled window.

    def _native_key_pressed(self, _controller, keyval, _keycode, state):
        self._native_input_mode()
        if keyval != Gdk.KEY_Escape or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if self.modal:
            self.modal.close()
        elif self.stack.get_visible_child_name() == N_("Tools") and self.tools_search_active:
            self.close_tools_search()
        elif self.stack.get_visible_child_name() != N_("Home"):
            self.show_page(N_("Home"))
        else:
            return False
        return True

    def _header_back(self):
        if self.stack.get_visible_child_name() == N_("Tools") and self.tools_search_active:
            self.close_tools_search()
        else:
            self.show_page(N_("Home"))

    def _clear_controller_focus_visuals(self):
        self.controller_focus_widget = None
        self._controller_focus_established = False
        for root in (self, self.modal):
            if root is None:
                continue
            pending = [root]
            while pending:
                widget = pending.pop()
                if widget.has_css_class("bazzite-controller-focus"):
                    widget.remove_css_class("bazzite-controller-focus")
                widget.remove_css_class("bazzite-controller-immediate")
                if hasattr(widget, "favorite_icon"):
                    self._set_favorite_emphasis(widget, False)
                if hasattr(widget, "controller_selection_prompt"):
                    set_prompt_visible(widget.controller_selection_prompt, False)
                child = widget.get_first_child()
                while child:
                    pending.append(child)
                    child = child.get_next_sibling()
        if hasattr(self, "legend"):
            set_prompt_visible(self.legend, self._prompts_visible())

    def _pointer_moved(self, root, x, y):
        previous = self._last_pointer_position.get(root)
        self._last_pointer_position[root] = (x, y)
        if previous is not None and (x, y) == previous:
            return
        picked = root.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and not hasattr(picked, "favorite_icon"):
            picked = picked.get_parent()
        self._native_input_mode()
        self._set_pointer_hover(picked)

    def _set_pointer_hover(self, row):
        previous = getattr(self, "_pointer_hovered_row", None)
        if previous is row:
            return
        if previous is not None:
            previous.pointer_hovered = False
            self._set_favorite_emphasis(previous, False)
        self._pointer_hovered_row = row
        if row is not None:
            row.pointer_hovered = True
            self._set_favorite_emphasis(row, False)

    def _pointer_left(self, root):
        self._last_pointer_position.pop(root, None)
        if root is self or root is self.modal:
            self._set_pointer_hover(None)

    def _controller_connections_changed(self, connected):
        self.controller_connected = connected
        self._refresh_all_prompts()
        if not connected:
            self.controller_priority.reset()
            self.controller_navigation = False
            self._sync_controller_style_mode()
            self._controller_focus_established = False
            if self._native_focus_timeout is not None:
                GLib.source_remove(self._native_focus_timeout)
                self._native_focus_timeout = None
            self._clear_controller_focus_visuals()
        self._update_controller_legend()
        for hint in (getattr(self, "options_hint", None), getattr(self, "search_hint", None),
                     getattr(self, "home_updates_hint", None), getattr(self, "update_a_hint", None),
                     getattr(self, "changelog_x_hint", None)):
            if hint is not None:
                set_prompt_visible(hint, self._prompts_visible())
        if hasattr(self, "legend"):
            set_prompt_visible(self.legend, self._prompts_visible())

    def _prompts_visible(self):
        return self.controller_connected or bool(self.store.preferences().get("always_show_controller_prompts", False))

    def _prompt_style(self):
        return self.store.preferences().get("controller_prompt_style", "auto")

    def _refresh_all_prompts(self, labels=None):
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        self._refresh_update_prompts(labels)
        self._refresh_context_prompts(labels)
        prompts = getattr(self, "confirmation_prompts", ())
        prompt_labels = getattr(self, "confirmation_prompt_labels", (N_("Yes"), N_("No")))
        for index, (command, accessible) in enumerate(zip(("confirm", "back"), prompt_labels)):
            if index >= len(prompts):
                break
            old = prompts[index]
            prompt = make_button_prompt(command, labels, accessible, self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            if parent is not None:
                parent.insert_child_after(prompt, old.get_prev_sibling())
                parent.remove(old)
            prompts[index] = prompt
        if hasattr(self, "legend"):
            self._update_controller_legend(labels)
        self._sync_maintenance_dialog()
        self._refresh_modal_selection_prompts(labels)
        self._refresh_fixed_modal_prompts(labels)

    def _refresh_modal_selection_prompts(self, labels=None):
        if self.modal is None:
            return
        labels = labels or getattr(getattr(self.controller, "backend", None), "button_labels", {})
        prompt_key = (self._prompt_style(), tuple(sorted(labels.items())))
        for target in controller_focus_targets(self.modal):
            overlay = getattr(target, "controller_prompt_overlay", None)
            if overlay is None:
                continue
            if getattr(target, "controller_prompt_key", None) != prompt_key:
                overlay.remove_overlay(target.controller_selection_prompt)
                prompt = make_button_prompt("confirm", labels, N_("Select"), self._prompt_style())
                prompt.set_halign(Gtk.Align.END)
                prompt.set_valign(Gtk.Align.CENTER)
                prompt.set_margin_end(8)
                overlay.add_overlay(prompt)
                overlay.set_measure_overlay(prompt, False)
                target.controller_selection_prompt = prompt
                target.controller_prompt_key = prompt_key
            prompt = target.controller_selection_prompt
            set_prompt_visible(prompt, self.controller_navigation and self._prompts_visible()
                               and target is self.controller_focus_widget)

    def _refresh_update_prompts(self, labels=None):
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        for command, attribute, accessible in (("confirm", "update_a_hint", self.update_button.get_label()),
                                                ("focus_search", "changelog_x_hint", N_("Show changelog"))):
            old = getattr(self, attribute, None)
            if old is None:
                continue
            prompt = make_button_prompt(command, labels, accessible, self._prompt_style())
            prompt.set_size_request(24, 24)
            set_prompt_visible(prompt, self._prompts_visible())
            target = self.update_button if command == "confirm" else self.release_notes_button
            target.set_prompt(prompt)
            setattr(self, attribute, prompt)

    def _refresh_context_prompts(self, labels=None):
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        for command, attribute, accessible in (("context_action", "options_hint", N_("Open Options")),
                                                ("focus_search", "home_updates_hint", N_("Open Updates")),
                                                ("focus_search", "system_refresh_hint", N_("Refresh system information")),
                                                ("focus_search", "management_refresh_hint", N_("Refresh system information")),
                                                ("focus_search", "search_hint", N_("Search for a tool"))):
            old = getattr(self, attribute, None)
            if old is None:
                continue
            prompt = make_context_prompt(command, labels, accessible, self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            if parent is not None:
                parent.insert_child_after(prompt, old.get_prev_sibling())
                parent.remove(old)
            setattr(self, attribute, prompt)

    def _controller_activity(self):
        self.controller_priority.mark_activity()
        self.controller_navigation = True
        self._sync_controller_style_mode()
        if self.controller_connected and hasattr(self, "legend"):
            set_prompt_visible(self.legend, True)

    def _schedule_native_focus_clear(self):
        if self._native_focus_timeout is not None:
            return
        delay_ms = max(1, int(self.controller_priority.remaining() * 1000 + 0.999))
        self._native_focus_timeout = GLib.timeout_add(delay_ms, self._deferred_native_focus_clear)

    def _deferred_native_focus_clear(self):
        self._native_focus_timeout = None
        if self.closing:
            return GLib.SOURCE_REMOVE
        if self.controller_priority.native_takeover_due():
            self.controller_navigation = False
            self._sync_controller_style_mode()
            self._clear_controller_focus_visuals()
            return GLib.SOURCE_REMOVE
        if self.controller_priority.native_takeover_pending:
            self._schedule_native_focus_clear()
        return GLib.SOURCE_REMOVE

    def work(self, function, callback, *, executor=None):
        future = (executor or self.executor).submit(function)
        def finished(result):
            if result.cancelled():
                return
            try:
                value, error = result.result(), None
            except Exception as exception:
                log.error("Background task failed", exc_info=True)
                value, error = None, str(exception)
            def deliver():
                if not self.closing:
                    if error:
                        self.toast(public_error(error, N_("The request failed. Open Logs for details."), context="Background task"))
                    else:
                        callback(value)
                return False
            GLib.idle_add(deliver)
        future.add_done_callback(finished)

    def show_page(self, name):
        if name not in PAGES:
            return
        if name == "Tools" and self.config_error:
            return
        if name == "Announcements" and not self.announcements_available:
            return
        previous_page = self.stack.get_visible_child_name()
        if name in {N_("Home"), N_("Tools")}:
            self._refresh_pins()
        if (previous_page == N_("Updates") and name != N_("Updates")
                and self.snapshot.run and not self.snapshot.running
                and self.snapshot.result in {"success", "failed"}):
            self._dismissed_result_run = self.snapshot.run
            self._visible_result_run = None
            self._render_status()
        if previous_page == N_("Home") and name != N_("Home"):
            opener = controller_focused_target(self)
            self._home_return_target = (opener if opener in self.pin_rows else
                                        self.home_category_rows.get(name))
        if name != N_("Updates") and hasattr(self, "update_console_revealer"):
            self._set_update_console(False)
        elif name == N_("Updates") and previous_page != N_("Updates") and hasattr(self, "update_console_revealer"):
            self._update_console_open = bool(self.store.preferences()["show_logs"])
        if name == N_("Updates") and hasattr(self, "updates_content_box"):
            self.updates_content_box.set_valign(
                Gtk.Align.START if self._update_console_open else Gtk.Align.CENTER
            )
        if name == N_("Tools") and self.stack.get_visible_child_name() != N_("Tools"):
            # A visit from another main page starts at the top of the catalog.
            # Do this before revealing Tools so Gtk.Stack cannot animate a
            # stale adjustment into view. Modal returns retain their own context.
            self.tools_search_active = False
            self.search.set_text("")
            self.tools_view.set_visible_child_name("catalog")
            adjustment = self.catalog_scroll.get_vadjustment()
            adjustment.set_value(adjustment.get_lower())
        self.stack.set_visible_child_name(name)
        self.home_options_box.set_visible(name == N_("Home"))
        for prefix in ("system", "management"):
            controls = getattr(self, prefix + "_refresh_header", None)
            if controls is not None:
                controls.set_visible(name == (N_("System") if prefix == "system" else "Management"))
        if hasattr(self, "tools_titlebar_search"):
            self.tools_titlebar_search.set_visible(name == N_("Tools"))
        self._render_home_activity()
        self._acknowledge_update_result()
        self._sync_header_back()
        page_title = PAGE_LABELS.get(name, "")
        self.header_title.set_title(format_message(page_title))
        if name in {N_("System"), "Management"}:
            self.header_title.set_title(format_message(_("System information") if name == N_("System") else _("System Management")))
            if not self.system_has_snapshot:
                # Reveal the page before scheduling even the loading-state changes.
                def load_first_visit():
                    if (not self.closing and not self.system_has_snapshot and
                            self.stack.get_visible_child_name() in {N_("System"), "Management"}):
                        self._refresh_system()
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(load_first_visit)
        if hasattr(self, "update_console_revealer"):
            self.update_console_revealer.set_reveal_child(name == N_("Updates") and self._update_console_open)
        self._update_controller_legend()
        def focus_page_start():
            if self.closing or self.modal is not None or self.stack.get_visible_child_name() != name:
                return GLib.SOURCE_REMOVE
            if name == N_("Updates") and self.controller_navigation:
                self._clear_controller_focus_visuals()
                return GLib.SOURCE_REMOVE
            page = self.stack.get_visible_child()
            targets = controller_focus_targets(page) if page else []
            if targets:
                target = getattr(self, "_home_return_target", None) if name == N_("Home") else None
                target = target if target in targets else targets[0]
                if self.controller_navigation:
                    self._set_controller_focus(target)
                    self._reveal_focus(self)
                else:
                    target.grab_focus()
            elif self.back_button.get_sensitive():
                self.back_button.grab_focus()
            self._update_controller_legend()
            return GLib.SOURCE_REMOVE
        if name == N_("Home") and self.controller_navigation:
            focus_page_start()
        else:
            GLib.idle_add(focus_page_start)

    def _build_home(self):
        page, box = content_page()
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.home_options_box = heading
        self.options_button = button(N_("Options"), lambda: self.show_page("Settings"), "preferences-system-symbolic")
        self.options_button.controller_exclude_focus = True
        self.options_button.set_focusable(True)
        self.options_button.set_tooltip_text(_("Options") + " · Y")
        self.options_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Options") + " · Y"])
        self.options_hint = make_context_prompt("context_action", accessible_name=N_("Open Options"))
        set_prompt_visible(self.options_hint, self._prompts_visible())
        heading.append(self.options_hint)
        heading.append(self.options_button)
        self.header.pack_end(heading)
        updates = group("")
        self.home_status = Adw.ActionRow(title=_('Update {bazzite} and Apps').format(bazzite='Bazzite'), subtitle=_("Update your system and installed apps."))
        self.home_status.set_use_markup(False)
        self.home_status.set_subtitle_lines(2)
        self.home_status.controller_exclude_focus = True
        self.home_status.set_activatable(True)
        self.home_activity_icon = Gtk.Image(pixel_size=32)
        from bazzite_portal.artwork.home_icon import HomeUpdateIcon
        self.home_update_artwork = HomeUpdateIcon(self.home_activity_icon, self.executor)
        self.connect("map", lambda *_: (self._render_home_activity(), self._acknowledge_update_result()))
        self.connect("notify::is-active", lambda *_: self._acknowledge_update_result())
        self.connect("notify::is-active", lambda *_: self._refresh_dirty_tools())
        self.connect("unmap", lambda *_: self._render_home_activity())
        self.stack.connect("notify::visible-child-name", lambda *_: self._render_home_activity())
        self.home_activity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.home_activity.append(self.home_activity_icon)
        self.home_status.add_prefix(self.home_activity)
        self.home_updates_hint = make_context_prompt("focus_search", accessible_name=N_("Open Updates"))
        set_prompt_visible(self.home_updates_hint, self._prompts_visible())
        self.home_status.add_suffix(self.home_updates_hint)
        self.home_status.connect("activated", lambda *_: self.show_page(N_("Updates")))
        updates.add(self.home_status)
        box.append(updates)
        self.home_category_rows = {}
        shortcuts = group(N_("Explore"))
        self.announcements_available = False
        for title, destination, description in (
            (N_("Tools"), N_("Tools"), N_("Browse setup, fixes, and utilities from the system Portal.")),
            (N_("System information"), N_("System"), C_('View deployments, package layers and {rpm_ostree} status.', rpm_ostree='RPM-OSTree')),
            (N_("System Management"), "Management", N_("Cancel a pending deployment, remove layered apps or recover your system.")),
            (N_("Troubleshooting"), N_("Troubleshooting"), C_('Report a problem, collect system logs, or reset {steam}.', steam='Steam')),
            (N_("Community"), N_("Community"), C_('{bazzite} documentation, website, and community links.', bazzite='Bazzite')),
            (N_("Announcements"), N_("Announcements"), C_('View {bazzite} release and project announcements.', bazzite='Bazzite')),
        ):
            row = Adw.ActionRow(title=format_message(title), subtitle=format_message(description))
            row.controller_focus_row = True
            row.set_activatable(True)
            missing_or_invalid = destination == "Tools" and bool(self.config_error)
            if destination == "Announcements":
                missing_or_invalid = not announcements.source_path(self.config_path).is_file()
            if missing_or_invalid:
                row.set_sensitive(False)
                row.set_activatable(False)
                row.set_subtitle(_("The configuration file for this category is missing or invalid."))
            elif destination == "Announcements":
                row.set_sensitive(False)
                row.set_activatable(False)
                row.set_subtitle(_("Loading…"))
            row.connect("activated", lambda *_args, selected=destination: self.show_page(selected))
            shortcuts.add(row)
            self.home_category_rows[destination] = row
        box.append(shortcuts)
        self.pin_group = group(N_("Pinned tools"))
        box.append(self.pin_group)
        self.pin_rows = []
        self._render_pins()
        self.stack.add_named(page, N_("Home"))
        self._render_home_activity()

    def _build_troubleshooting(self):
        page, box = content_page(N_("Troubleshooting"), C_('Get help, collect diagnostic logs, or repair the {steam} client.', steam='Steam'))
        box.remove(box.get_first_child())
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        heading.append(label(N_("Troubleshooting"), "title-1"))
        heading.append(Gtk.Box(hexpand=True))
        box.prepend(heading)
        group_box = group("")
        self.report_problem_button = Adw.ActionRow(title=_("Report a problem"), subtitle=_('Collect relevant logs and open a {bazzite} {github} issue.').format(bazzite='Bazzite', github='GitHub'))
        self.report_problem_button.set_activatable(True)
        self.report_problem_button.connect("activated", lambda *_: self._begin_report_flow())
        self.report_problem_button.add_css_class("destructive-action")
        self.report_problem_button.controller_focus_row = True
        group_box.add(self.report_problem_button)
        box.append(group_box)
        group_box = group("")
        self.collect_system_logs_button = Adw.ActionRow(title=_("Collect system logs"), subtitle=_("Save logs and hardware details for troubleshooting on your Desktop."))
        self.collect_system_logs_button.set_activatable(True)
        self.collect_system_logs_button.connect("activated", lambda *_: self._begin_system_logs_collection())
        self.collect_system_logs_button.controller_focus_row = True
        group_box.add(self.collect_system_logs_button)
        box.append(group_box)
        group_box = group("")
        self.reset_steam_button = Adw.ActionRow(title=_('Reset {steam}').format(steam='Steam'), subtitle=_('Reset the {steam} client without removing your games.').format(steam='Steam'))
        self.reset_steam_button.set_activatable(True)
        self.reset_steam_button.connect("activated", lambda *_: self._begin_steam_reset())
        self.reset_steam_button.controller_focus_row = True
        group_box.add(self.reset_steam_button)
        box.append(group_box)
        self.stack.add_named(page, N_("Troubleshooting"))
        self._report_collecting = False
        self._report_generation = 0
        self._troubleshooting_busy = False

    def _begin_report_flow(self):
        if self.modal is not None or self._troubleshooting_busy:
            return
        self._report_restore_target = controller_focused_target(self)
        self._report_restore_with_controller = self.controller_navigation
        self._report_account_dialog()

    def _set_troubleshooting_busy(self, busy):
        self._troubleshooting_busy = bool(busy)
        for action in (self.report_problem_button, self.collect_system_logs_button, self.reset_steam_button):
            action.set_sensitive(not self._troubleshooting_busy)

    def _report_account_dialog(self):
        dialog = self._report_modal("GitHub account", 510)
        box = self._report_modal_box(dialog)
        box.append(label("Are you signed in to GitHub in your browser?", "title-3"))
        box.append(label("You need a GitHub account to report a problem."))
        no_button = button(N_("No"), lambda: self._close_report_modal(dialog, self._show_account_required))
        yes_button = button(N_("Yes"), lambda: self._close_report_modal(dialog, self._start_report_collection))
        dialog.report_yes_button = yes_button
        dialog.report_no_button = no_button
        anchor = self._fixed_modal_actions(dialog, box, (("back", no_button), ("confirm", yes_button)))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _show_account_required(self):
        if self.closing or self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        dialog = self._report_modal("GitHub account required", 470)
        box = self._report_modal_box(dialog)
        box.append(label("Sign in to GitHub in your browser. If you don’t have an account, create one first."))
        close_button = button(N_("Close"), dialog.close)
        anchor = self._fixed_modal_actions(dialog, box, (("confirm", close_button),))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _start_report_collection(self):
        if self.closing:
            return
        if self._troubleshooting_busy or self.stack.get_visible_child_name() != N_("Troubleshooting"):
            if self.modal is None:
                GLib.idle_add(self._restore_report_focus)
            return
        self._report_collecting = True
        self._set_troubleshooting_busy(True)
        self._report_generation += 1
        generation = self._report_generation
        self._report_collecting_dialog = None
        self._show_report_collecting(generation)
        desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        desktop = Path(desktop) if desktop else Path.home() / "Desktop"
        self._report_collecting_dialog = self.modal

        def collect_and_prepare_issue():
            report = diagnostics.collect_report(desktop)
            try:
                issue_url, issue_error = diagnostics.build_issue_url(report), None
            except Exception as exception:
                issue_url, issue_error = "", str(exception)
            return report, issue_url, issue_error

        future = self.executor.submit(collect_and_prepare_issue)

        def finished(result):
            try:
                (report, issue_url, issue_error), error = result.result(), None
            except Exception as exception:
                report, issue_url, issue_error, error = None, "", None, str(exception)
            def deliver():
                if self.closing or generation != self._report_generation:
                    return GLib.SOURCE_REMOVE
                self._report_collection_finished(generation, report, issue_url, issue_error, error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)

        future.add_done_callback(finished)

    def _begin_system_logs_collection(self):
        if self.modal is not None or self._troubleshooting_busy:
            return
        if self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        self._report_restore_target = controller_focused_target(self)
        self._report_restore_with_controller = self.controller_navigation
        self._troubleshooting_busy = True
        self._report_collecting = True
        self._report_generation += 1
        generation = self._report_generation
        self._set_troubleshooting_busy(True)
        self._report_collecting_dialog = None
        self._show_report_collecting(generation, N_("Collecting system logs"))
        desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        desktop = Path(desktop) if desktop else Path.home() / "Desktop"
        self._report_collecting_dialog = self.modal
        future = self.executor.submit(diagnostics.collect_system_logs, desktop)

        def finished(result):
            try:
                report, error = result.result(), None
            except Exception as exception:
                report, error = None, str(exception)
            def deliver():
                if self.closing or generation != self._report_generation:
                    return GLib.SOURCE_REMOVE
                self._system_logs_collection_finished(generation, report, error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)

        future.add_done_callback(finished)

    def _system_logs_collection_finished(self, generation, report, error):
        if self.closing or generation != self._report_generation:
            return
        self._report_collecting = False
        collecting_dialog = getattr(self, "_report_collecting_dialog", None)
        cancelled = bool(getattr(collecting_dialog, "report_cancelled", False))
        superseded = self.modal is not None and self.modal is not collecting_dialog
        if collecting_dialog is not None and self.modal is collecting_dialog:
            collecting_dialog.close()
        self._set_troubleshooting_busy(False)
        if error:
            self.toast(public_error(error, N_('Could not collect system logs. Open Logs for details.'), context='Collect system logs'))
            GLib.idle_add(self._restore_report_focus)
        elif cancelled or superseded:
            self.toast(_('System logs collected and saved to {directory}.').format(directory=report.directory))
            GLib.idle_add(self._restore_report_focus)
        else:
            self._show_system_logs_ready(report)

    def _show_system_logs_ready(self, report):
        dialog = self._report_modal(N_("System logs ready"), 560, 440)
        box = self._report_modal_box(dialog)
        review = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        review.append(label(N_("System logs are ready."), "title-3"))
        review.append(label(_("Location: {path}").format(path=report.directory)))
        files = "\n".join(f"• {name}" for name in report.files) or _("No files were created.")
        review.append(label(N_("Files collected"), "heading"))
        files_label = label(files, translate=False)
        files_label.set_selectable(True)
        files_label.controller_exclude_focus = True
        review.append(files_label)
        if report.warnings:
            review.append(label(N_("Warnings"), "heading"))
            review.append(label("\n".join(dict.fromkeys(public_error(warning, N_("Some diagnostic information could not be collected. See the collected logs for details."), context="Report collection") for warning in report.warnings)), translate=False))
        scroll = Gtk.ScrolledWindow(min_content_height=120, vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(review)
        dialog.report_review_scroll = scroll
        box.append(scroll)
        close_button = button(N_("Close"), dialog.close)
        folder_button = button(N_("Open logs folder"), lambda: self._open_report_folder(report.directory))
        anchor = self._fixed_modal_actions(dialog, box, (("back", close_button), ("confirm", folder_button)))
        dialog.open_folder_button = folder_button
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _begin_steam_reset(self):
        if self.modal is not None or self._troubleshooting_busy:
            return
        if self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        self._report_restore_target = controller_focused_target(self)
        self._report_restore_with_controller = self.controller_navigation
        dialog = self._report_modal(C_('Reset {steam} Installation?', steam='Steam'), 560)
        box = self._report_modal_box(dialog)
        box.append(label(C_('{steam} will stop and sign you out. This only cleans the steam installation folder.', steam='Steam')))
        box.append(label(_('Your games, Saves, Configuration, Music and {proton} files will stay untouched.').format(proton='Proton')))
        cancel_button = button(N_("Cancel"), dialog.close)
        reset_button = button(C_('Reset {steam}', steam='Steam'), lambda: self._close_report_modal(dialog, self._start_steam_reset))
        reset_button.add_css_class("destructive-action")
        dialog.confirm_yes_button = reset_button
        dialog.confirm_cancel_button = cancel_button
        anchor = self._fixed_modal_actions(dialog, box, (("back", cancel_button), ("confirm", reset_button)))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _start_steam_reset(self):
        if self.closing or self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        self._set_troubleshooting_busy(True)
        self._report_collecting = True
        self._report_generation += 1
        generation = self._report_generation
        self._report_collecting_dialog = None
        self._show_report_collecting(generation, C_('Resetting {steam}', steam='Steam'))
        self._report_collecting_dialog = self.modal
        future = self.executor.submit(steam_reset.reset_steam)

        def finished(result):
            try:
                outcome, error = result.result(), None
            except Exception as exception:
                outcome, error = None, exception
            def deliver():
                if self.closing or generation != self._report_generation:
                    return GLib.SOURCE_REMOVE
                self._steam_reset_finished(generation, outcome, error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)

        future.add_done_callback(finished)

    def _steam_reset_finished(self, generation, outcome, error):
        if self.closing or generation != self._report_generation:
            return
        self._report_collecting = False
        dialog = getattr(self, "_report_collecting_dialog", None)
        cancelled = bool(getattr(dialog, "report_cancelled", False))
        superseded = self.modal is not None and self.modal is not dialog
        if dialog is not None and self.modal is dialog:
            dialog.close()
        self._set_troubleshooting_busy(False)
        if error:
            message = public_error(error, C_('{steam} reset did not complete. Open Logs for details.', steam='Steam'), context="Steam reset")
            removed = getattr(error, "removed", ())
            if removed:
                log.warning("Steam reset partially completed; removed: %s", ", ".join(removed))
                message += " " + _('Some {steam} data has already been removed.').format(steam='Steam')
            self.toast(message)
        elif cancelled or superseded:
            self.toast(C_('{steam} reset finished after its progress window was closed.', steam='Steam'))
        else:
            self._show_steam_reset_result(outcome)
        GLib.idle_add(self._restore_report_focus)

    def _show_steam_reset_result(self, outcome):
        dialog = self._report_modal(C_('{steam} reset complete', steam='Steam'), 520)
        box = self._report_modal_box(dialog)
        box.append(label(C_('{steam} reset is complete.', steam='Steam'), "title-3"))
        box.append(label(C_('You can now open {steam}. Its client files need to download again, so the first launch may take a while.', steam='Steam')))
        close_button = button(N_("Close"), dialog.close)
        close_button.add_css_class("suggested-action")
        box.append(close_button)
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, close_button)

    def _show_report_collecting(self, generation, title=N_("Collecting logs")):
        if self.closing or generation != self._report_generation:
            return
        if self.modal is not None:
            self.modal.close()
        dialog = self._report_modal(title, 430)
        box = self._report_modal_box(dialog)
        progress = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        spinner = Adw.Spinner()
        spinner.set_focusable(False)
        spinner.controller_exclude_focus = True
        progress.append(spinner)
        progress.append(label(N_("Working on this device…")))
        box.append(progress)
        dialog.report_generation = generation
        dialog.report_transient = True
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        close_button = button(N_("Close"), dialog.close)
        anchor = self._fixed_modal_actions(dialog, box, (("back", close_button),))
        self._present_report_modal(dialog, anchor)

    def _report_collection_finished(self, generation, report, issue_url, issue_error, error):
        if self.closing or generation != self._report_generation:
            return
        self._report_collecting = False
        self._set_troubleshooting_busy(False)
        collecting_dialog = getattr(self, "_report_collecting_dialog", None)
        cancelled = bool(getattr(collecting_dialog, "report_cancelled", False))
        superseded = self.modal is not None and self.modal is not collecting_dialog
        if collecting_dialog is not None and self.modal is collecting_dialog:
            collecting_dialog.close()
        if error:
            self.toast(public_error(error, N_('Could not collect diagnostic logs. Open Logs for details.'), context='Collect diagnostic logs'))
            GLib.idle_add(self._restore_report_focus)
            return
        if cancelled or superseded:
            message = _('Logs collected and saved to {directory}.').format(directory=report.directory)
            if issue_error:
                message += " The GitHub issue form could not be prepared."
            self.toast(message)
            GLib.idle_add(self._restore_report_focus)
            return
        self._show_report_ready(report, issue_url, issue_error)

    def _show_report_ready(self, report, issue_url, issue_error=None):
        dialog = self._report_modal(N_("Logs ready"), 800, 720)
        box = self._report_modal_box(dialog)
        review = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        review.append(label(N_("Logs are ready."), "title-3"))
        review.append(label(_("Location: {path}").format(path=report.directory)))
        hardware = label(str(report.hardware), "monospace", selectable=True, translate=False)
        hardware.set_yalign(0)
        hardware.controller_exclude_focus = True
        hardware.set_max_width_chars(100)
        dialog.report_hardware_selection = hardware
        review.append(label(N_("Hardware summary"), "heading"))
        review.append(hardware)
        files = "\n".join(f"• {name}" for name in report.files) or _("No files were created.")
        review.append(label(N_("Files collected"), "heading"))
        files_label = label(files, translate=False)
        files_label.set_selectable(True)
        files_label.set_wrap(True)
        files_label.controller_exclude_focus = True
        review.append(files_label)
        if report.warnings:
            review.append(label(N_("Warnings"), "heading"))
            review.append(label("\n".join(dict.fromkeys(public_error(warning, N_("Some diagnostic information could not be collected. See the collected logs for details."), context="Report collection") for warning in report.warnings)), translate=False))
        review.append(label(N_("Attach only the logs relevant to your problem. Then fill in the remaining fields and describe the issue.")))
        review_scroll = Gtk.ScrolledWindow(min_content_height=300, vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        review_scroll.set_child(review)
        dialog.report_review_scroll = review_scroll
        box.append(review_scroll)
        close_button = button(N_("Close"), dialog.close)
        folder_button = button(N_("Open logs folder"), lambda: self._open_report_folder(report.directory))
        issue_button = button("Open GitHub issue", lambda: self._open_report_issue(issue_url))
        issue_button.add_css_class("suggested-action")
        issue_button.set_sensitive(bool(issue_url))
        if issue_error:
            issue_button.set_tooltip_text(public_error(issue_error, N_("The issue form could not be prepared. Open Logs for details."), context='Prepare issue form'))
        dialog.open_folder_button = folder_button
        dialog.open_issue_button = issue_button
        dialog.hardware_summary = hardware
        dialog.files_label = files_label
        dialog.issue_url = issue_url
        anchor = self._fixed_modal_actions(dialog, box, (
            ("back", close_button), ("context_action", folder_button), ("confirm", issue_button)))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _fixed_modal_actions(self, dialog, box, actions):
        """Keep pad actions fixed while retaining ordinary pointer and Tab access."""
        anchor = Gtk.Box(focusable=True)
        anchor.controller_exclude_focus = True
        anchor.set_size_request(0, 0)
        anchor.set_opacity(0)
        box.append(anchor)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)
        dialog.fixed_controller_actions = dict(actions)
        dialog.fixed_controller_prompts = []
        labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
        for command, control in actions:
            choice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            prompt = make_button_prompt(command, labels, control.get_label(), self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            control.controller_exclude_focus = True
            control.add_css_class("confirmation-choice")
            choice.append(prompt)
            choice.append(control)
            row.append(choice)
            dialog.fixed_controller_prompts.append((command, control, prompt))
        box.append(row)
        return anchor

    def _refresh_fixed_modal_prompts(self, labels):
        if self.modal is None:
            return
        about_legend = getattr(self.modal, "about_legend", None)
        if about_legend is not None:
            about_legend.set_controller_content(_("D-pad Move"), [("confirm", _("Select"), labels), ("back", _("Back"), labels)], self._prompt_style())
            set_prompt_visible(about_legend, self._prompts_visible())
        picker_legend = getattr(self.modal, "picker_legend", None)
        if picker_legend is not None:
            picker_legend.set_controller_content(_("D-pad Move"), [], self._prompt_style())
            set_prompt_visible(picker_legend, self._prompts_visible())
            wizard = self.modal.image_wizard
            old = wizard.back_prompt
            prompt = make_button_prompt("back", labels, N_("Back"), self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            parent.insert_child_after(prompt, old.get_prev_sibling())
            parent.remove(old)
            wizard.back_prompt = prompt
        if getattr(self.modal, "layer_selection", False):
            legend = getattr(self.modal, "layer_legend", None)
            if legend is not None:
                set_prompt_visible(legend, self._prompts_visible())
            for index, (command, title, old) in enumerate(getattr(self.modal, "layer_legend_prompts", ())):
                prompt = make_button_prompt(command, labels, title + " · controller button", self._prompt_style())
                set_prompt_visible(prompt, self._prompts_visible())
                parent = old.get_parent()
                if parent is not None:
                    parent.insert_child_after(prompt, old.get_prev_sibling())
                    parent.remove(old)
                self.modal.layer_legend_prompts[index] = (command, title, prompt)
        prompts = getattr(self.modal, "fixed_controller_prompts", ())
        for index, (command, control, old) in enumerate(prompts):
            prompt = make_button_prompt(command, labels, control.get_label(), self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            if parent is not None:
                parent.insert_child_after(prompt, old.get_prev_sibling())
                parent.remove(old)
            prompts[index] = (command, control, prompt)

    def _open_report_folder(self, directory):
        if self.closing:
            return
        folder = Path(directory).resolve()
        self._run_xdg_open(str(folder), _("Could not open logs folder"),
                           self._launch_report_folder_worker)

    def _run_xdg_open(self, target, error_prefix, worker=None):
        if self.closing:
            return
        future = self.executor.submit(worker or self._launch_xdg_open_worker, target)

        def finished(result):
            try:
                result.result()
            except Exception as error:
                message = public_error(error, _("{action}. Open Logs for details.").format(action=error_prefix), context="Open external location")
                def report_error():
                    if not self.closing:
                        self.toast(message)
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(report_error)

        future.add_done_callback(finished)

    @staticmethod
    def _launch_xdg_open_worker(target):
        process = subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            status = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Some desktop handlers stay running while their window is open.
            return
        if status:
            raise RuntimeError(f"xdg-open exited with status {status}")

    @staticmethod
    def _launch_report_folder_worker(directory):
        return Window._launch_xdg_open_worker(directory)

    def _open_report_issue(self, issue_url):
        if self.closing:
            return
        if not issue_url:
            self.toast(C_('The {github} issue form could not be prepared.', github='GitHub'))
            return
        self._launch_report_uri(issue_url)

    def _launch_report_uri(self, uri):
        if self.closing:
            return
        self._run_xdg_open(uri, _("Could not open link"))

    def _report_modal(self, title, width, height=-1):
        dialog = Adw.Window(transient_for=self, modal=True, title=format_message(title), default_width=width,
                            default_height=height)
        dialog.set_application(self.get_application())
        self.modal = dialog
        self._install_input_mode_controllers(dialog)
        return dialog

    @staticmethod
    def _report_modal_box(dialog, top_aligned=False):
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for name in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + name)(22)
        if top_aligned:
            box.set_valign(Gtk.Align.START)
            clamp = Adw.Clamp(maximum_size=560, tightening_threshold=560)
            clamp.set_valign(Gtk.Align.START)
            clamp.set_child(box)
            toolbar.set_content(clamp)
        else:
            toolbar.set_content(box)
        set_window_content(dialog, toolbar)
        return box

    def _present_report_modal(self, dialog, focus_widget):
        dialog.connect("map", lambda *_: GLib.idle_add(
            lambda: self._focus_report_dialog(dialog, focus_widget)))
        dialog.set_focus(focus_widget)
        focus_widget.grab_focus()
        dialog.present()
        self._update_controller_legend()

    def _focus_report_dialog(self, dialog, focus_widget):
        if dialog is self.modal and dialog.get_mapped():
            if self.controller_navigation and not hasattr(dialog, "fixed_controller_actions"):
                self._set_controller_focus(focus_widget)
            else:
                focus_widget.grab_focus()
            hardware = getattr(dialog, "report_hardware_selection", None)
            if hardware is not None:
                hardware.select_region(0, 0)
        return GLib.SOURCE_REMOVE

    def _close_report_modal(self, dialog, callback=None):
        if callback is not None:
            dialog.report_on_close = callback
        dialog.close()

    def _report_modal_closed(self, dialog):
        if self.modal is dialog:
            parent = getattr(dialog, "modal_parent", None)
            if (not self.closing and parent is not None
                    and not getattr(parent, "maintenance_closed", False)
                    and parent.get_visible()):
                self.modal = parent
                self._sync_maintenance_dialog()
            else:
                self.modal = None
        self._last_pointer_position.pop(dialog, None)
        self._update_controller_legend()
        if (getattr(dialog, "report_transient", False) and self._report_collecting
                and getattr(dialog, "report_generation", None) == self._report_generation):
            dialog.report_cancelled = True
        action = getattr(dialog, "report_on_close", None)
        if action is not None and not self.closing:
            GLib.idle_add(lambda: (action(), GLib.SOURCE_REMOVE)[1])
        elif not getattr(dialog, "report_transient", False) and not self.closing:
            GLib.idle_add(self._restore_report_focus)
        return False

    def _restore_report_focus(self):
        target = getattr(self, "_report_restore_target", None)
        if self.closing or self.modal is not None or target is None:
            return GLib.SOURCE_REMOVE
        if target.get_mapped():
            if getattr(self, "_report_restore_with_controller", False) and self.controller_navigation:
                self._set_controller_focus(target)
            else:
                self._native_input_mode()
                target.grab_focus()
        self._report_restore_target = None
        self._update_controller_legend()
        return GLib.SOURCE_REMOVE

    def _render_home_activity(self):
        if not hasattr(self, "home_status"):
            return
        running = bool(self._active_system_operations() or self._deployment_transaction_active())
        if running:
            self.home_status.add_css_class("bazzite-update-active-row")
        else:
            self.home_status.remove_css_class("bazzite-update-active-row")
        self.home_update_artwork.update(
            self.store.preferences()["notification_icon_theme"], running,
            self.get_mapped() and self.stack.get_visible_child_name() == N_("Home"))
        if running:
            return

    def _render_pins(self):
        self.pin_group.portal_status_generation = getattr(self.pin_group, "portal_status_generation", 0) + 1
        self.pin_group.portal_status_rows = {}
        for row in self.pin_rows:
            self.pin_group.remove(row)
        self.pin_rows.clear()
        pins = self._pins
        self.pin_group.set_visible(bool(pins))
        for pin in pins:
            item = portal.resolve(self.items, pin)
            title = item["action"].get("title", N_("Action")) if item else pin.get("title") or pin.get("id", N_("Unavailable tool"))
            subtitle = item["action"].get("description", "") if item else N_("Unavailable or ambiguous · select to unpin")
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
            row.set_title_lines(2)
            row.set_subtitle_lines(3)
            row.pinned_reference = pin
            row.controller_focus_row = True
            row.set_activatable(True)
            if item:
                row.portal_item = item
                row.connect("activated", lambda *_args, selected=item: self.open_action(selected))
                if item["action"].get("status_script"):
                    self._add_tool_status(row, self.pin_group)
            else:
                row.connect("activated", lambda *_args, selected=pin: self._remove_unavailable_pin(selected))
            self.pin_group.add(row)
            self.pin_rows.append(row)
            if item and item["action"].get("status_script"):
                self._request_portal_status(item, row=row)

    def _remove_unavailable_pin(self, pin):
        self.confirm(
            N_("Unpin unavailable tool?"),
            N_("This pin no longer resolves to one unambiguous Portal action. Removing the pin does not run any script."),
            N_("Unpin"),
            lambda: self.toggle_pin(pin),
        )

    def toggle_pin(self, pin):
        pins = self.store.pins()
        if pin in pins:
            pins.remove(pin)
        elif len(pins) < 100:
            pins.append(pin)
        self._save_pins(pins)

    def _refresh_pins(self):
        pins = self.store.pins()
        if pins != self._pins:
            self._pins = pins
            self._render_pins()
            self._sync_tool_favorites()

    def _save_pins(self, pins):
        if not self.store.write("pins", {"items": pins}):
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))
            return False
        self._pins = pins
        self._render_pins()
        self._sync_tool_favorites()
        self._update_controller_legend()
        return True

    def move_pin(self, index, delta):
        pins = self.store.pins()
        if 0 <= index + delta < len(pins):
            pins[index], pins[index + delta] = pins[index + delta], pins[index]
            self._save_pins(pins)

    def _build_tools(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_vexpand(True)
        page = Adw.Clamp(maximum_size=760, tightening_threshold=640)
        page.set_child(box)
        page.set_vexpand(True)
        self.tools_search_active = False
        self.tools_view = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        search_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_button = button(N_("Search"), self.open_tools_search, "system-search-symbolic")
        self.search_button.controller_exclude_focus = True
        self.search_button.set_focusable(True)
        self.search_button.set_tooltip_text(_("Search tools") + " · X")
        self.search_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Search tools") + " · X"])
        self.search_hint = make_context_prompt("focus_search", accessible_name=N_("Search for a tool"))
        set_prompt_visible(self.search_hint, self._prompts_visible())
        search_header.append(self.search_button)
        search_header.append(self.search_hint)
        self.tools_search_header = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.tools_search_header.set_hhomogeneous(False)
        self.tools_search_header.add_named(search_header, "catalog")
        self.tools_titlebar_search = self.tools_search_header
        self.header.pack_start(self.tools_titlebar_search)

        catalog = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        catalog_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.tools_box.set_margin_top(8)
        self.tools_box.set_margin_bottom(8)
        catalog_scroll.set_child(self.tools_box)
        catalog.append(catalog_scroll)
        self.catalog_scroll = catalog_scroll

        search_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.search = Gtk.SearchEntry(placeholder_text=_(N_("Search tools")), hexpand=True)
        self.search.set_size_request(240, -1)
        self.search.update_property([Gtk.AccessibleProperty.LABEL], [N_("Search tools")])
        self.search.connect("search-changed", lambda *_: self._render_tools() if self.tools_search_active else None)
        self.tools_search_header.add_named(self.search, "search")
        search_results_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.search_results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.search_results_box.set_margin_top(8)
        self.search_results_box.set_margin_bottom(8)
        self.search_results_box.set_vexpand(True)
        search_results_scroll.set_child(self.search_results_box)
        search_page.append(search_results_scroll)
        self.search_results_scroll = search_results_scroll
        self.tools_view.add_named(catalog, "catalog")
        self.tools_view.add_named(search_page, "search")
        self.tools_view.connect("notify::visible-child-name", lambda *_:
            self.tools_search_header.set_visible_child_name(self.tools_view.get_visible_child_name()))
        self.tools_view.set_vexpand(True)
        box.append(self.tools_view)
        self._render_tools()
        self.stack.add_named(page, N_("Tools"))

    def open_tools_search(self):
        if self.stack.get_visible_child_name() != N_("Tools"):
            return
        self.tools_search_active = True
        self.tools_view.set_visible_child_name("search")
        self._sync_header_back()
        self.search.set_text("")
        self._render_tools()
        GLib.idle_add(lambda: self.search.grab_focus() and GLib.SOURCE_REMOVE)

    def _open_search_keyboard(self):
        self.work(lambda: subprocess.Popen(["steam", "-ifrunning", "steam://open/keyboard"]), lambda _: None)

    def close_tools_search(self):
        self.tools_search_active = False
        self.tools_view.set_visible_child_name("catalog")
        self._sync_header_back()
        GLib.idle_add(self._focus_tools_start)

    def _sync_header_back(self):
        if not hasattr(self, "back_button"):
            return
        search_back = self.stack.get_visible_child_name() == N_("Tools") and getattr(self, "tools_search_active", False)
        self.back_button.set_tooltip_text(format_message(N_("Back to Tools") if search_back else N_("Back to Home")))
        self.back_button.update_property([Gtk.AccessibleProperty.LABEL], [N_("Back to Tools") if search_back else N_("Back to Home")])
        self.back_button.set_sensitive(search_back or self.stack.get_visible_child_name() != N_("Home"))

    def _focus_tools_start(self):
        if self.closing or self.stack.get_visible_child_name() != N_("Tools") or self.tools_search_active:
            return GLib.SOURCE_REMOVE
        targets = controller_focus_targets(self.tools_view)
        if targets:
            if self.controller_navigation:
                self._set_controller_focus(targets[0])
            else:
                targets[0].grab_focus()
        return GLib.SOURCE_REMOVE

    def _render_tools(self):
        if not hasattr(self, "tools_box"):
            return
        destination = self.search_results_box if self.tools_search_active else self.tools_box
        destination.portal_status_generation = getattr(destination, "portal_status_generation", 0) + 1
        destination.portal_status_rows = {}
        while child := destination.get_first_child():
            destination.remove(child)
        if self.config_error:
            return
        results = portal.search(self.items, self.search.get_text() if self.tools_search_active else "")
        query = self.search.get_text().strip() if self.tools_search_active else ""
        if self.tools_search_active and not query:
            empty = Adw.StatusPage(title=_("Search for a tool"), icon_name="system-search-symbolic")
            empty.set_vexpand(True)
            destination.append(empty)
            return
        if not results:
            destination.append(Adw.StatusPage(title=_("No matching tools"), description=_("Try another search."), icon_name="system-search-symbolic"))
            return
        groups = {}
        pins = self._pins
        for item in results:
            category = item["category"]
            if category not in groups:
                groups[category] = group(category, translate=False)
                destination.append(groups[category])
            action = item["action"]
            row = Adw.ActionRow(title=GLib.markup_escape_text(action.get("title", N_("Action"))), subtitle=GLib.markup_escape_text(action.get("description", "")))
            row.set_title_lines(2)
            row.set_subtitle_lines(3)
            pin = portal.reference(item)
            row.controller_focus_row = True
            row.set_focusable(True)
            row.portal_item = item
            row.portal_reference = pin
            row.set_activatable(True)
            row.connect("activated", lambda *_args, selected=item: self.open_action(selected))
            if action.get("status_script"):
                self._add_tool_status(row, destination)
            self._add_favorite_hit_area(row, item, pin in pins)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", lambda _keys, keyval, _keycode, _state, selected=item, target=row:
                         self._action_row_key(selected, target, keyval))
            row.add_controller(keys)
            row.favorite_key_controller = keys
            groups[category].add(row)
            if action.get("status_script"):
                self._request_portal_status(item, row=row)

    def _add_tool_status(self, row, container):
        icon, text = portal_status_presentation("")
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        status.set_valign(Gtk.Align.CENTER)
        row.status_icon = Gtk.Label(label=icon)
        row.status_text = Gtk.Label(label=text)
        for widget in (row.status_icon, row.status_text):
            widget.set_wrap(False)
            widget.set_single_line_mode(True)
            status.append(widget)
        status.set_tooltip_text(_("Checking current status…"))
        status.update_property([Gtk.AccessibleProperty.LABEL], [_("Checking status")])
        row.add_suffix(status)
        row.status_box = status
        row.portal_status_generation = container.portal_status_generation
        row.portal_status_container = container

    def _request_portal_status(self, item, *, row=None, dialog=None, refresh=False):
        if self.closing:
            return
        key = id(item)
        if row is not None:
            row.portal_status_container.portal_status_rows[key] = row
            if dialog is not None and hasattr(row, "portal_status_token"):
                self._apply_portal_status_to_dialog(dialog, item, row.portal_status_token)
            if not refresh and hasattr(row, "portal_status_token"):
                return
        action = item.get("action", {})
        script = action.get("status_script")
        if not script:
            return
        if key in self._tool_status_requests:
            self._tool_status_requests[key] |= refresh
            return
        self._tool_status_requests[key] = False

        def status_ready(token):
            if self.closing:
                return
            if self._tool_status_requests.pop(key):
                self._request_portal_status(item, refresh=True)
                return
            normalized = token.strip() if isinstance(token, str) else ""
            for container in (self.pin_group, self.tools_box, self.search_results_box):
                row = getattr(container, "portal_status_rows", {}).get(key)
                if (row is None
                        or row.portal_status_generation != container.portal_status_generation
                        or not row.is_ancestor(container)):
                    continue
                row.portal_status_token = normalized
                icon, status_text = portal_status_presentation(normalized)
                row.status_icon.set_text(icon)
                row.status_text.set_text(status_text)
                unknown = not normalized or normalized.casefold() == "unknown"
                row.status_box.set_tooltip_text(format_message(_("Current status is unknown.") if unknown else _("Current status: {status}").format(status=status_text)))
                row.status_box.update_property([Gtk.AccessibleProperty.LABEL], [_("Status {status}").format(status=status_text)])
            target_dialog = self.modal
            if (target_dialog is not None and target_dialog is self.modal
                    and getattr(target_dialog, "portal_item", None) is item):
                self._apply_portal_status_to_dialog(target_dialog, item, normalized)

        self.work(lambda: portal.status(script, tool=action.get("id") or action.get("title")),
                  status_ready, executor=self.portal_status_executor)

    def _add_favorite_hit_area(self, row, item, favorite):
        area = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        area.set_size_request(48, 44)
        # Keep this suffix fixed-width even though its internal spacer expands
        # to right-align the star. Otherwise Gtk.ActionRow may expand the
        # entire suffix and shift preceding status content by row content.
        area.set_hexpand(False)
        area.set_margin_start(12)
        area.set_margin_end(10)
        area.set_valign(Gtk.Align.CENTER)
        area.set_focusable(False)
        can_favorite = portal.resolve(self.items, portal.reference(item)) is not None
        area.set_sensitive(can_favorite)
        row.favorite_available = can_favorite
        if not can_favorite:
            area.set_tooltip_text(_("This tool has a missing or ambiguous ID"))
            area.update_property([Gtk.AccessibleProperty.LABEL], [_("Pin unavailable: missing or ambiguous ID")])
        icon = Gtk.Image.new_from_icon_name("starred-symbolic" if favorite else "non-starred-symbolic")
        icon.set_pixel_size(18)
        area.append(Gtk.Box(hexpand=True))
        area.append(icon)
        row.add_suffix(area)
        row.favorite_hit_area = area
        row.favorite_icon = icon
        row.favorite_reference = portal.reference(item)
        self._set_favorite_state(row, favorite)

        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", lambda click, *_: click.set_state(Gtk.EventSequenceState.CLAIMED))
        gesture.connect("released", lambda *_: self._toggle_favorite(item, row))
        area.add_controller(gesture)
        row.favorite_gesture = gesture

    def _set_favorite_emphasis(self, row, active):
        if not hasattr(row, "favorite_icon"):
            return
        pointer_reveal = getattr(row, "pointer_hovered", False)
        row.favorite_icon.set_opacity(1.0 if row.favorite_pinned or pointer_reveal else 0.0)

    def _set_favorite_state(self, row, favorite):
        if getattr(row, "favorite_pinned", None) == favorite:
            return
        row.favorite_pinned = favorite
        row.favorite_icon.set_from_icon_name("starred-symbolic" if favorite else "non-starred-symbolic")
        if favorite:
            row.favorite_icon.add_css_class("bazzite-favorite")
        else:
            row.favorite_icon.remove_css_class("bazzite-favorite")
        if row.favorite_available:
            row.favorite_hit_area.set_tooltip_text(format_message(_("Unpin") if favorite else _("Pin")))
            row.favorite_hit_area.update_property([Gtk.AccessibleProperty.LABEL], [_("Unpin") if favorite else _("Pin")])
        self._set_favorite_emphasis(row, False)

    def _action_row_key(self, item, row, keyval):
        if keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            self._native_input_mode()
            self._toggle_favorite(item, row)
            return True
        return False

    def _toggle_favorite(self, item, row=None):
        pin = portal.reference(item)
        if portal.resolve(self.items, pin) is None:
            return
        pins = self.store.pins()
        if pin in pins:
            pins.remove(pin)
        else:
            pins.append(pin)
        controller_restore = self.controller_navigation
        if not self._save_pins(pins):
            return
        if row is not None:
            def restore_focus():
                if self.closing or self.stack.get_visible_child_name() != N_("Tools"):
                    return GLib.SOURCE_REMOVE
                target = next((candidate for candidate in controller_focus_targets(self.tools_view)
                               if getattr(candidate, "portal_reference", None) == pin), None)
                if target:
                    if controller_restore:
                        self._set_controller_focus(target)
                    else:
                        target.grab_focus()
                    self._update_controller_legend()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(restore_focus)

    def _sync_tool_favorites(self):
        # Keep both catalog and search rows, including pending status callbacks.
        for root in (self.tools_box, self.search_results_box):
            pending = [root]
            while pending:
                widget = pending.pop()
                if hasattr(widget, "favorite_icon"):
                    self._set_favorite_state(widget, widget.favorite_reference in self._pins)
                child = widget.get_first_child()
                while child:
                    pending.append(child)
                    child = child.get_next_sibling()

    def open_action(self, item):
        action = item["action"]
        if action.get("options") or action.get("status_script"):
            self._action_dialog(action, item)
        elif self.store.preferences()["skip_one_shot_confirmation"]:
            self._launch_script(action.get("script"))
        else:
            self._action_dialog(action, item, confirmation=True)

    def _refresh_tool_status(self, item):
        if self.closing:
            return GLib.SOURCE_REMOVE
        self._request_portal_status(item, refresh=True)
        return GLib.SOURCE_REMOVE

    def _refresh_dirty_tools(self):
        if self.closing or not self.is_active():
            return
        dirty, self._dirty_tools = self._dirty_tools, {}
        for item in dirty.values():
            self._refresh_tool_status(item)

    def _launch_script(self, script, refresh=None, *, item=None):
        if not script:
            self.toast(N_("No command is configured for this tool."))
            return
        def launched(process):
            if refresh:
                refresh()
            if item is None or not item["action"].get("status_script"):
                return
            self._dirty_tools[id(item)] = item
            def finished():
                process.wait()
                GLib.idle_add(self._refresh_tool_status, item)
            # Waiting for an interactive terminal must not occupy a work-pool
            # slot or keep Portal alive after its window closes.
            threading.Thread(target=finished, daemon=True).start()
        self.work(lambda: portal.terminal(script), launched)

    def _apply_portal_status_to_dialog(self, dialog, item, token):
        if dialog is not self.modal or getattr(dialog, "portal_item", None) is not item:
            return
        _icon, status_text = portal_status_presentation(token)
        if getattr(dialog, "portal_status_label", None) is not None:
            unknown = not isinstance(token, str) or token.strip().casefold() in {"", "unknown"}
            dialog.portal_status_label.set_text(_("Current status is unknown.") if unknown
                                                else _("Current status: {status}").format(status=status_text))
        normalized = token.casefold() if isinstance(token, str) else ""
        options = {option.get("id"): option for option in item["action"].get("options") or []}
        for option_id, widget in getattr(dialog, "option_buttons", {}).items():
            widget.remove_css_class("suggested-action")
            if normalized not in ("", "unknown") and option_id == normalized and option_id in options:
                widget.add_css_class("suggested-action")

    def _action_dialog(self, action, item=None, confirmation=False, *, on_confirm=None):
        display_action = format_message if on_confirm is not None else lambda text: text
        dialog = Adw.Window(transient_for=self, modal=True,
                            title=display_action((action.get("confirmation_title", _('Run {value1}?').format(value1=action.get('title', 'tool'))) if confirmation else action.get("title", N_("Tool")))),
                            default_width=480)
        dialog.set_application(self.get_application())
        dialog.portal_item = item
        dialog.option_buttons = {}
        self.modal = dialog
        restore = None
        page_name = self.stack.get_visible_child_name()
        if item is not None and page_name in (N_("Home"), N_("Tools")):
            view_name = "search" if self.tools_search_active else "catalog"
            if page_name == N_("Tools"):
                targets = controller_focus_targets(self.tools_view)
                scroll = self.search_results_scroll if self.tools_search_active else self.catalog_scroll
            else:
                targets = self.pin_rows
                scroll = self.stack.get_child_by_name(page_name)
            opener = next((target for target in targets
                           if getattr(target, "portal_item", None) is item), None)
            restore = (opener, view_name, scroll.get_vadjustment().get_value(), self.controller_navigation)
        toolbar = Adw.ToolbarView()
        set_window_content(dialog, toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())
        self._install_input_mode_controllers(dialog)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for name in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + name)(24)
        toolbar.set_content(box)
        first_option = None
        if confirmation:
            box.append(label(action.get("title", N_("Tool")), "title-3", translate=on_confirm is not None))
            if on_confirm is None:
                box.append(label(N_("This command opens in your desktop terminal."), "heading"))
            if action.get("description"):
                box.append(label(action.get("description", ""), translate=on_confirm is not None))
            dialog.confirmation_focus_anchor = Gtk.Box()
            dialog.confirmation_focus_anchor.set_focusable(True)
            dialog.confirmation_focus_anchor.controller_exclude_focus = True
            dialog.confirmation_focus_anchor.append(label(action.get("question", _("Do you want to run this tool?")), "heading", translate=on_confirm is not None))
            box.append(dialog.confirmation_focus_anchor)
            choices = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                              halign=Gtk.Align.CENTER)
            labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
            self.confirmation_prompts = [
                make_button_prompt(command, labels, N_("Yes") if command == "confirm" else N_("No"), self._prompt_style())
                for command in ("confirm", "back")
            ]
            self.confirmation_prompt_labels = (_("Yes"), _("No"))
            for prompt in self.confirmation_prompts:
                set_prompt_visible(prompt, self._prompts_visible())

            def confirm_yes():
                dialog.close()
                if on_confirm is not None:
                    on_confirm()
                else:
                    self._launch_script(action.get("script"), item=item)

            for prompt, text, callback in zip(self.confirmation_prompts, (N_("Yes"), N_("No")),
                                               (confirm_yes, dialog.close)):
                choice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                choice.append(prompt)
                control = button(format_message(text), callback, translate=False)
                control.controller_exclude_focus = True
                control.add_css_class("confirmation-choice")
                choice.append(control)
                choices.append(choice)
                if text == N_("Yes"):
                    first_option = control
                    dialog.confirm_yes_button = control
                else:
                    dialog.confirm_no_button = control
            box.append(choices)
        else:
            box.append(label(action.get("description", ""), translate=on_confirm is not None))
            if action.get("status_script"):
                dialog.portal_status_label = label(
                    N_("Checking current status…"),
                    "heading",
                )
                box.append(dialog.portal_status_label)
            def run_option(option):
                dialog.close()
                self._launch_script(option.get("script"), item=item)
            for option in action.get("options") or []:
                widget = ModalOptionButton(label=option.get("label", N_("Run")))
                widget.controller_label = option.get("label", N_("Run"))
                widget.update_property([Gtk.AccessibleProperty.LABEL], [widget.controller_label])
                widget.connect("clicked", lambda *_args, selected=option: run_option(selected))
                if option.get("id"):
                    dialog.option_buttons[option["id"]] = widget
                text = widget.get_child()
                text.set_margin_start(34)
                text.set_margin_end(34)
                overlay = Gtk.Overlay()
                widget.set_child(None)
                overlay.set_child(text)
                widget.set_child(overlay)
                prompt = make_button_prompt("confirm", accessible_name=N_("Select"), style=self._prompt_style())
                prompt.set_halign(Gtk.Align.END)
                prompt.set_valign(Gtk.Align.CENTER)
                prompt.set_margin_end(8)
                set_prompt_visible(prompt, False)
                overlay.add_overlay(prompt)
                overlay.set_measure_overlay(prompt, False)
                widget.controller_prompt_overlay = overlay
                widget.controller_selection_prompt = prompt
                box.append(widget)
                if first_option is None:
                    first_option = widget
            if item is not None and action.get("status_script"):
                current_row = next((rows[id(item)]
                                    for container in (self.pin_group, self.tools_box, self.search_results_box)
                                    if id(item) in (rows := getattr(container, "portal_status_rows", {}))), None)
                if current_row is not None:
                    self._request_portal_status(
                        item, row=current_row,
                        dialog=dialog,
                    )
                else:
                    self._request_portal_status(item, dialog=dialog)
        # This tool provides status only; it has no action commands.
        def focus_first_option(*_args):
            if dialog is not self.modal or not dialog.get_mapped():
                return GLib.SOURCE_REMOVE
            if confirmation:
                # Keep GTK's native focus away from Yes/No while preserving
                # their normal Tab and pointer access.
                dialog.confirmation_focus_anchor.grab_focus()
                return GLib.SOURCE_REMOVE
            if first_option is None:
                return GLib.SOURCE_REMOVE
            if self.controller_navigation:
                self._set_controller_focus(first_option)
                set_prompt_visible(self.legend, True)
            else:
                first_option.grab_focus()
            self._update_controller_legend()
            return GLib.SOURCE_REMOVE

        dialog.connect("map", lambda *_args: GLib.idle_add(focus_first_option))

        def closed(*_):
            app = self.get_application()
            restore_parent_activation = dialog.is_active() or (app is not None and app.get_active_window() is dialog)
            self._last_pointer_position.pop(dialog, None)
            self.confirmation_prompts = []
            self.modal = None
            if restore is not None:
                def restore_opener():
                    opener, view_name, value, opened_with_controller = restore
                    if self.closing or self.modal is not None or self.stack.get_visible_child_name() != page_name:
                        return GLib.SOURCE_REMOVE
                    if restore_parent_activation:
                        self.present()
                    if page_name == N_("Tools"):
                        self.tools_search_active = view_name == "search"
                        self.tools_view.set_visible_child_name(view_name)
                    scroll.get_vadjustment().set_value(value)
                    if opener is not None and opener.get_mapped():
                        if opened_with_controller and self.controller_navigation:
                            self._set_controller_focus(opener)
                        else:
                            self._native_input_mode()
                            opener.grab_focus()
                    self._update_controller_legend()
                    if not self.controller_navigation:
                        set_prompt_visible(self.legend, self._prompts_visible())
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(restore_opener)
            return False
        dialog.connect("close-request", closed)
        if confirmation:
            dialog.set_focus(dialog.confirmation_focus_anchor)
        dialog.present()

    def _build_updates(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(18)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=640)
        clamp.set_child(box)
        page = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        page.set_child(clamp)
        self.updates_content_box = box
        box.set_valign(Gtk.Align.START if self.store.preferences()["show_logs"] else Gtk.Align.CENTER)

        controls = Gtk.CenterBox()
        self.update_controls = controls
        action_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.update_action_group = action_group
        self.update_a_hint = make_button_prompt("confirm", accessible_name=N_("Update"), style=self._prompt_style())
        set_prompt_visible(self.update_a_hint, self._prompts_visible())
        self.update_button = UpdateActionButton(N_("Update"), self._primary_update_action)
        self.update_button.set_prompt(self.update_a_hint)
        self.update_button.add_css_class("bazzite-update-cta")
        self.update_button.action_label.add_css_class("title-3")
        action_group.append(self.update_button)
        controls.set_center_widget(action_group)
        self.changelog_x_hint = make_button_prompt("focus_search", accessible_name=N_("Show changelog"), style=self._prompt_style())
        set_prompt_visible(self.changelog_x_hint, self._prompts_visible())
        self.release_notes_button = UpdateActionButton(N_("Changelog"), self._show_release_notes)
        self.release_notes_button.action_label.add_css_class("title-3")
        self.release_notes_button.set_prompt(self.changelog_x_hint)
        self.release_notes_button.set_sensitive(False)
        action_group.append(self.release_notes_button)
        for action in (self.update_button, self.release_notes_button):
            action.set_size_request(220, 56)
        self.update_button.controller_exclude_focus = True
        self.release_notes_button.controller_exclude_focus = True
        box.append(controls)

        self.update_steps_group = Adw.PreferencesGroup()
        self.update_steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.update_steps_group.add(self.update_steps_box)
        self.update_steps_group.set_visible(False)
        box.append(self.update_steps_group)
        self.log_buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.log_buffer, editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.add_css_class("bazzite-update-log")
        view.set_margin_top(12)
        view.set_margin_bottom(12)
        view.set_margin_start(12)
        view.set_margin_end(12)
        log_scroll = Gtk.ScrolledWindow(min_content_height=240, max_content_height=320,
                                      propagate_natural_height=True)
        log_scroll.set_child(view)
        self.log_scroll = log_scroll
        self.empty_log_label = label(N_("No log output available yet."), "dim-label")
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        details.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.log_heading = label(N_("Update log"), "heading")
        details.append(self.log_heading)
        details.append(self.empty_log_label)
        details.append(log_scroll)
        details.set_margin_top(12)
        details.set_margin_start(12)
        details.set_margin_end(12)
        self.update_console_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                                   transition_duration=220, reveal_child=False)
        self.update_console_revealer.set_child(details)
        self._update_console_open = bool(self.store.preferences()["show_logs"])
        box.append(self.update_console_revealer)

        self.update_error = label()
        self.update_error.set_selectable(True)
        self.update_error.add_css_class("error")
        self.update_error.set_visible(False)
        box.append(self.update_error)
        self.update_transaction_notice = label(C_('An {rpm_ostree} transaction is active. Wait for it to finish before updating or restarting.', rpm_ostree='RPM-OSTree'), "dim-label")
        self.update_transaction_notice.set_visible(False)
        box.append(self.update_transaction_notice)
        self.release_identity = None
        self.stack.add_named(page, N_("Updates"))

    def _step_indicator(self, status, fraction=None):
        normalized = status.casefold().replace("-", "_") if isinstance(status, str) else ""
        if normalized == "finished":
            image = app_icon("circle-check", 30)
            image.add_css_class("dim-label")
            image.update_property([Gtk.AccessibleProperty.LABEL], [_("Finished · awaiting update result")])
            return image
        if normalized == "success":
            image = app_icon("circle-check", 30)
            image.add_css_class("success")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Completed")])
            return image
        if normalized == "failed":
            image = app_icon("circle-x", 30)
            image.add_css_class("error")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Failed")])
            return image
        if normalized == "pending":
            image = app_icon("clock", 30)
            image.add_css_class("dim-label")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Waiting to start")])
            return image
        if normalized == "skipped":
            image = app_icon("circle-minus", 30)
            image.add_css_class("dim-label")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Skipped")])
            return image
        if normalized in {"active", "running", "in_progress", "in-progress"}:
            if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and 0 <= fraction <= 1:
                ring = Gtk.DrawingArea()
                ring.set_content_width(30)
                ring.set_content_height(30)
                percent = round(fraction * 100)
                # xgettext: no-python-format
                ring.update_property([Gtk.AccessibleProperty.LABEL], [_('{percent}% complete').format(percent=percent)])
                def draw(_area, cr, width, height):
                    cx, cy, radius = width / 2, height / 2, min(width, height) / 2 - 2
                    cr.set_line_width(2.4)
                    cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
                    cr.arc(cx, cy, radius, 0, 6.283185307)
                    cr.stroke()
                    cr.set_source_rgba(0.541, 0.169, 0.886, 1.0)
                    cr.arc(cx, cy, radius, -1.570796327, -1.570796327 + 6.283185307 * fraction)
                    cr.stroke()
                ring.set_draw_func(draw)
                return ring
            spinner = Adw.Spinner()
            spinner.set_size_request(30, 30)
            spinner.update_property([Gtk.AccessibleProperty.LABEL], [N_("In progress")])
            return spinner
        image = app_icon("circle-help", 30)
        image.add_css_class("dim-label")
        image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Status unavailable")])
        return image

    def _render_update_steps(self, modules):
        rows = getattr(self, "_update_step_rows", {})
        retained = {}
        if not isinstance(modules, list) or not modules:
            for row in rows.values():
                self.update_steps_box.remove(row)
            self._update_step_rows = {}
            self.update_steps_group.set_visible(False)
            return
        self.update_steps_group.set_visible(True)
        reading_progress = self.snapshot.running and not any(
            isinstance(module, dict) and module.get("status") in {"running", "finished", "success", "failed"}
            for module in modules)
        previous = None
        for module in modules:
            if not isinstance(module, dict):
                continue
            name = str(module.get("name") or N_("Update step"))
            identity = str(module.get("id") or name)
            if identity == "system" or name == N_("System"):
                name = C_('{bazzite} System', bazzite='Bazzite')
            status = str(module.get("status") or "unknown")
            status_text = {"pending": N_("Waiting to update"), "skipped": N_("Skipped"), "unknown": N_("Status unavailable")}.get(status, status.replace("_", " ").capitalize())
            detail = str(module.get("error") or module.get("detail") or "").strip()
            if status == "failed" or module.get("error"):
                detail = public_error(detail, N_("Update failed. Open the update log for details."), context="Update step: " + identity,
                                      logs=module.get("logs", ()) if status == "failed" else ())
            up_to_date = identity == "system" and status == "skipped" and detail == N_("No system update is available.")
            if up_to_date:
                status = "success"
            if status == "finished":
                detail = N_("Finished · awaiting update result")
            elif status == "success" and identity in {"brew", "flatpak", "system"} and not up_to_date:
                detail = N_("Updated")
            if status == "success" and identity == "system" and not up_to_date and getattr(self.snapshot, "deployment", ""):
                detail = N_("New version ready on next restart")
            elif status == "pending":
                detail = N_("Reading update progress…") if reading_progress else status_text
            elif not detail:
                detail = status_text
            row = rows.get(identity)
            if row is None:
                row = Adw.ActionRow()
                row.set_use_markup(False)
                row.set_subtitle_lines(1)
                row.add_css_class("bazzite-update-step-row")
                row.set_focusable(False)
                row.controller_exclude_focus = True
                self.update_steps_box.append(row)
            row.set_title(format_message(name))
            row.set_subtitle(format_message(detail))
            indicator_state = (status, module.get("fraction"))
            if getattr(row, "indicator_state", None) != indicator_state:
                if hasattr(row, "step_indicator"):
                    row.remove(row.step_indicator)
                row.step_indicator = self._step_indicator(*indicator_state)
                row.indicator_state = indicator_state
                row.add_prefix(row.step_indicator)
            if module.get("logs_missing") or module.get("logs_available") is False:
                row.set_subtitle(" · ".join(filter(None, [row.get_subtitle(), _("Logs unavailable")])))
            elif module.get("logs_available") is True and not module.get("logs"):
                row.set_subtitle(" · ".join(filter(None, [row.get_subtitle(), _("No module log output")])))
            self.update_steps_box.reorder_child_after(row, previous)
            previous = row
            retained[identity] = row
        for identity, row in rows.items():
            if identity not in retained:
                self.update_steps_box.remove(row)
        self._update_step_rows = retained

    def _deployment_transaction_active(self):
        management = self.snapshot.management
        if isinstance(management, dict) and management:
            return bool((management.get("summary") or {}).get("transaction"))
        return bool((getattr(self, "image_deployment_status", None) or {}).get("transaction"))

    def _active_system_operations(self):
        """Include requests awaiting service discovery, not just live transactions."""
        state = self.snapshot
        active = set()
        if state.running or getattr(self, "_update_request_pending", False):
            active.add("update")
        checks = {
            "rollback": (getattr(self, "rollback_request_active", False)
                         or getattr(state, "rollback_state", "") == "running"),
            "cancel": (getattr(self, "cancel_request_active", False)
                       or getattr(self, "cancel_waiting_snapshot", False)
                       or getattr(state, "cancel_state", "") == "running"),
            "layer": (getattr(self, "layers_request_active", False)
                      or getattr(state, "layer_state", "") == "running"),
            "cache": (getattr(self, "cache_request_active", False)
                      or getattr(state, "cache_state", "") == "running"),
            "channel": (getattr(self, "channel_request_active", False)
                        or getattr(state, "channel_state", "") == "running"
                        or isinstance(getattr(self, "channel_status", None), dict)
                        and self.channel_status.get("state") == "running"),
            "image": (getattr(self, "image_request_active", False)
                      or getattr(state, "image_state", "") == "running"
                      or isinstance(getattr(self, "image_status", None), dict)
                      and self.image_status.get("state") == "running"),
            "delete": (getattr(self, "delete_request_active", False)
                       or getattr(state, "delete_state", "") == "running"
                       or isinstance(getattr(self, "delete_status", None), dict)
                       and self.delete_status.get("state") == "running"),
        }
        active.update(kind for kind, running in checks.items() if running)
        active.update(kind for kind in getattr(self, "maintenance_pending_units", {})
                      if kind in checks)
        return active

    def _management_blocker_reason(self, excluding=None):
        """Return the shared explanation when another system action is active."""
        if excluding and self._maintenance_row_should_open(excluding):
            return ""
        if self._deployment_transaction_active():
            return N_('Another system operation is running. Wait for it to finish.')
        active = self._active_system_operations() - {excluding}
        if active:
            return N_('Another system operation is running. Wait for it to finish.')
        return ""

    def _primary_update_action(self):
        if not self.status_received:
            return
        if self._management_blocker_reason():
            return
        if not self.snapshot.available:
            return
        if self.snapshot.deployment:
            if not self.snapshot.deployment_known:
                return
            self.confirm(C_('Restart {bazzite}?', bazzite='Bazzite'), C_('All update steps have finished. Restart now to start the prepared {bazzite} version. Save your work before continuing.', bazzite='Bazzite'), N_("Restart"), lambda: self._operation(N_("Restart")))
            return
        self._awaiting_update_run = self.snapshot.run
        self._update_request_pending = True
        self._set_log_text("")
        self._render_status()
        self._operation("StartUpdate")

    def _show_release_notes(self):
        if self.modal or self.closing or not self.release_identity:
            return
        identity = dict(self.release_identity)
        dialog = Adw.Window(transient_for=self, modal=True, default_width=900, default_height=680,
                            title=identity["role"] + " changelog · " + identity["version"])
        dialog.set_application(self.get_application())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        set_window_content(dialog, toolbar)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_margin_top(16)
        scroll.set_margin_bottom(16)
        scroll.set_margin_start(20)
        scroll.set_margin_end(20)
        scroll.set_child(label("Loading the matching GitHub release notes…"))
        toolbar.set_content(scroll)
        dialog.maintenance_scroll = scroll
        dialog.maintenance_legend = ControllerLegend()
        dialog.maintenance_legend.set_margin_start(20)
        dialog.maintenance_legend.set_margin_end(20)
        dialog.maintenance_legend.set_margin_top(8)
        dialog.maintenance_legend.set_margin_bottom(16)
        toolbar.add_bottom_bar(dialog.maintenance_legend)
        self.modal = dialog
        self._sync_maintenance_dialog()
        self._install_input_mode_controllers(dialog)
        def closed(*_):
            if self.modal is dialog:
                self.modal = None
            self._last_pointer_position.pop(dialog, None)
            def restore():
                if not self.closing:
                    self.present()
                    if self.controller_navigation:
                        self._clear_controller_focus_visuals()
                    else:
                        self.release_notes_button.grab_focus()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(restore)
            return False
        dialog.connect("close-request", closed)
        if gaming_mode():
            dialog.fullscreen()
        dialog.present()
        def loaded(result):
            if self.modal is not dialog:
                return
            if result.get("state") == "available":
                body = result.get("body") or N_("This release has no written notes.")
                from bazzite_portal.ui.release_notes_view import build_release_notes
                scroll.set_child(build_release_notes(body))
            else:
                scroll.set_child(label(error_text(result.get("error"), N_("Could not load the changelog. Try again later."), context="Changelog") or N_("No matching release notes were found.")))
        self.work(lambda: fetch_release_notes(identity["version"], identity["channel"]), loaded)

    def _set_release_identity(self, summary):
        entries = summary.get("entries", []) if isinstance(summary, dict) else []
        selected = next((entry for entry in entries if entry.get("role") == N_("Next restart")), None)
        selected = selected or next((entry for entry in entries if entry.get("role") == N_("Running now")), None)
        self.release_identity = None
        if selected:
            parts = system_presentation._reference_parts(selected.get("image"))
            channel = system_presentation._track(parts).lower()
            version = selected.get("version", N_("Unknown"))
            if channel in ("stable", "testing") and version != N_("Unknown"):
                self.release_identity = {"version": version, "channel": channel,
                                         "role": N_("Pending update") if selected["role"] == N_("Next restart") else N_("Installed version")}
        self.release_notes_button.set_sensitive(self.release_identity is not None)
        if self.release_identity:
            self.release_notes_button.set_tooltip_text(format_message(self.release_identity["role"] + " changelog · " + self.release_identity["version"]))
        else:
            self.release_notes_button.set_tooltip_text(_("No verified release version and channel are available."))

    def _set_update_console(self, open_console):
        open_console = bool(open_console)
        if hasattr(self, "updates_content_box"):
            self.updates_content_box.set_valign(Gtk.Align.START if open_console else Gtk.Align.CENTER)
        was_open = self._update_console_open
        if open_console and not was_open:
            self._update_log_previous_focus = self.get_focus()
        self._update_console_open = open_console
        visible = self.stack.get_visible_child_name() == N_("Updates") and self._update_console_open
        self.update_console_revealer.set_reveal_child(visible)
        if was_open and not open_console:
            restore = (self.controller_focus_widget if self.controller_navigation
                       else getattr(self, "_update_log_previous_focus", None))
            if restore is not None and restore.get_visible() and restore.get_sensitive():
                restore.grab_focus()

    def _snapshot_changed(self, data):
        previous_run = self.snapshot.run
        previously_received = self.status_received
        previous_running = self.snapshot.running
        previous_deployment = self.snapshot.deployment
        previous_cancel_state = getattr(self.snapshot, "cancel_state", "unavailable")
        previous_layer_state = getattr(self.snapshot, "layer_state", "unavailable")
        allowed = Snapshot.__dataclass_fields__
        self.snapshot = Snapshot(**{key: value for key, value in data.items() if key in allowed})
        viewed_records = data.get("maintenance_viewed", ())
        if not isinstance(viewed_records, (list, tuple)):
            viewed_records = ()
        service_acknowledged = set()
        for record in viewed_records:
            if not isinstance(record, (list, tuple)) or len(record) != 4:
                continue
            kind, unit, invocation, generation = record
            if (isinstance(kind, str)
                    and kind in {"rollback", "cancel", "cache", "layer", "delete", "channel", "image"}
                    and isinstance(unit, str) and 0 < len(unit) <= 255
                    and isinstance(invocation, str) and len(invocation) <= 32
                    and type(generation) is int and 0 <= generation <= 2**63 - 1):
                service_acknowledged.add((kind, unit, invocation, generation))
        pending_acknowledgements = getattr(self, "maintenance_acknowledgement_pending", set())
        pending_acknowledgements.difference_update(service_acknowledged)
        pending_acknowledgements = {
            key for key in pending_acknowledgements
            if self._maintenance_key(key[0]) == key[1:]
        }
        self.maintenance_acknowledgement_pending = pending_acknowledgements
        self.maintenance_acknowledged = service_acknowledged | pending_acknowledgements
        self.status_received = True
        shared_management = bool(self.snapshot.management)
        if shared_management:
            self._apply_management_snapshot(self.snapshot.management)
        if self.snapshot.running or self.snapshot.run != getattr(self, "_awaiting_update_run", None):
            self._awaiting_update_run = None
            self._update_request_pending = False
        if self.snapshot.run and (self.snapshot.running or
                (previously_received and self.snapshot.run != previous_run)):
            self._observed_update_run = self.snapshot.run
        if self.cancel_waiting_snapshot and (
                self.snapshot.cancel_state not in {"idle", "unavailable"}
                or not self.snapshot.deployment_known
                or not self.snapshot.deployment
                or (self.cancel_target and self.cancel_target.get("identity") != self.snapshot.deployment)):
            self.cancel_waiting_snapshot = False
            self.cancel_feedback = None
        if not shared_management and (previous_deployment != self.snapshot.deployment or
                (not previous_running and self.snapshot.running)):
            self.deployment_summary_fresh = False
        cancel_finished = (previous_cancel_state == "running"
                           and self.snapshot.cancel_state in {"success", "failed", "idle", "unknown"})
        if cancel_finished and not shared_management:
            self.cancel_target_fresh = False
        self._render_status()
        self._acknowledge_update_result()
        if (not shared_management and not self.system_loading and
                (previous_deployment != self.snapshot.deployment or
                 (previous_running and not self.snapshot.running))):
            # Refresh the known changelog version after staging/completion.
            # This reads deployment metadata in a worker, without a network call.
            self._refresh_system()
        if hasattr(self, "layers_row"):
            self._sync_layers_ui()
        if hasattr(self, "clear_cache_row"):
            self._sync_clear_cache_ui()
        self._sync_maintenance_dialog()
        if cancel_finished and not shared_management:
            self._refresh_system_after_cancel()
        if previous_layer_state == "running" and getattr(self.snapshot, "layer_state", "unknown") != "running":
            if not shared_management:
                self.cancel_target_fresh = False
            self._sync_layers_ui()
            if not shared_management:
                self._refresh_system_after_cancel()
        self._maybe_open_maintenance_details()

    def _set_log_text(self, text, follow_tail=False):
        text = str(text)[-12000:]
        has_logs = bool(text)
        self.log_scroll.set_visible(has_logs)
        self.empty_log_label.set_visible(not has_logs)
        if text == self.log_text:
            return
        adjustment = self.log_scroll.get_vadjustment()
        was_at_bottom = adjustment.get_upper() - (adjustment.get_value() + adjustment.get_page_size()) <= 8
        self.log_buffer.set_text(text)
        self.log_text = text
        if follow_tail and was_at_bottom:
            def scroll_tail():
                adjustment.set_value(max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()))
                return GLib.SOURCE_REMOVE
            GLib.idle_add(scroll_tail)

    def _acknowledge_update_result(self):
        state = self.snapshot
        if (self.client is not None and self.get_mapped()
                and self.stack.get_visible_child_name() == N_("Updates")
                and not state.running and state.result in {"failed", "success"} and state.run):
            if not self.is_active():
                return
            if state.result_viewed_run == state.run or getattr(self, "_dismissed_result_run", None) == state.run:
                return
            self._visible_result_run = state.run
            self.client.updates_viewed(state.run)

    def _render_status(self):
        state = self.snapshot
        for kind in tuple(self.maintenance_pending_units):
            self._reconcile_maintenance_pending(kind)
        active_operations = self._active_system_operations()
        maintenance_busy = bool(active_operations - {"update"})
        historical_run = bool(state.run and not state.running
                              and state.run != getattr(self, "_observed_update_run", ""))
        dismissed_result = (not state.running and state.result in {"success", "failed"}
                             and (state.run == getattr(self, "_dismissed_result_run", None)
                                  or (state.run and state.result_viewed_run == state.run
                                      and state.run != getattr(self, "_visible_result_run", None))))
        awaiting_run = getattr(self, "_awaiting_update_run", None) == state.run
        self.update_steps_group.set_title(format_message(""))
        previous_update = bool(state.module_plan and awaiting_run)
        modules = state.module_plan if previous_update else state.modules
        if dismissed_result:
            modules = state.module_plan or [
                {"id": row.get("id"), "name": row.get("name"), "status": "pending"}
                for row in state.modules if isinstance(row, dict)
            ]
        if not self.store.preferences()["show_disabled_update_steps"]:
            disabled = {row["id"] for row in state.module_plan if row.get("status") == "skipped"}
            modules = [row for row in modules if row.get("id") not in disabled]
        self._render_update_steps(modules)
        self.log_heading.set_text(_("Update log"))
        if state.running:
            text = N_("Rolling back…") if state.operation == "rollback" else C_('{bazzite} is updating…', bazzite='Bazzite')
        elif active_operations & {"channel", "image"}:
            text = N_("Rebasing…")
        elif "rollback" in active_operations:
            text = N_("Rolling back…")
        elif maintenance_busy:
            text = N_("System operation in progress…")
        else:
            text = C_('Update {bazzite} and Apps', bazzite='Bazzite')
        detail = state.stage if state.running and state.stage else ""
        if state.running:
            active_modules = [module for module in modules
                              if isinstance(module, dict) and module.get("status") == "running"]
            active = next((module for module in active_modules
                           if state.stage.casefold() in {
                               str(module.get("id", "")).casefold(),
                               str(module.get("name", "")).casefold()}),
                          active_modules[0] if active_modules else None)
            if active is not None:
                identity = str(active.get("id") or active.get("name") or N_("Update step"))
                row = self._update_step_rows.get(identity)
                if row is not None:
                    detail = " • ".join(filter(None, (row.get_title(), row.get_subtitle())))
        status_error = error_text(state.error, N_('Details are unavailable. Open Logs for more information.'), context='_render_status')
        if state.running and status_error:
            detail = (detail + " · " if detail else "") + status_error
        if maintenance_busy and not state.running:
            detail = N_('Another system operation is running. Wait for it to finish.')
        self.home_status.set_title(format_message(text))
        self.home_status.set_subtitle(format_message(format_message(detail) if detail else _("Update your system and installed apps.")))
        errors = []
        transaction_active = self._deployment_transaction_active()
        self.update_transaction_notice.set_text(format_message(
            N_('Another system operation is running. Wait for it to finish.') if maintenance_busy
            else C_('An {rpm_ostree} transaction is active. Wait for it to finish before updating or restarting.', rpm_ostree='RPM-OSTree')))
        self.update_transaction_notice.set_visible((transaction_active or maintenance_busy) and not state.running)
        if status_error and state.result != "failed":
            errors.append(status_error)
        if state.deployment and not state.deployment_known:
            errors.append(C_('A system change is pending, but {app_name} could not verify it. Restart is unavailable until its status can be checked.', app_name='Bazzite Portal'))
        if self.status_received and not state.available:
            errors.append(C_('{bazzite} updates are unavailable. Open Logs for details.', bazzite='Bazzite'))
        self.update_error.set_text("\n".join(public_error(error, N_("Could not read update status. Open Logs for details."), context='Update status') for error in filter(None, errors)))
        self.update_error.set_visible(bool(errors))
        reboot_ready = bool(state.deployment and state.deployment_known and not state.running)
        request_pending = getattr(self, "_update_request_pending", False)
        if state.running or request_pending:
            update_label = _("Updating…")
        elif maintenance_busy or transaction_active:
            update_label = _("Busy")
        elif reboot_ready:
            update_label = _("Restart")
        else:
            update_label = _("Update")
        self.update_button.set_label(format_message(update_label))
        self.update_button.set_visible(True)
        self.update_button.set_sensitive(self.status_received and state.available
            and not state.running and not request_pending and not transaction_active and not maintenance_busy
            and (not state.deployment or state.deployment_known))
        self._sync_rollback_ui()
        self._sync_cancel_pending_ui()
        self._sync_channel_ui()
        self._sync_delete_deployments_ui()
        self._sync_image_ui()
        self._set_log_text("" if historical_run or awaiting_run or dismissed_result else "\n".join(state.logs), follow_tail=True)
        self._refresh_update_prompts()
        self._update_controller_legend()
        self._render_home_activity()

    def _operation(self, method):
        if method == "StartUpdate":
            def rejected(error):
                if self.closing:
                    return
                self._update_request_pending = False
                self._awaiting_update_run = None
                self._render_status()
                self.toast(public_error(error, N_("The update request could not be sent. Open Logs for details."), context='Start update'))
            try:
                if self.client is None or getattr(self.client, "closed", False):
                    rejected(N_("System status is unavailable. Open Logs for details."))
                else:
                    self.client.call(method, error_callback=rejected)
            except Exception as error:
                rejected(error)
        elif self.client:
            self.client.call(method)

    def confirm(self, title, body, action, callback, *, informational=False, documentation_url=None):
        dialog = Adw.Window(transient_for=self, modal=True, title=format_message(title), default_width=480)
        dialog.set_application(self.get_application())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        set_window_content(dialog, toolbar)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for name in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + name)(24)
        toolbar.set_content(box)
        box.append(label(body))
        if documentation_url:
            documentation = Gtk.Label(xalign=0, wrap=True)
            documentation.set_markup('<a href="' + GLib.markup_escape_text(documentation_url) + '">'
                                     + GLib.markup_escape_text(_("Read the rebase documentation")) + '</a>')
            documentation.connect("activate-link", lambda _label, uri: (
                self._run_xdg_open(uri, N_("Could not open link")), True)[1])
            box.append(documentation)
        labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
        self.confirmation_prompts = [
            make_button_prompt(command, labels, action if command == "confirm" else N_("Cancel"), self._prompt_style())
            for command in (("confirm",) if informational else ("confirm", "back"))
        ]
        self.confirmation_prompt_labels = (action,) if informational else (action, N_("Cancel"))
        for prompt in self.confirmation_prompts:
            set_prompt_visible(prompt, self._prompts_visible())
        choices = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          halign=Gtk.Align.CENTER)
        actions = [(action, callback, self.confirmation_prompts[0])]
        if not informational:
            actions.append((N_("Cancel"), None, self.confirmation_prompts[1]))
        for text, callback_fn, prompt in actions:
            choice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            choice.append(prompt)
            control = button(text, lambda cb=callback_fn: (dialog.close(), cb() if cb else None))
            control.controller_exclude_focus = True
            control.add_css_class("confirmation-choice")
            if text == action:
                control.add_css_class("suggested-action" if informational else "destructive-action")
                dialog.confirm_yes_button = control
            else:
                dialog.confirm_no_button = control
            choice.append(control)
            choices.append(choice)
        box.append(choices)
        self.modal = dialog
        self._install_input_mode_controllers(dialog)
        dialog.connect("close-request", lambda *_: self._clear_confirm_modal(dialog))
        dialog.present()
        self._update_controller_legend()

    def _clear_confirm_modal(self, dialog):
        if self.modal is dialog:
            self.modal = None
            self.confirmation_prompts = []
            self.confirmation_prompt_labels = (_("Yes"), _("No"))
        self._update_controller_legend()
        return False


    def _build_announcements(self):
        page, box = content_page(N_("Community"), C_('Visit {bazzite} documentation, community spaces, and source code.', bazzite='Bazzite'))
        resources = group(N_("Resources"))
        self.community_link_rows = {}
        self.community_favicon_images = {}
        self.community_favicon_paths = {}
        self.community_favicon_fetch_started = False
        for link_id, title, description, url in community.LINKS:
            row = Adw.ActionRow(title=GLib.markup_escape_text(format_message(title)), subtitle=GLib.markup_escape_text(format_message(description)))
            row.controller_focus_row = True
            row.set_activatable(True)
            row.connect("activated", lambda *_args, target=url: self._open_community_link(target))
            tile = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            tile.add_css_class("community-favicon-tile")
            tile.set_size_request(32, 32)
            tile.set_hexpand(False)
            tile.set_vexpand(False)
            tile.set_halign(Gtk.Align.CENTER)
            tile.set_valign(Gtk.Align.CENTER)
            image = Gtk.Image.new_from_icon_name(self._community_fallback_icon(link_id))
            image.set_pixel_size(24)
            image.set_hexpand(True)
            image.set_vexpand(True)
            image.set_halign(Gtk.Align.CENTER)
            image.set_valign(Gtk.Align.CENTER)
            tile.append(image)
            row.add_prefix(tile)
            row.favicon_tile = tile
            row.favicon_image = image
            self.community_favicon_images[link_id] = image
            self._set_community_favicon(link_id, self.community_favicon_paths.get(link_id))
            resources.add(row)
            self.community_link_rows[link_id] = row
        box.append(resources)
        self.stack.add_named(page, N_("Community"))

        announcements_page, announcements_box = content_page(
            N_("Announcements"), C_('{bazzite} release and project announcements.', bazzite='Bazzite')
        )
        announcement_status = Adw.ActionRow(title=_("No announcements available"))
        announcement_status.set_subtitle_lines(2)
        announcement_status.set_visible(False)
        self.announcement_status_row = announcement_status
        announcements_state = group("")
        announcements_state.add(announcement_status)
        announcements_state.set_visible(False)
        self.announcement_status_group = announcements_state
        announcements_box.append(announcements_state)
        self.announcements_group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.announcements_group.add_css_class("boxed-list")
        announcements_box.append(self.announcements_group)
        self.announcement_rows = []
        self.stack.add_named(announcements_page, N_("Announcements"))

    def _start_announcements_load(self):
        """Read announcements at startup without blocking page navigation."""

        def fetch():
            try:
                path = announcements.source_path(self.config_path)
                return announcements.load_announcements(path), ""
            except announcements.AnnouncementLoadError as error:
                log.warning("Announcements configuration could not be loaded", exc_info=True)
                return None, str(error)
            except Exception:
                log.warning("Announcements configuration could not be loaded", exc_info=True)
                return None, "The announcements file could not be loaded."

        self.work(fetch, self._show_announcements)

    def _show_announcements(self, result):
        items, error = result
        while child := self.announcements_group.get_first_child():
            self.announcements_group.remove(child)
        self.announcement_rows = []
        self.announcements_available = not bool(error)
        home_row = self.home_category_rows["Announcements"]
        home_row.set_sensitive(self.announcements_available)
        home_row.set_activatable(self.announcements_available)
        home_row.set_subtitle(_('View {bazzite} release and project announcements.').format(bazzite='Bazzite') if self.announcements_available
                              else _("The configuration file for this category is missing or invalid."))
        if error:
            self.announcement_status_group.set_visible(False)
            return
        if not items:
            self.announcement_status_group.set_visible(True)
            self.announcement_status_row.set_title(format_message(N_("No announcements available")))
            self.announcement_status_row.set_subtitle(format_message(""))
            self.announcement_status_row.set_visible(True)
            return
        self.announcement_status_row.set_visible(False)
        self.announcement_status_group.set_visible(False)
        for item in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(item["title"]))
            subtitle = item.get("subtitle", "")
            if subtitle:
                row.set_subtitle(GLib.markup_escape_text(subtitle))
            row.set_subtitle_lines(3)
            row.controller_focus_row = True
            row.set_activatable(True)
            row.connect("activated", lambda *_args, target=item["link"]: self._open_community_link(target))
            date_widget = self._announcement_date_widget(item["date"])
            row.add_suffix(date_widget)
            row.announcement_date_widget = date_widget
            self.announcements_group.append(row)
            self.announcement_rows.append(row)

    @staticmethod
    def _announcement_date_widget(value):
        date_parts = announcements.format_date(value, announcements.date.today()).split()
        day = Gtk.Label(label=date_parts[0], valign=Gtk.Align.CENTER)
        day.add_css_class("title-2")
        month = Gtk.Label(label=date_parts[1], valign=Gtk.Align.CENTER)
        month.add_css_class("title-3")
        day_month = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5,
                            halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        day_month.append(day)
        day_month.append(month)
        date_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                           halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        date_box.set_size_request(110, -1)
        date_box.append(day_month)
        if len(date_parts) == 3:
            year = Gtk.Label(label=date_parts[2], halign=Gtk.Align.START)
            year.add_css_class("caption")
            year.add_css_class("dim-label")
            date_box.append(year)
        date_box.add_css_class("announcement-date")
        date_box.day_month_row = day_month
        return date_box

    @staticmethod
    def _community_fallback_icon(link_id):
        return {
            "docs": "help-browser-symbolic",
            "discord": "user-available-symbolic",
            "github": "applications-development-symbolic",
            "announcements": "mail-unread-symbolic",
            "website": "web-browser-symbolic",
            "forums": "user-group-new-symbolic",
            "reddit": "user-available-symbolic",
        }.get(link_id, "web-browser-symbolic")

    def _set_community_favicon(self, link_id, path):
        image = self.community_favicon_images.get(link_id)
        if image is None:
            return
        if path is None:
            image.set_from_icon_name(self._community_fallback_icon(link_id))
            return
        try:
            image.set_from_gicon(Gio.FileIcon.new(Gio.File.new_for_path(str(path))))
            self.community_favicon_paths[link_id] = Path(path)
        except (GLib.Error, OSError, TypeError, ValueError):
            image.set_from_icon_name(self._community_fallback_icon(link_id))

    def _ensure_community_favicons(self):
        if self.community_favicon_fetch_started:
            return
        self.community_favicon_fetch_started = True
        def cached_loaded(paths):
            self.community_favicon_paths.update(paths)
            for link_id, path in paths.items():
                self._set_community_favicon(link_id, path)
            # Publish valid local artwork before network requests begin. Each
            # download remains bounded by favicons.FETCH_TIMEOUT_SECONDS.
            self.work(lambda: favicons.fetch_favicons(VERSION), fetched_loaded)

        def fetched_loaded(paths):
            self.community_favicon_paths.update(paths)
            for link_id, path in paths.items():
                self._set_community_favicon(link_id, path)

        self.work(favicons.cached_favicons, cached_loaded)

    def _open_community_link(self, url):
        if not community.valid_link(url):
            self.toast(_('This community link is not a valid {https} address.').format(https='HTTPS'))
            return
        if self.closing:
            return
        self._launch_community_uri(url)

    def _launch_community_uri(self, url):
        if self.closing:
            return
        self._run_xdg_open(url, N_("Could not open link"))

    def _build_settings(self):
        page, box = content_page("")
        box.remove(box.get_first_child())
        preferences = group(N_("Preferences"))
        self.switches = {}
        self.updating_settings = False
        for key, title, subtitle in (
            ("open_at_login", _("Open app at login"), _("Open the window at login. Background monitoring starts independently.")),
            ("show_indicator", _("Hide notification area icon"), _("Hide the update icon in the notification area.")),
            ("show_logs", _("Show console during update"), _("Show the console on the Updates screen. Intended for advanced users.")),
            ("show_disabled_update_steps", _('Show disabled {uupd} steps').format(uupd='UUPD'), _('Display steps disabled in {uupd}. This does not enable them.').format(uupd='UUPD')),
            ("show_unsupported_rebase_targets", _("Show unsupported rebase targets"),
             _('Include other desktops and the {track} channel. Intended for advanced testing; these targets may cause problems.').format(
                 track=channel_name("unstable"))),
            ("skip_one_shot_confirmation", _("Don’t ask before running tools"), _("Skip confirmation for tools that run without an options dialog.")),
            ("always_show_controller_prompts", _("Always show controller prompts"), _("Keep button prompts visible when no controller is connected.")),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.controller_focus_row = True
            switch = Gtk.Switch(active=(not self.store.preferences()[key] if key == "show_indicator" else self.store.preferences()[key]), valign=Gtk.Align.CENTER)
            switch.controller_exclude_focus = True
            switch.set_tooltip_text(format_message(title))
            switch.connect("notify::active", lambda widget, _, selected=key: self._preference(selected, widget.get_active()))
            row.add_suffix(switch)
            row.set_activatable(True)
            row.connect("activated", lambda _row, control=switch: control.set_active(not control.get_active()))
            preferences.add(row)
            self.switches[key] = switch
            if key == "always_show_controller_prompts":
                self.controller_prompts_row = row
        style_values = ["auto", "xbox", "playstation"]
        style_names = [_("Automatic"), "Xbox", "PlayStation"]
        self.controller_style_row = Adw.ActionRow(title=_("Controller prompt style"), subtitle=_("Choose how controller buttons are drawn; controls keep the same actions."))
        self.controller_style_row.controller_focus_row = True
        self.controller_style_dropdown = Gtk.DropDown(model=Gtk.StringList.new(style_names), valign=Gtk.Align.CENTER)
        self.controller_style_dropdown.controller_exclude_focus = True
        self.controller_style_dropdown.set_size_request(-1, 36)
        current_style = self.store.preferences()["controller_prompt_style"]
        self.controller_style_dropdown.set_selected(style_values.index(current_style))
        self.controller_style_dropdown.set_tooltip_text(_("Controller prompt style"))
        self.controller_style_dropdown.connect("notify::selected", self._controller_style_selected)
        self.controller_style_choice, style_choice = choice_label(style_names, style_values.index(current_style))
        self.controller_style_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.controller_style_controls.add_css_class("controller-style-pill")
        self.controller_style_controls.set_size_request(-1, 36)
        self.controller_style_controls.set_valign(Gtk.Align.CENTER)
        self.controller_style_previous = Gtk.Button.new_from_icon_name("pan-start-symbolic")
        self.controller_style_next = Gtk.Button.new_from_icon_name("pan-end-symbolic")
        for arrow in (self.controller_style_previous, self.controller_style_next):
            arrow.set_focusable(False)
            arrow.add_css_class("flat")
            arrow.add_css_class("controller-style-arrow")
        self.controller_style_previous.connect("clicked", lambda *_: self._cycle_controller_style(-1))
        self.controller_style_next.connect("clicked", lambda *_: self._cycle_controller_style(1))
        self.controller_style_controls.append(self.controller_style_previous)
        self.controller_style_controls.append(style_choice)
        self.controller_style_controls.append(self.controller_style_next)
        self.controller_style_row.add_suffix(self.controller_style_controls)
        self.controller_style_row.add_suffix(self.controller_style_dropdown)
        self.controller_style_controls.set_hexpand(False)
        self.controller_style_controls.set_halign(Gtk.Align.END)
        self.controller_style_dropdown.set_hexpand(False)
        self.controller_style_dropdown.set_halign(Gtk.Align.END)
        self.controller_style_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.controller_style_size_group.add_widget(self.controller_style_controls)
        self.controller_style_size_group.add_widget(self.controller_style_dropdown)
        self.controller_style_row.set_activatable(True)
        self.controller_style_row.connect("activated", self._open_controller_style)
        self.controller_style_dropdown.set_visible(False)
        self._sync_controller_style_mode()
        preferences.add(self.controller_style_row)
        from bazzite_portal.artwork.appearance import THEMES, THEME_NAMES
        theme_names = [_(name) for _theme_id, name in THEME_NAMES]
        self.notification_theme_row = Adw.ActionRow(title=_("Notification icon theme"))
        self.notification_theme_row.controller_focus_row = True
        self.notification_theme_row.set_visible(NOTIFICATION_THEMES_ENABLED)
        self.notification_theme_dropdown = Gtk.DropDown(model=Gtk.StringList.new(theme_names), valign=Gtk.Align.CENTER)
        self.notification_theme_dropdown.controller_exclude_focus = True
        self.notification_theme_dropdown.set_selected(THEMES.index(self.store.preferences()["notification_icon_theme"]))
        self.notification_theme_dropdown.connect("notify::selected", self._notification_theme_selected)
        self.notification_theme_choice, theme_choice = choice_label(theme_names, self.notification_theme_dropdown.get_selected())
        self.notification_theme_controls = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.notification_theme_controls.add_css_class("controller-style-pill")
        for icon_name, delta in (("pan-start-symbolic", -1), ("pan-end-symbolic", 1)):
            arrow = Gtk.Button.new_from_icon_name(icon_name)
            arrow.set_focusable(False)
            arrow.add_css_class("flat")
            arrow.connect("clicked", lambda _button, step=delta: self._cycle_notification_theme(step))
            self.notification_theme_controls.append(arrow)
            if delta == -1:
                self.notification_theme_controls.append(theme_choice)
        self.notification_theme_row.add_suffix(self.notification_theme_controls)
        self.notification_theme_row.add_suffix(self.notification_theme_dropdown)
        self.notification_theme_controls.set_size_request(-1, 36)
        self.notification_theme_dropdown.set_size_request(-1, 36)
        self.notification_theme_controls.set_hexpand(False)
        self.notification_theme_controls.set_halign(Gtk.Align.END)
        self.notification_theme_dropdown.set_hexpand(False)
        self.notification_theme_dropdown.set_halign(Gtk.Align.END)
        self.notification_theme_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.notification_theme_size_group.add_widget(self.notification_theme_controls)
        self.notification_theme_size_group.add_widget(self.notification_theme_dropdown)
        self.notification_theme_row.set_activatable(True)
        self.notification_theme_row.connect("activated", lambda *_: self.notification_theme_dropdown.emit("activate") if not self.controller_navigation else None)
        preferences.add(self.notification_theme_row)
        installed_themes = list_gtk4_themes()
        self.gtk_theme_values = ("system", "Adwaita", *installed_themes)
        gtk_theme_names = [_("System"), "Adwaita", *installed_themes]
        self.gtk_theme_row = Adw.ActionRow(
            title=_("GTK theme"),
            subtitle=_("Use the desktop theme or choose an installed GTK 4 theme."),
        )
        self.gtk_theme_row.controller_focus_row = True
        current_gtk_theme = self.store.preferences()["gtk_theme"]
        if current_gtk_theme not in self.gtk_theme_values:
            current_gtk_theme = "system"
        self.gtk_theme_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new(gtk_theme_names), valign=Gtk.Align.CENTER)
        self.gtk_theme_dropdown.controller_exclude_focus = True
        self.gtk_theme_dropdown.set_selected(self.gtk_theme_values.index(current_gtk_theme))
        self.gtk_theme_dropdown.connect("notify::selected", self._gtk_theme_selected)
        self.gtk_theme_row.add_suffix(self.gtk_theme_dropdown)
        self.gtk_theme_row.set_activatable(True)
        self.gtk_theme_row.connect(
            "activated",
            lambda *_: self.gtk_theme_dropdown.emit("activate") if not self.controller_navigation
            else self._cycle_gtk_theme(1),
        )
        preferences.add(self.gtk_theme_row)
        self.color_scheme_values = ("system", "light", "dark")
        color_scheme_names = (_("System"), _("Light"), _("Dark"))
        self.color_scheme_row = Adw.ActionRow(
            title=_("Color scheme"),
            subtitle=_("Follow the desktop setting or choose a light or dark appearance."),
        )
        self.color_scheme_row.controller_focus_row = True
        self.color_scheme_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new(color_scheme_names), valign=Gtk.Align.CENTER)
        self.color_scheme_dropdown.controller_exclude_focus = True
        self.color_scheme_dropdown.set_selected(
            self.color_scheme_values.index(self.store.preferences()["color_scheme"]))
        self.color_scheme_dropdown.connect("notify::selected", self._color_scheme_selected)
        self.color_scheme_row.add_suffix(self.color_scheme_dropdown)
        self.color_scheme_row.set_activatable(True)
        self.color_scheme_row.connect(
            "activated",
            lambda *_: self.color_scheme_dropdown.emit("activate") if not self.controller_navigation
            else self._cycle_color_scheme(1),
        )
        preferences.add(self.color_scheme_row)
        self._sync_controller_style_mode()
        box.append(preferences)
        steam_group = group("Steam")
        self.add_to_steam_row = Adw.ActionRow(
            title=_('Add {app_name} to {steam}').format(app_name='Bazzite Portal', steam='Steam'),
            subtitle=_('Open this app from your {steam} library, including Gaming Mode.').format(steam='Steam'))
        self.add_to_steam_row.controller_focus_row = True
        self.add_to_steam_row.set_activatable(True)
        self.add_to_steam_row.connect("activated", lambda *_: self._add_to_steam())
        steam_group.add(self.add_to_steam_row)
        box.append(steam_group)
        data = group(N_("Local app data"), str(self.store.root))
        logs_row = Adw.ActionRow(title=_("Open app logs"), subtitle=_("Open the folder containing this app’s private logs."))
        logs_row.controller_focus_row = True
        logs_row.set_activatable(True)
        logs_row.connect("activated", lambda *_: self._open_app_logs_folder())
        data.add(logs_row)
        self.app_logs_row = logs_row
        row = Adw.ActionRow(title=_("Clear local app data"), subtitle=_("Remove pins, preferences, and dismissed reminders. Running jobs are unaffected."))
        row.controller_focus_row = True
        row.set_activatable(True)
        row.connect("activated", lambda *_: self.confirm(N_("Clear local app data?"), N_("Your pins, preferences, and dismissed reminders will be removed. System updates and their logs will not be changed."), N_("Clear data"), self._reset))
        data.add(row)
        box.append(data)
        about_group = group(N_("About"))
        about_row = Adw.ActionRow(title=_('About {app_name}').format(app_name='Bazzite Portal'))
        about_row.controller_focus_row = True
        about_row.set_activatable(True)
        about_row.connect("activated", lambda *_: self.get_application().activate_action("about", None))
        about_group.add(about_row)
        box.append(about_group)
        self.settings_errors = label("\n".join(public_error(error, N_("Could not save or load app preferences. Open Logs for details."), context='App preferences') for error in self.store.errors), translate=False)
        box.append(self.settings_errors)
        self.stack.add_named(page, "Settings")

    def _add_to_steam(self):
        if self.closing:
            return
        if not self.add_to_steam_row.get_sensitive():
            return
        self.add_to_steam_row.set_sensitive(False)
        future = self.executor.submit(steam_shortcut.add_to_steam)

        def finished(result):
            if result.cancelled():
                return
            try:
                artwork_ready = result.result()
                message = (C_('Added to {steam}.', steam='Steam') if artwork_ready else
                           C_('Add to {steam} requested.', steam='Steam'))
            except Exception as error:
                log.warning("Add to Steam failed: %s", error)
                message = public_error(error, C_('Could not add {app_name} to {steam}. Open Logs for details.', app_name='Bazzite Portal', steam='Steam'), context='Add to Steam')

            def present():
                if not self.closing:
                    self.add_to_steam_row.set_sensitive(True)
                    self.toast(message)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(present)

        future.add_done_callback(finished)

    def _open_app_logs_folder(self):
        if self.closing:
            return
        try:
            from bazzite_portal.core.logging import get_log_directory
            directory = get_log_directory()
        except OSError as error:
            self.toast(public_error(error, N_('Could not open the app logs folder.'), context='Open app logs'))
            return
        self._open_report_folder(directory)

    def _preference(self, key, value):
        if self.updating_settings:
            return
        if key == "show_indicator":
            value = not value
        saved = self.store.set_preference(key, value)
        if not saved:
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))
        if key == "show_logs":
            self._set_update_console(value)
        if key == "show_disabled_update_steps":
            self._render_status()
        if key == "notification_icon_theme":
            self._render_home_activity()
        if key in {"gtk_theme", "color_scheme"}:
            preferences = self.store.preferences()
            preferences[key] = value
            self.get_application().apply_appearance(preferences)
        if key in {"always_show_controller_prompts", "controller_prompt_style"}:
            self._refresh_all_prompts()
            set_prompt_visible(self.legend, self._prompts_visible())
        self._reload_agent()
        if saved and key == "show_unsupported_rebase_targets" and value:
            self.confirm(N_("Unsupported rebase targets"),
                         _('Rebasing to unsupported targets, including other desktops and the {track} channel, can and will break your system. The {bazzite} team reserves the right not to provide support in these situations. Please read our documentation for further information.').format(bazzite='Bazzite',
                             track=channel_name("unstable")),
                         N_("OK"), lambda: None, informational=True,
                         documentation_url="https://docs.bazzite.gg/Installing_and_Managing_Software/Updates_Rollbacks_and_Rebasing/rebase_guide/")

    def _controller_style_selected(self, dropdown, _property):
        if self.updating_settings:
            return
        values = ("auto", "xbox", "playstation")
        selected = dropdown.get_selected()
        if selected < len(values):
            self._preference("controller_prompt_style", values[selected])
            self.controller_style_choice.set_text((_("Automatic"), "Xbox", "PlayStation")[selected])

    def _open_controller_style(self, *_):
        if not self.controller_navigation:
            self.controller_style_dropdown.emit("activate")

    def _notification_theme_selected(self, dropdown, _property):
        if self.updating_settings:
            return
        from bazzite_portal.artwork.appearance import THEMES, THEME_NAMES
        selected = dropdown.get_selected()
        if selected < len(THEMES):
            self._preference("notification_icon_theme", THEMES[selected])
            self.notification_theme_choice.set_text(_(THEME_NAMES[selected][1]))

    def _cycle_notification_theme(self, delta):
        from bazzite_portal.artwork.appearance import THEMES
        self.notification_theme_dropdown.set_selected((self.notification_theme_dropdown.get_selected() + delta) % len(THEMES))

    def _gtk_theme_selected(self, dropdown, _property):
        if self.updating_settings:
            return
        selected = dropdown.get_selected()
        if selected < len(self.gtk_theme_values):
            self._preference("gtk_theme", self.gtk_theme_values[selected])

    def _cycle_gtk_theme(self, delta):
        selected = self.gtk_theme_dropdown.get_selected()
        self.gtk_theme_dropdown.set_selected((selected + delta) % len(self.gtk_theme_values))

    def _color_scheme_selected(self, dropdown, _property):
        if self.updating_settings:
            return
        selected = dropdown.get_selected()
        if selected < len(self.color_scheme_values):
            self._preference("color_scheme", self.color_scheme_values[selected])

    def _cycle_color_scheme(self, delta):
        selected = self.color_scheme_dropdown.get_selected()
        self.color_scheme_dropdown.set_selected((selected + delta) % len(self.color_scheme_values))

    def _sync_controller_style_mode(self):
        if not hasattr(self, "controller_style_dropdown"):
            return
        self.controller_style_controls.set_visible(self.controller_navigation)
        self.controller_style_dropdown.set_visible(not self.controller_navigation)
        if hasattr(self, "notification_theme_dropdown"):
            self.notification_theme_controls.set_visible(self.controller_navigation)
            self.notification_theme_dropdown.set_visible(not self.controller_navigation)

    def _cycle_controller_style(self, delta):
        selected = self.controller_style_dropdown.get_selected()
        self.controller_style_dropdown.set_selected((selected + delta) % 3)

    def _reload_agent(self):
        if self.client:
            self.client.call("ReloadPreferences")

    def _reset(self):
        def reset_done(_result=None):
            self.store.errors.clear()
            self.updating_settings = True
            for key, switch in self.switches.items():
                switch.set_active(not self.store.preferences()[key] if key == "show_indicator" else self.store.preferences()[key])
            self.controller_style_dropdown.set_selected(("auto", "xbox", "playstation").index(
                self.store.preferences()["controller_prompt_style"]))
            self.gtk_theme_dropdown.set_selected(
                self.gtk_theme_values.index(self.store.preferences()["gtk_theme"])
                if self.store.preferences()["gtk_theme"] in self.gtk_theme_values else 0)
            self.color_scheme_dropdown.set_selected(
                self.color_scheme_values.index(self.store.preferences()["color_scheme"]))
            from bazzite_portal.artwork.appearance import THEMES, THEME_NAMES
            self.notification_theme_dropdown.set_selected(THEMES.index(self.store.preferences()["notification_icon_theme"]))
            self.notification_theme_choice.set_text(_(THEME_NAMES[self.notification_theme_dropdown.get_selected()][1]))
            self.updating_settings = False
            self.controller_style_choice.set_text((_("Automatic"), "Xbox", "PlayStation")[self.controller_style_dropdown.get_selected()])
            self._refresh_all_prompts()
            set_prompt_visible(self.legend, self._prompts_visible())
            self._pins = self.store.pins()
            self._render_pins()
            self._render_tools()
            self._sync_tool_favorites()
            self._render_home_activity()
            self._set_update_console(self.store.preferences()["show_logs"])
            self.get_application().apply_appearance(self.store.preferences())
            self.settings_errors.set_text("\n".join(public_error(error, N_("Could not save or load app preferences. Open Logs for details."), context='App preferences') for error in self.store.errors))
            self.toast(N_("Local app data cleared."))
        if self.client:
            self.client.call("ResetPreferences", callback=reset_done)
        elif self.store.reset():
            reset_done()
        else:
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))

    def about(self):
        if self.modal is not None:
            self.modal.present()
            return
        restore_target = self.controller_focus_widget or self.get_focus()
        dialog = Adw.AboutWindow(transient_for=self, application_name='Bazzite Portal', application_icon=APP_ID, version=VERSION, developer_name="Xarishark • Zacharias Xenakis", license_type=Gtk.License.GPL_3_0_ONLY, comments=_('Manage {bazzite} updates, tools, and system settings.').format(bazzite='Bazzite'))
        translator_credits = _("translator-credits")
        if translator_credits != "translator-credits":
            dialog.set_translator_credits(translator_credits)
        dialog.set_developers(["Xarishark • Zacharias Xenakis https://github.com/Xarishark"])
        dialog.set_website("https://github.com/Xarishark/bazzite-portal")
        dialog.set_issue_url("https://github.com/Xarishark/bazzite-portal/issues")
        dialog.connect("activate-link", self._about_activate_link)
        dialog.set_modal(True)
        dialog.set_application(self.get_application())
        self._configure_about_controller(dialog)
        self.modal = dialog
        self._refresh_fixed_modal_prompts(getattr(getattr(self.controller, "backend", None), "button_labels", {}))
        self._install_input_mode_controllers(dialog)
        dialog.connect("close-request", lambda *_: self._about_closed(dialog, restore_target))
        dialog.present()
        self._update_controller_legend()

    def _configure_about_controller(self, dialog):
        dialog.controller_about = True
        native_content = dialog.get_content()
        dialog.set_content(None)
        dialog.about_native_content = native_content
        toolbar = Adw.ToolbarView()
        toolbar.set_content(native_content)
        dialog.about_legend = ControllerLegend()
        dialog.about_legend.set_halign(Gtk.Align.CENTER)
        for side in ("start", "end", "top", "bottom"):
            getattr(dialog.about_legend, "set_margin_" + side)(12)
        toolbar.add_bottom_bar(dialog.about_legend)
        dialog.set_content(toolbar)

    def _about_closed(self, dialog, restore_target):
        if self.modal is dialog:
            self._clear_controller_focus_visuals()
            self.modal = None
        self._last_pointer_position.pop(dialog, None)

        def restore_focus():
            if (not self.closing and self.modal is None and restore_target is not None
                    and restore_target.get_mapped()):
                if self.controller_navigation:
                    self._set_controller_focus(restore_target)
                else:
                    restore_target.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(restore_focus)
        self._update_controller_legend()
        return False

    def _about_focus_targets(self):
        def prepare(widget):
            if not widget.get_visible() or not widget.get_mapped():
                return
            if isinstance(widget, Adw.HeaderBar) or widget.has_css_class("app-version"):
                widget.controller_exclude_focus = True
                return
            if isinstance(widget, Adw.ActionRow) and widget.get_activatable():
                widget.controller_focus_row = True
            elif isinstance(widget, Gtk.Label) and widget.get_use_markup() and "<a " in widget.get_label():
                widget.controller_focus_row = True
            child = widget.get_first_child()
            while child is not None:
                prepare(child)
                child = child.get_next_sibling()

        content = self.modal.about_native_content
        prepare(content)
        return controller_focus_targets(content)

    def _about_back(self):
        dialog = self.modal
        if dialog is None or not getattr(dialog, "controller_about", False):
            return

        def pop_page(widget):
            if not widget.get_visible() or not widget.get_mapped():
                return False
            if isinstance(widget, Adw.NavigationView):
                page = widget.get_visible_page()
                if page is not None and pop_page(page):
                    return True
                if widget.pop():
                    return True
                return False
            child = widget.get_first_child()
            while child is not None:
                if pop_page(child):
                    return True
                child = child.get_next_sibling()
            return False

        if not pop_page(dialog.about_native_content):
            dialog.close()

    def _dispatch_about(self, command):
        dialog = self.modal
        if command == "back":
            self._about_back()
            return
        targets = self._about_focus_targets()
        if not targets:
            return
        focus = self.controller_focus_widget
        if focus not in targets:
            focus = controller_focused_target(dialog)
        if command in ("up", "down", "left", "right"):
            step = -1 if command in ("up", "left") else 1
            if focus in targets:
                target = targets[(targets.index(focus) + step) % len(targets)]
            else:
                target = targets[-1] if step < 0 else targets[0]
            self._set_controller_focus(target)
            GLib.timeout_add(16, self._reveal_focus, dialog)
        elif command == "confirm" and focus in targets:
            self._set_controller_focus(focus)
            if isinstance(focus, Adw.ActionRow):
                action_name = focus.get_action_name()
                if action_name:
                    focus.activate_action(action_name, focus.get_action_target_value())
                else:
                    focus.activate()
            elif isinstance(focus, Gtk.Label):
                self._activate_about_label(focus)
            elif isinstance(focus, Gtk.Button):
                focus.emit("clicked")
            else:
                focus.activate()

    def _activate_about_label(self, label):
        uri = label.get_current_uri()
        if not uri:
            # Selectable native license labels can focus their text without an
            # active link. Their Pango markup still contains the destination.
            from xml.etree import ElementTree
            try:
                markup = ElementTree.fromstring("<markup>" + label.get_label() + "</markup>")
            except ElementTree.ParseError:
                return
            uri = next((link.get("href") for link in markup.iter("a") if link.get("href")), None)
        if uri:
            self._about_activate_link(self.modal, uri)

    def _about_activate_link(self, _dialog, uri):
        if self.closing:
            return True
        self._run_xdg_open(uri, N_("Could not open link"))
        return True

    def open_action_id(self, action_id):
        matches = [item for item in self.items if item["action"].get("id") == action_id]
        self.show_page(N_("Tools"))
        if len(matches) == 1:
            self.open_action(matches[0])
        else:
            self.toast(N_("The requested action is missing or ambiguous."))

    def _controller_context(self):
        window = self.modal or self
        return (id(window), self.stack.get_visible_child_name()) if window.is_active() and window.get_visible() else None

    def _controller_has_focus(self):
        # Connection ownership follows the whole Portal window family. Input
        # still goes only to the active current modal/view above.
        application = self.get_application()
        return (not self.closing and application is not None
                and any(window.get_visible() and window.is_active()
                        for window in application.get_windows()))

    def _schedule_controller_focus_sync(self):
        if self.closing or self._controller_focus_sync_source is not None:
            return
        # A parent loses activation before its modal gains it. Coalesce that
        # transition so switching between Portal windows need not reopen SDL.
        self._controller_focus_sync_source = GLib.idle_add(self._sync_controller_focus)

    def _sync_controller_focus(self):
        self._controller_focus_sync_source = None
        if not self.closing and self.controller is not None:
            self.controller.set_active(self._controller_has_focus())
        return GLib.SOURCE_REMOVE

    def _set_controller_focus(self, widget):
        previous = self.controller_focus_widget
        if previous is not None and previous is not widget:
            previous.remove_css_class("bazzite-controller-focus")
            if hasattr(previous, "controller_selection_prompt"):
                set_prompt_visible(previous.controller_selection_prompt, False)
            if hasattr(previous, "favorite_icon"):
                self._set_favorite_emphasis(previous, False)
        self.controller_focus_widget = widget
        self._controller_focus_established = True
        widget.add_css_class("bazzite-controller-immediate")
        widget.add_css_class("bazzite-controller-focus")
        if hasattr(widget, "controller_selection_prompt"):
            set_prompt_visible(widget.controller_selection_prompt, self.controller_navigation and self._prompts_visible())
        if hasattr(widget, "favorite_icon"):
            self._set_favorite_emphasis(widget, True)
        widget.grab_focus()

    def _jump_tool_category(self, step):
        rows = [target for target in controller_focus_targets(self.tools_view)
                if getattr(target, "portal_item", None) is not None]
        if not rows:
            return
        categories = {}
        for row in rows:
            categories.setdefault(row.portal_item["category"], row)
        names = list(categories)
        focus = controller_focused_target(self)
        item = getattr(focus, "portal_item", None)
        if item is None or item["category"] not in categories:
            target = rows[0] if step > 0 else categories[names[-1]]
        else:
            index = names.index(item["category"]) + step
            if not 0 <= index < len(names):
                return
            target = categories[names[index]]
        self._scroll_tool_category(target)

    def _stop_tool_category_scroll(self):
        animation = getattr(self, "_category_scroll_animation", None)
        if animation is not None:
            animation.pause()
            self._category_scroll_animation = None
        restore = getattr(self, "_category_scroll_restore", None)
        if restore is not None:
            restore()
            self._category_scroll_restore = None

    def _scroll_tool_category(self, row):
        self._stop_tool_category_scroll()
        scroll = self.catalog_scroll
        viewport = scroll.get_child()
        content = viewport.get_child() if isinstance(viewport, Gtk.Viewport) else viewport
        category = row.get_ancestor(Adw.PreferencesGroup)
        bounds = (category or row).compute_bounds(content)
        if not bounds[0]:
            self._set_controller_focus(row)
            return
        adjustment = scroll.get_vadjustment()
        destination = max(adjustment.get_lower(), min(bounds[1].get_y() - 12,
                          adjustment.get_upper() - adjustment.get_page_size()))
        # GTK's automatic focus reveal otherwise scrolls the first tool to
        # the bottom before our category-heading animation can begin.
        if isinstance(viewport, Gtk.Viewport):
            previous = viewport.get_scroll_to_focus()
            viewport.set_scroll_to_focus(False)
            self._category_scroll_restore = lambda: viewport.set_scroll_to_focus(previous)
        self._set_controller_focus(row)
        target = Adw.PropertyAnimationTarget.new(adjustment, "value")
        animation = Adw.TimedAnimation.new(scroll, adjustment.get_value(), destination, 160, target)
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._category_scroll_animation = animation
        def finished(*_args):
            if self._category_scroll_animation is animation:
                self._category_scroll_animation = None
                restore = getattr(self, "_category_scroll_restore", None)
                if restore is not None:
                    restore()
                    self._category_scroll_restore = None
        animation.connect("done", finished)
        animation.play()

    def _dispatch(self, command):
        if (command not in ("left", "right") or self.modal is not None
                or self.stack.get_visible_child_name() != N_("Tools") or self.tools_search_active):
            self._stop_tool_category_scroll()
        labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
        self._refresh_context_prompts(labels)
        self._refresh_update_prompts(labels)
        self._refresh_modal_selection_prompts(labels)
        set_prompt_visible(self.legend, self._prompts_visible())
        window = self.modal or self
        if getattr(self.modal, "controller_about", False):
            self._dispatch_about(command)
            return
        wizard = getattr(self.modal, "image_wizard", None)
        if wizard is not None:
            if wizard.dialog.selection_loading:
                return
            if command == "back":
                wizard.back()
                return
            if command == "confirm":
                focus = controller_focused_target(self.modal)
                if isinstance(focus, Gtk.Button) and focus.get_sensitive():
                    focus.emit("clicked")
                return
        if self.modal is not None and hasattr(self.modal, "fixed_controller_actions"):
            if getattr(self.modal, "image_selection", False) and command in {"confirm", "left", "right"}:
                focus = controller_focused_target(self.modal)
                if isinstance(focus, Gtk.DropDown) and focus.get_sensitive():
                    count = focus.get_model().get_n_items()
                    if count:
                        step = -1 if command == "left" else 1
                        focus.set_selected((focus.get_selected() + step) % count)
                return
            if (command == "confirm" and getattr(self.modal, "layer_selection", False)):
                focus = controller_focused_target(self.modal)
                if isinstance(focus, Gtk.CheckButton):
                    focus.set_active(not focus.get_active())
                return
            control = self.modal.fixed_controller_actions.get(command)
            if command == "back" and control is None and getattr(self.modal, "layer_selection", False):
                self.modal.close()
                return
            if control is not None and control.get_sensitive():
                control.emit("clicked")
            if not ((getattr(self.modal, "layer_selection", False) or getattr(self.modal, "image_selection", False))
                    and command in ("up", "down", "left", "right")):
                return
        if self.modal is not None and hasattr(self.modal, "report_review_scroll"):
            if command in ("up", "down"):
                adjustment = self.modal.report_review_scroll.get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command == "up" else 48))
                return
        if self.modal is not None and hasattr(self.modal, "maintenance_scroll"):
            if command in ("up", "down", "left", "right"):
                adjustment = self.modal.maintenance_scroll.get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command in ("up", "left") else 48))
                return
            if command in ("confirm", "focus_search", "context_action", "left", "right"):
                return
        if (self.modal is not None and hasattr(self.modal, "confirm_yes_button")
                and command in ("up", "down", "left", "right")):
            if hasattr(self.modal, "package_scroll") and command in ("up", "down"):
                adjustment = self.modal.package_scroll.get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command == "up" else 48))
            return
        if (self.modal is None and self.stack.get_visible_child_name() == N_("Tools")
                and command in ("left", "right")):
            if not self.tools_search_active:
                self.controller_navigation = True
                self._sync_controller_style_mode()
                self._jump_tool_category(-1 if command == "left" else 1)
                self._update_controller_legend(labels)
            return
        if command in ("confirm", "up", "down", "left", "right", "context_action"):
            self.controller_navigation = True
            self._sync_controller_style_mode()
        if self.modal is None and self.stack.get_visible_child_name() == N_("System"):
            if command in ("up", "down"):
                adjustment = self.stack.get_visible_child().get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command == "up" else 48))
                self._update_controller_legend(labels)
                return
            if command in ("confirm", "left", "right"):
                return
        if self.modal is None and self.stack.get_visible_child_name() == N_("Updates"):
            self.controller_navigation = True
            self._clear_controller_focus_visuals()
            if command in ("up", "down", "left", "right"):
                if self.update_console_revealer.get_reveal_child():
                    adjustment = self.log_scroll.get_vadjustment()
                    adjustment.set_value(adjustment.get_value() + (-48 if command in ("up", "left") else 48))
            elif command == "confirm":
                self._primary_update_action()
            elif command == "focus_search":
                self._show_release_notes()
            elif command == "back":
                self.show_page(N_("Home"))
            self._update_controller_legend(labels)
            return
        focus_now = controller_focused_target(self.modal or self)
        if (not self.modal and self.stack.get_visible_child_name() == "Settings"
                and focus_now is self.notification_theme_row and command in ("left", "right")):
            self._cycle_notification_theme(-1 if command == "left" else 1)
            self._set_controller_focus(self.notification_theme_row)
            self._update_controller_legend(labels)
            return
        if (not self.modal and self.stack.get_visible_child_name() == "Settings"
                and focus_now is self.gtk_theme_row and command in ("left", "right")):
            self._cycle_gtk_theme(-1 if command == "left" else 1)
            self._set_controller_focus(self.gtk_theme_row)
            self._update_controller_legend(labels)
            return
        if (not self.modal and self.stack.get_visible_child_name() == "Settings"
                and focus_now is self.color_scheme_row and command in ("left", "right")):
            self._cycle_color_scheme(-1 if command == "left" else 1)
            self._set_controller_focus(self.color_scheme_row)
            self._update_controller_legend(labels)
            return
        if (self.stack.get_visible_child_name() == "Settings" and focus_now is self.controller_style_row
                and command in ("left", "right")):
            self._cycle_controller_style(-1 if command == "left" else 1)
            self._set_controller_focus(self.controller_style_row)
            self._update_controller_legend(labels)
            return
        if command == "confirm":
            if self.modal is not None and hasattr(self.modal, "confirm_yes_button"):
                self.modal.confirm_yes_button.emit("clicked")
                self._update_controller_legend(labels)
                return
            focus = controller_focused_target(window)
            if focus is not None and not focus.get_sensitive():
                return
            if (not self.modal and self.stack.get_visible_child_name() == N_("Tools")
                    and self.tools_search_active and focus is self.search):
                self._set_controller_focus(self.search)
                self._open_search_keyboard()
            elif isinstance(focus, Gtk.Switch):
                self._set_controller_focus(focus)
                focus.set_active(not focus.get_active())
            elif isinstance(focus, Gtk.ListBoxRow) and not isinstance(focus, Adw.ActionRow):
                self._set_controller_focus(focus)
                parent = focus.get_parent()
                if isinstance(parent, Gtk.ListBox):
                    parent.select_row(focus)
            elif isinstance(focus, Gtk.Button):
                self._set_controller_focus(focus)
                focus.emit("clicked")
            elif isinstance(focus, Adw.ActionRow) and getattr(focus, "controller_focus_row", False):
                self._set_controller_focus(focus)
                focus.emit("activated")
            elif focus:
                self._set_controller_focus(focus)
                focus.activate()
        elif command in ("up", "down", "left", "right"):
            targets = controller_focus_targets(window)
            if targets:
                focus = controller_focused_target(window)
                if not self._controller_focus_established:
                    target = targets[-1] if command in ("up", "left") else targets[0]
                    self._set_controller_focus(target)
                    GLib.timeout_add(16, self._reveal_focus, window)
                    self._update_controller_legend(labels)
                    return
                try:
                    index = next(index for index, target in enumerate(targets) if target is focus)
                except StopIteration:
                    target = targets[-1] if command in ("up", "left") else targets[0]
                else:
                    step = -1 if command in ("up", "left") else 1
                    if window is self:
                        target = targets[max(0, min(len(targets) - 1, index + step))]
                    else:
                        target = targets[(index + step) % len(targets)]
                self._set_controller_focus(target)
            # Let GTK apply focus and row allocation before measuring bounds.
            # A short one-shot deferral keeps held navigation and scroll in step.
            GLib.timeout_add(16, self._reveal_focus, window)
        elif command == "back":
            if self.modal:
                self.modal.close()
            elif self.stack.get_visible_child_name() == N_("Home"):
                return
            elif self.stack.get_visible_child_name() == N_("Tools") and self.tools_search_active:
                self.close_tools_search()
            else:
                self.show_page(N_("Home"))
        elif command == "context_action" and not self.modal:
            page = self.stack.get_visible_child_name()
            if page == N_("Home"):
                self.controller_navigation = True
                self.show_page("Settings")
            elif page == N_("Tools"):
                focus = controller_focused_target(self)
                item = getattr(focus, "portal_item", None)
                if item:
                    self._toggle_favorite(item, focus)
        elif command == "focus_search" and not self.modal:
            page = self.stack.get_visible_child_name()
            if page == N_("Home"):
                self.show_page(N_("Updates"))
            elif page in {N_("System"), "Management"}:
                self._refresh_system()
            elif page == N_("Tools"):
                self.open_tools_search()
                self._open_search_keyboard()
        self._update_controller_legend(labels)

    def _update_controller_legend(self, labels=None):
        if not hasattr(self, "legend"):
            return
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        text = _("Move")
        log_open = (self.modal is None and self.stack.get_visible_child_name() == N_("Updates") and
                    self.update_console_revealer.get_reveal_child())
        scrolling = log_open or (self.modal is not None and hasattr(self.modal, "maintenance_scroll"))
        if scrolling:
            text = _("Scroll")
        if (self._prompts_visible() and not self.modal and
                self.stack.get_visible_child_name() == N_("Tools") and not self.tools_search_active):
            target = controller_focused_target(self)
            item = getattr(target, "portal_item", None)
            favorite = bool(item and portal.reference(item) in self._pins)
            action = _("Unpin") if favorite else _("Pin")
            has_favorite_action = True
        else:
            has_favorite_action = False
        information_open = self.stack.get_visible_child_name() == N_("System") and not self.modal
        if information_open:
            text = _("Scroll")
            scrolling = True
        prompts = ([] if log_open or information_open else [("confirm", _("Select"), labels)])
        prompts.append(("back", _("Back"), labels))
        if has_favorite_action:
            action = _("Unpin") if favorite else _("Pin")
            prompts.append(("context_action", action, labels))
        if self.stack.get_visible_child_name() == N_("Updates") and not self.modal:
            text = _("D-pad Scroll") if log_open else ""
            scrolling = bool(log_open)
            prompts = [("back", _("Home"), labels)]
        elif self.modal is not None and hasattr(self.modal, "maintenance_scroll"):
            text = _("D-pad Scroll")
            scrolling = True
            prompts = [("back", _("Back"), labels)]
        if self.modal is not None and hasattr(self.modal, "fixed_controller_actions"):
            text = ""
            prompts = [(command, control.get_label(), labels)
                       for command, control in self.modal.fixed_controller_actions.items()]
            if getattr(self.modal, "layer_selection", False):
                text = _("D-pad Move")
                scrolling = False
                prompts = [("confirm", _("Select"), labels), ("back", _("Back"), labels),
                           ("context_action", _("Review selected"), labels)]
            elif getattr(self.modal, "image_selection", False):
                prompts.insert(0, ("back", _("Back"), labels))
        if self.modal is not None and any(
                hasattr(target, "controller_selection_prompt")
                for target in controller_focus_targets(self.modal)):
            prompts = [prompt for prompt in prompts if prompt[0] != "confirm"]
        self.legend.set_controller_content(text, prompts, self._prompt_style(), scrolling=scrolling)

    @staticmethod
    def _reveal_focus(window):
        stack = getattr(window, "stack", None)
        if stack is not None and stack.get_visible_child_name() == N_("Home"):
            page = stack.get_visible_child()
            targets = controller_focus_targets(page)
            if targets and controller_focused_target(window) is targets[0]:
                adjustment = page.get_vadjustment()
                adjustment.set_value(adjustment.get_lower())
                return
        focus = controller_focused_target(window)
        if not focus:
            return
        parent = focus.get_parent()
        while parent:
            if isinstance(parent, Gtk.ScrolledWindow):
                content = parent.get_child()
                if isinstance(content, Gtk.Viewport):
                    content = content.get_child()
                bounds = focus.compute_bounds(content)
                if bounds[0]:
                    rect = bounds[1]
                    adjustment = parent.get_vadjustment()
                    top, bottom = rect.get_y(), rect.get_y() + rect.get_height()
                    lower = adjustment.get_lower()
                    value = adjustment.get_value()
                    size = adjustment.get_page_size()
                    margin = min(12.0, size / 12.0)
                    target_value = value
                    if top < value + margin:
                        target_value = top - margin
                    elif bottom > value + size - margin:
                        target_value = bottom - size + margin
                    upper = adjustment.get_upper() - size
                    adjustment.set_value(max(lower, min(target_value, upper)))
                break
            parent = parent.get_parent()

    def _close(self, *_):
        self._stop_tool_category_scroll()
        application = self.get_application()
        if application is not None and application.window is self:
            application.window = None
        self.closing = True
        if self._controller_focus_sync_source is not None:
            GLib.source_remove(self._controller_focus_sync_source)
            self._controller_focus_sync_source = None
        self.home_update_artwork.close()
        self._last_pointer_position.clear()
        if self._native_focus_timeout is not None:
            GLib.source_remove(self._native_focus_timeout)
            self._native_focus_timeout = None
        if self.controller:
            self.controller.close()
        if self.client:
            self.client.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.portal_status_executor.shutdown(wait=False, cancel_futures=True)
        return False


class Application(Adw.Application):
    def __init__(self, config_path=portal.DEFAULT_CONFIG, updates=False, action_id=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.config_path, self.initial_updates = config_path, updates
        self.window = None
        self.store = Store()
        self.system_gtk_theme = "Adwaita"
        self.add_main_option("updates", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, N_("Open Updates"), None)
        self.add_main_option("action-id", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING, N_("Open a Portal tool"), "ID")

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        settings = Gtk.Settings.get_default()
        if settings is not None:
            self.system_gtk_theme = settings.get_property("gtk-theme-name") or "Adwaita"
        self.apply_appearance()
        for name, callback in (("settings", lambda: self.activate_page("Settings")), ("updates", lambda: self.activate_page(N_("Updates"))), ("about", lambda: self.window.about())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _, __, selected=callback: selected())
            self.add_action(action)
        self.set_accels_for_action("app.settings", ["<Primary>comma"])

    def apply_appearance(self, preferences=None):
        preferences = preferences or self.store.preferences()
        settings = Gtk.Settings.get_default()
        if settings is not None:
            theme_name = preferences.get("gtk_theme", "system")
            if theme_name != "system" and theme_name not in {"Adwaita", *list_gtk4_themes()}:
                theme_name = "system"
            settings.set_property(
                "gtk-theme-name",
                self.system_gtk_theme if theme_name == "system" else theme_name,
            )

        scheme = preferences.get("color_scheme", "system")
        color_schemes = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        default_scheme = Adw.ColorScheme.FORCE_DARK if gaming_mode() else Adw.ColorScheme.DEFAULT
        self.get_style_manager().set_color_scheme(color_schemes.get(scheme, default_scheme))

    def do_activate(self):
        if not self.window:
            self.window = Window(self, self.config_path)
            if gaming_mode():
                self.window.fullscreen()
            self.window.connect("destroy", self._window_destroyed)
            window = self.window
            window.home_update_artwork.when_ready(window._refresh_all_prompts)
            window.home_update_artwork.when_ready(
                lambda: window.present() if not window.closing and not window.get_mapped() else None)
        else:
            window = self.window
            window.home_update_artwork.when_ready(lambda: window.present() if not window.closing else None)
        if self.initial_updates:
            self.window.show_page(N_("Updates"))
            self.initial_updates = False

    def _window_destroyed(self, window):
        if self.window is window:
            self.window = None

    def activate_page(self, name):
        self.activate()
        self.window.show_page(name)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        self.activate()
        if options.contains("updates"):
            self.window.show_page(N_("Updates"))
        action_id = options.lookup_value("action-id", GLib.VariantType.new("s"))
        if action_id:
            self.window.open_action_id(action_id.get_string())
        return 0
