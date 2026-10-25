"""User-session monitor; GTK is intentionally never imported here."""
import json
import logging
from pathlib import Path
import sys
import threading
from gi.repository import Gio, GLib
from bazzite_portal import APP_ID
from bazzite_portal.core.storage import Store
from bazzite_portal.core.first_login import FirstLogin
from bazzite_portal.integration import steam
from bazzite_portal.maintenance import auto_updates
from bazzite_portal.updates.monitor import (
    Monitor,
    maintenance_viewed_results,
    mark_maintenance_viewed,
    clear_maintenance_viewed,
    start_operation,
    start_cancel_pending,
    start_cancel_rollback,
    start_remove_layers,
    start_remove_selected_layers,
    start_clear_download_cache,
    start_channel_switch,
    start_delete_deployment,
    start_image_switch,
    restart,
)
from bazzite_portal.services.tray import Tray
from bazzite_portal.artwork.appearance import artwork_kind
from bazzite_portal.updates.model import indicator
from bazzite_portal.core.i18n import C_, N_, format_message, set_language

log = logging.getLogger(__name__)


def _application_command(*arguments):
    """Run the installed entry point with its private package path available."""
    entry_point = Path(__file__).resolve().parent.parent / "__main__.py"
    return [sys.executable, "-I", str(entry_point), *arguments]

AGENT_ID = APP_ID + ".Agent"
AGENT_PATH = "/io/github/Xarishark/bazzite_portal/Agent"
AGENT_XML = '''<node><interface name="io.github.Xarishark.bazzite_portal.Agent">
<method name="GetSnapshot"><arg type="s" direction="out"/></method>
<method name="AddToSteam"><arg type="s" direction="out"/></method>
<method name="GetAutoUpdateStatus"><arg type="s" direction="out"/></method>
<method name="SetAutoUpdates"><arg type="b" direction="in"/><arg type="s" direction="out"/></method>
<method name="GetDeckyStatus"><arg type="s" direction="out"/></method>
<method name="InstallDecky"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Refresh"/>
<method name="SessionStarted"/>
<method name="MaintenanceViewed"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="x" direction="in"/></method>
<method name="WindowVisible"><arg type="b" direction="in"/></method>
<method name="UpdatesViewed"><arg type="s" direction="in"/></method>
<method name="StartUpdate"/><method name="StartRollback"/><method name="Restart"/>
<method name="CancelPending"><arg type="s" direction="in"/></method>
<method name="CancelRollback"><arg type="s" direction="in"/></method>
<method name="DeleteDeployment"><arg type="s" direction="in"/></method>
<method name="StartImageSwitch"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
<method name="RemoveLayers"><arg type="s" direction="in"/></method>
<method name="RemoveSelectedLayers"><arg type="s" direction="in"/><arg type="as" direction="in"/></method>
<method name="ClearDownloadCache"><arg type="s" direction="in"/></method>
<method name="StartChannelSwitch"><arg type="s" direction="in"/></method>
<method name="ReloadPreferences"/><method name="ResetPreferences"/>
<signal name="Changed"><arg type="s"/></signal>
<signal name="DeckyChanged"><arg type="s"/></signal>
</interface></node>'''


class SteamArtworkMonitor:
    """Retry deferred artwork only on relevant filesystem changes."""

    def __init__(self, backend=steam, gio=Gio, glib=GLib,
                 thread_factory=threading.Thread):
        self.backend, self.gio, self.glib = backend, gio, glib
        self.thread_factory = thread_factory
        self.monitors = {}
        self.closed = False
        self.busy = False
        self.dirty = False
        self.debounce = None
        self.pending_path = backend.pending_artwork_path()
        self._start()

    def _changed(self, _monitor, file, other_file, _event):
        # Ignore grid image writes and unrelated Steam activity, including our
        # own artwork copies. Directory creation can reveal a new Steam profile.
        names = {item.get_basename() for item in (file, other_file) if item}
        directories = {"Steam", "steam", ".steam", ".local", "share", ".var",
                       "app", "com.valvesoftware.Steam", "userdata", "config"}
        relevant = bool(names & {self.pending_path.name, "shortcuts.vdf", "loginusers.vdf"})
        if not relevant:
            relevant = any(name in directories or name.isdecimal() for name in names)
        if not self.closed and relevant:
            self._schedule()

    def _schedule(self):
        if self.closed:
            return
        if self.busy:
            self.dirty = True
        elif self.debounce is None:
            self.debounce = self.glib.timeout_add(200, self._start)

    def _start(self):
        self.debounce = None
        if self.closed or self.busy:
            return False
        self.busy = True
        self.dirty = False

        def work():
            paths = ()
            try:
                self.pending_path.parent.mkdir(parents=True, exist_ok=True)
                # Resolve all filesystem state off the GLib main thread.
                paths = (self.pending_path.parent,)
                if self.backend.apply_pending_artwork():
                    paths += self.backend.artwork_watch_paths()
            except Exception:
                log.exception("Could not apply pending Steam artwork")
                # Keep watching for an external correction after an error.
                try:
                    paths += self.backend.artwork_watch_paths()
                except Exception:
                    log.exception("Could not discover Steam artwork watch paths")
            self.glib.idle_add(self._finished, paths)

        self.thread_factory(target=work, daemon=True,
                            name="steam-artwork").start()
        return False

    def _finished(self, paths):
        self.busy = False
        if self.closed:
            return False
        wanted = {str(path) for path in paths}
        for path in set(self.monitors) - wanted:
            self.monitors.pop(path).cancel()
        added = False
        for path in wanted - set(self.monitors):
            try:
                monitor = self.gio.File.new_for_path(path).monitor_directory(
                    self.gio.FileMonitorFlags.NONE, None)
                monitor.connect("changed", self._changed)
                self.monitors[path] = monitor
                added = True
            except Exception:
                log.exception("Could not watch Steam artwork directory %s", path)
        # Close the read-before-watch race once when a new watch is attached.
        if self.dirty or added:
            self._schedule()
        return False

    def close(self):
        self.closed = True
        if self.debounce is not None:
            self.glib.source_remove(self.debounce)
            self.debounce = None
        for monitor in self.monitors.values():
            monitor.cancel()
        self.monitors.clear()


class Agent:
    def __init__(self, session_start=False):
        self.session_start = bool(session_start)
        self.first_login = FirstLogin()
        self._login_handled = False
        self._first_login_pending = False
        self.store = Store()
        self.loop = GLib.MainLoop()
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.visible_clients = set()
        self.notified = set()
        self._runtime_dismissed = {"run": "", "deployment": "",
                                   "notified_run": "", "result_viewed_run": ""}
        self.maintenance_viewed = maintenance_viewed_results()
        self.busy = False
        self.tray = None
        self.steam_artwork = None
        self._tray_generation = 0
        self._tray_target = None
        self._tray_prepare_lock = threading.Lock()
        self._tray_preparing = False
        self.active_timer = None
        self.registration = None
        self.system_connection = None
        self.system_subscription = None
        self.monitor = Monitor(self._changed, GLib)
        self.name = Gio.bus_own_name_on_connection(self.connection, AGENT_ID, Gio.BusNameOwnerFlags.NONE, self._acquired, self._lost)

    def _acquired(self, *_):
        self.registration = self.connection.register_object(AGENT_PATH, Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0], self._method, None, None)
        self.tray = Tray(self.connection, self.open_updates, self.dismiss)
        self._watch_systemd()
        self.steam_artwork = SteamArtworkMonitor()
        self.connection.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE, self._owner_changed)
        self.store.base.mkdir(parents=True, exist_ok=True)
        self.file_monitor = Gio.File.new_for_path(str(self.store.base)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.file_monitor.connect("changed", lambda *_: self._preferences_changed())
        # Deployment status can change outside UUPD. This is a low-frequency reconciliation,
        # not a loop checking for updates or a replacement scheduler.
        GLib.timeout_add_seconds(300, self._reconcile)
        self.monitor.refresh()
        if self.session_start:
            self._session_started()

    def _session_started(self):
        if self._login_handled:
            return
        self._login_handled = True
        first_launch = not self.first_login.completed()
        if not first_launch and not self.store.preferences()["open_at_login"]:
            return
        self._first_login_pending = first_launch
        if self.visible_clients:
            self._acknowledge_first_login()
        else:
            log.info("Opening app at login (%s)", "first launch" if first_launch else "user preference")
            self.open_updates(home=True)

    def _acknowledge_first_login(self):
        if self._first_login_pending and self.first_login.acknowledge():
            self._first_login_pending = False
            log.info("First login launch completed; startup preference preserved")

    def _lost(self, *_):
        if self.session_start and self.registration is None:
            # An on-demand monitor can own the name before desktop autostart runs.
            # Deliver login intent to it instead of silently losing the window launch.
            try:
                self.connection.call_sync(AGENT_ID, AGENT_PATH, AGENT_ID,
                                          "SessionStarted", None, None,
                                          Gio.DBusCallFlags.NONE, 3000, None)
            except GLib.Error as error:
                log.warning("Could not deliver graphical login to agent: %s", error)
        self._tray_generation += 1
        self._tray_target = None
        if self.tray:
            self.tray.update("", "")
            if hasattr(self.tray, "close"):
                self.tray.close()
        if self.steam_artwork is not None:
            self.steam_artwork.close()
        self.monitor.close()
        self._unwatch_systemd()
        self.loop.quit()

    def _watch_systemd(self):
        # Keep the connection alive: a local-only Gio wrapper drops its signal
        # subscriptions when collected, leaving only the reconciliation timer.
        self._systemd_properties = {}
        self.system_connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.system_subscription = self.system_connection.signal_subscribe(
            "org.freedesktop.systemd1", "org.freedesktop.DBus.Properties", "PropertiesChanged",
            None, None, Gio.DBusSignalFlags.NONE, self._system_changed)
        self.system_connection.call(
            "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "Subscribe", None, None,
            Gio.DBusCallFlags.NONE, 3000, None, self._subscribed)

    def _unwatch_systemd(self):
        connection = getattr(self, "system_connection", None)
        subscription = getattr(self, "system_subscription", None)
        if connection is not None and subscription is not None:
            connection.signal_unsubscribe(subscription)
            try:
                connection.call("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                                "org.freedesktop.systemd1.Manager", "Unsubscribe", None, None,
                                Gio.DBusCallFlags.NONE, 3000, None, None)
            except GLib.Error:
                pass
        self.system_subscription = None
        self.system_connection = None

    def _subscribed(self, bus, result):
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            self.monitor.snapshot.error = f"Could not subscribe to systemd: {error}"

    def _system_changed(self, connection, sender, path, interface, signal, parameters):
        if any(name in path for name in ("uupd", "bazzite_2dportal_2dmaintenance")):
            changed_interface, properties, invalidated = parameters.unpack()
            watched = {"LoadState", "ActiveState", "SubState", "InvocationID",
                       "Result", "ExecMainStatus", "ExecMainCode", "MainPID"}
            values = {key: value for key, value in properties.items() if key in watched}
            cache = getattr(self, "_systemd_properties", None)
            if cache is None:
                cache = self._systemd_properties = {}
            key = (path, changed_interface)
            if key not in cache and len(cache) >= 64:
                cache.pop(next(iter(cache)))
            previous = cache.setdefault(key, {})
            changed = any(key not in previous or previous[key] != value for key, value in values.items())
            previous.update(values)
            # Querying an unloaded unit can re-emit its unchanged properties.
            # Ignore duplicates to avoid a refresh/query/load feedback loop.
            invalid = [key for key in invalidated if key in watched and key in previous]
            for key in invalid:
                previous.pop(key, None)
            if changed or invalid:
                self.monitor.refresh()

    def _owner_changed(self, connection, sender, path, interface, signal, parameters):
        name, old, new = parameters.unpack()
        if not new and name in self.visible_clients:
            self.visible_clients.discard(name)
            self._preferences_changed()

    def _preferences_changed(self):
        preferences = self.store.preferences()
        set_language(preferences["language"])
        if self.tray:
            semantic = indicator(self.monitor.snapshot, preferences, self._runtime_dismissed,
                                 bool(self.visible_clients))
            theme = preferences.get("notification_icon_theme", "spinning")
            if not semantic:
                target = ("", "", False)
            else:
                try:
                    art = artwork_kind(theme, semantic)
                    target = (art, semantic, False)
                except ValueError:
                    # Unknown desktop for the native theme: preserve the saved
                    # choice and keep a generic status icon available.
                    target = (semantic, semantic, True)
            if target == self._tray_target:
                return False
            self._tray_target = target
            self._tray_generation += 1
            generation = self._tray_generation
            kind, semantic, fallback_only = target
            if not kind:
                self.tray.update("", "")
                return False
            if fallback_only:
                self.tray.update(kind, semantic, fallback_only=True)
                return False

            self._prepare_tray()
        return False

    def _prepare_tray(self):
        # Coalesce rapid preference/state changes into one worker. Obsolete
        # results never publish and only the latest request is prepared next.
        if self._tray_preparing or not self.tray or not self._tray_target:
            return
        kind, semantic, fallback_only = self._tray_target
        if not kind or fallback_only:
            return
        self._tray_preparing = True
        generation = self._tray_generation
        tray = self.tray
        def prepare():
            failed = False
            try:
                with self._tray_prepare_lock:
                    tray.prepare_artwork(kind)
            except Exception:
                failed = True
            GLib.idle_add(publish, failed)
        def publish(failed):
            self._tray_preparing = False
            if generation == self._tray_generation and self._tray_target == (kind, semantic, False):
                tray.update(kind, semantic, fallback_only=failed)
            elif self._tray_target:
                self._prepare_tray()
            return False
        threading.Thread(target=prepare, daemon=True).start()

    def _reconcile(self):
        self.monitor.refresh()
        return True

    def _tick(self):
        self.monitor.refresh()
        if (not self.monitor.snapshot.running
                and not any(getattr(self.monitor.snapshot, kind + "_state") == "running"
                            for kind in ("cancel", "layer", "cache", "channel", "image", "delete", "rollback"))
                and not self.busy):
            self.active_timer = None
            return False
        return True

    def _changed(self, snapshot):
        error = snapshot.error or ""
        if error and error != getattr(self, "_logged_monitor_error", ""):
            log.warning("Status monitoring: %s", error[:2048])
        self._logged_monitor_error = error
        if snapshot.run and snapshot.result in {"success", "failed", "unknown"}:
            key = (snapshot.run, snapshot.result)
            if key != getattr(self, "_logged_update_result", None):
                log.log(logging.INFO if snapshot.result == "success" else logging.WARNING,
                        "Update result: %s run=%s", snapshot.result, snapshot.run[:64])
            self._logged_update_result = key
        outcomes = getattr(self, "_logged_outcomes", {})
        for kind in ("cancel", "layer", "cache", "channel", "image", "delete", "rollback"):
            state = getattr(snapshot, kind + "_state", "idle")
            identity = (getattr(snapshot, kind + "_unit", ""),
                        getattr(snapshot, kind + "_invocation", ""),
                        snapshot.maintenance_generations.get(kind, 0), state)
            if identity[0] and state in {"success", "failed", "unknown"} and outcomes.get(kind) != identity:
                log.log(logging.INFO if state == "success" else logging.WARNING,
                        "Operation result: %s %s %s", kind, state,
                        getattr(snapshot, kind + "_error", "")[:2048])
            outcomes[kind] = identity
        self._logged_outcomes = outcomes
        if (snapshot.running or any(getattr(snapshot, kind + "_state") == "running"
                                    for kind in ("cancel", "layer", "cache", "channel", "image", "delete", "rollback"))) and self.active_timer is None:
            interval = 2 if any(getattr(snapshot, kind + "_state") == "running" for kind in ("cancel", "layer", "cache", "channel", "image", "delete", "rollback")) else 10
            self.active_timer = GLib.timeout_add_seconds(interval, self._tick)
        should_notify = bool(snapshot.result == "failed" and snapshot.run
                             and snapshot.run not in self.notified)
        if should_notify:
            self.notified.add(snapshot.run)
            self._runtime_dismissed["notified_run"] = snapshot.run
        self._preferences_changed()
        self._publish_snapshot(snapshot)
        if should_notify:
            if snapshot.failures:
                log.warning("Failed update components: %s", ", ".join(snapshot.failures)[:2048])
            self.notify(C_('{bazzite} update failed', bazzite='Bazzite') if snapshot.operation == "update" else C_('{bazzite} rollback failed', bazzite='Bazzite'), N_("Open Updates to inspect the operation log."))

    def _snapshot_payload(self, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.monitor.snapshot
        return {**snapshot.to_dict(),
                "result_viewed_run": getattr(self, "_runtime_dismissed", {}).get("result_viewed_run", ""),
                "dismissed_status": dict(getattr(self, "_runtime_dismissed", {})),
                "request_busy": bool(getattr(self, "busy", False)),
                "maintenance_viewed": [[kind, *getattr(self, "maintenance_viewed", {})[kind]]
                                        for kind in sorted(getattr(self, "maintenance_viewed", {}))]}

    def _acknowledge_maintenance_viewed(self, kind, unit, invocation, generation):
        """Consume only the exact completed helper result currently in the snapshot."""
        if kind not in {"cancel", "layer", "cache", "channel", "image", "delete", "rollback"}:
            return False
        snapshot = self.monitor.snapshot
        state = getattr(snapshot, kind + "_state", "unknown")
        current = (
            getattr(snapshot, kind + "_unit", ""),
            getattr(snapshot, kind + "_invocation", ""),
            getattr(snapshot, "maintenance_generations", {}).get(kind, 0),
        )
        if (state in {"running", "idle", "unavailable"}
                or not isinstance(generation, int) or generation <= 0
                or current != (unit, invocation, generation)):
            return False
        key = (unit, invocation, generation)
        if self.maintenance_viewed.get(kind) == key:
            return False
        self.maintenance_viewed[kind] = key
        mark_maintenance_viewed(kind, *key)
        self._publish_snapshot(snapshot)
        return True

    def _publish_snapshot(self, snapshot=None):
        payload = json.dumps(self._snapshot_payload(snapshot))
        self.connection.emit_signal(None, AGENT_PATH, AGENT_ID, "Changed", GLib.Variant("(s)", (payload,)))

    def notify(self, title, body):
        def notified(connection, result):
            try:
                response = connection.call_finish(result)
                notification_id = response.unpack()[0]
                self.notification_id = notification_id
            except GLib.Error:
                pass
        if not hasattr(self, "notification_subscription"):
            self.notification_subscription = self.connection.signal_subscribe("org.freedesktop.Notifications", "org.freedesktop.Notifications", "ActionInvoked", "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE, self._notification_action)
        title = format_message(title)
        body = format_message(body)
        self.connection.call("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications", "Notify", GLib.Variant("(susssasa{sv}i)", ("Bazzite Portal", 0, APP_ID, title, GLib.markup_escape_text(body), ["default", format_message(N_("Open Updates"))], {"desktop-entry": GLib.Variant("s", APP_ID)}, -1)), GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 3000, None, notified)

    def _notification_action(self, connection, sender, path, interface, signal, parameters):
        notification_id, action = parameters.unpack()
        if notification_id == getattr(self, "notification_id", None):
            self.open_updates()

    def open_updates(self, home=False):
        try:
            Gio.Subprocess.new(_application_command(
                *([] if home else ["--updates"])), Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            self.monitor.snapshot.error = str(error)

    def dismiss(self):
        snapshot = self.monitor.snapshot
        self._runtime_dismissed.update(
            run=snapshot.run if snapshot.running or snapshot.result == "failed" else "",
            deployment=snapshot.deployment,
        )
        self._preferences_changed()
        self._publish_snapshot(snapshot)

    def _acknowledge_failed_run(self, viewed_run):
        if self.monitor.snapshot.result == "failed":
            self._acknowledge_update_run(viewed_run)

    def _acknowledge_update_run(self, viewed_run):
        snapshot = self.monitor.snapshot
        if (snapshot.result not in {"success", "failed"} or snapshot.running or not snapshot.run
                or viewed_run != snapshot.run):
            return
        if self._runtime_dismissed.get("result_viewed_run") == snapshot.run:
            return
        # Keep the currently displayed result visible, but let later visits
        # start fresh. Restart reminders have their own deployment identity.
        self._runtime_dismissed["result_viewed_run"] = snapshot.run
        if snapshot.result == "failed":
            self._runtime_dismissed["run"] = snapshot.run
            self._preferences_changed()
        self._publish_snapshot(snapshot)

    def _decky_request(self, method, parameters, invocation):
        """On-demand plugin integration, independent of system management state."""
        installing = method == "InstallDecky"
        if installing and getattr(self, "_decky_installing", False):
            invocation.return_dbus_error(AGENT_ID + ".Error", N_("A Decky plugin installation request is already running."))
            return
        if installing:
            self._decky_installing = True
        expected = parameters.unpack()[0] if installing else None

        def work():
            try:
                from bazzite_portal.integration import decky
                result = decky.install(expected) if installing else decky.status()
            except Exception:
                log.exception("Could not %s Decky plugin", "install" if installing else "inspect")
                result = {"can_install": False, "can_update": False,
                          "error": N_("Could not manage the Decky plugin. Open Logs for details.")}
            GLib.idle_add(finish, result)

        def finish(result):
            if installing:
                self._decky_installing = False
            result = {**result, "installing": bool(getattr(self, "_decky_installing", False))}
            payload = GLib.Variant("(s)", (json.dumps(result),))
            invocation.return_value(payload)
            self.connection.emit_signal(None, AGENT_PATH, AGENT_ID, "DeckyChanged", payload)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _add_to_steam_request(self, invocation):
        """Reuse desktop's native shortcut/artwork helper, off the agent main loop."""
        if getattr(self, "_steam_adding", False):
            invocation.return_dbus_error(AGENT_ID + ".Error", "An Add to Steam request is already running.")
            return
        self._steam_adding = True

        def work():
            try:
                result = {"accepted": True, **steam.add_to_steam(),
                          "artwork_targets": steam.bundled_artwork_targets()}
            except Exception:
                log.exception("Could not add Portal to Steam")
                result = {"accepted": False, "error": "Could not add Bazzite Portal to Steam. Open Logs for details."}
            GLib.idle_add(finish, result)

        def finish(result):
            self._steam_adding = False
            invocation.return_value(GLib.Variant("(s)", (json.dumps(result),)))
            return False

        threading.Thread(target=work, daemon=True).start()

    def _auto_update_request(self, method, parameters, invocation):
        setting = method == "SetAutoUpdates"
        if setting and (self.busy or getattr(self, "_auto_updates_pending", False)):
            invocation.return_dbus_error(AGENT_ID + ".Error", N_("Another operation request is pending."))
            return
        if setting:
            self._auto_updates_pending = True
        enabled = parameters.unpack()[0] if setting else None

        def work():
            try:
                current = auto_updates.request(enabled) if setting else auto_updates.status()
                result = {"accepted": True, **current}
            except Exception as error:
                log.exception("Could not %s automatic update scheduling", "change" if setting else "read")
                result = {"accepted": False, "error": str(error) or "Automatic update scheduling is unavailable."}
            GLib.idle_add(finish, result)

        def finish(result):
            if setting:
                self._auto_updates_pending = False
            invocation.return_value(GLib.Variant("(s)", (json.dumps(result),)))
            return False

        threading.Thread(target=work, daemon=True).start()

    def _method(self, connection, sender, path, interface, method, parameters, invocation):
        if method in {"GetAutoUpdateStatus", "SetAutoUpdates"}:
            self._auto_update_request(method, parameters, invocation)
            return
        if method == "AddToSteam":
            self._add_to_steam_request(invocation)
            return
        if method in {"GetDeckyStatus", "InstallDecky"}:
            self._decky_request(method, parameters, invocation)
            return
        if method == "GetSnapshot":
            invocation.return_value(GLib.Variant("(s)", (json.dumps(self._snapshot_payload()),)))
        elif method == "Refresh":
            self.monitor.refresh()
            invocation.return_value(None)
        elif method == "SessionStarted":
            self._session_started()
            invocation.return_value(None)
        elif method == "WindowVisible":
            if parameters.unpack()[0]:
                self.visible_clients.add(sender)
                self._acknowledge_first_login()
            else:
                self.visible_clients.discard(sender)
            self._preferences_changed()
            invocation.return_value(None)
        elif method == "UpdatesViewed":
            self._acknowledge_update_run(parameters.unpack()[0])
            invocation.return_value(None)
        elif method == "MaintenanceViewed":
            self._acknowledge_maintenance_viewed(*parameters.unpack())
            invocation.return_value(None)
        elif method == "ReloadPreferences":
            self._preferences_changed()
            invocation.return_value(None)
        elif method == "ResetPreferences":
            if self.store.reset():
                self._runtime_dismissed = {"run": "", "deployment": "",
                                           "notified_run": "", "result_viewed_run": ""}
                self.notified.clear()
                self.maintenance_viewed.clear()
                clear_maintenance_viewed()
                self._preferences_changed()
                self.monitor.refresh()
                self._publish_snapshot()
                invocation.return_value(None)
            else:
                invocation.return_dbus_error(AGENT_ID + ".Error", self.store.errors[-1])
        elif method in ("StartUpdate", "StartRollback", "CancelRollback", "Restart", "CancelPending", "RemoveLayers", "RemoveSelectedLayers", "ClearDownloadCache", "StartChannelSwitch", "DeleteDeployment", "StartImageSwitch"):
            if self.busy:
                invocation.return_dbus_error(AGENT_ID + ".Error", N_("Another operation request is pending."))
                return
            self.busy = True
            log.info("Operation requested: %s", method)
            # Start polling before the worker invokes systemd. PropertiesChanged
            # is the fast path, while this catches a missed signal and keeps
            # running state/log snapshots live until the request has a result.
            if self.active_timer is None:
                self.active_timer = GLib.timeout_add_seconds(2, self._tick)
            self.monitor.refresh()
            def worker():
                error = None
                try:
                    if method == "CancelRollback":
                        start_cancel_rollback(parameters.unpack()[0])
                    elif method == "CancelPending":
                        start_cancel_pending(parameters.unpack()[0])
                    elif method == "RemoveLayers":
                        start_remove_layers(parameters.unpack()[0])
                    elif method == "RemoveSelectedLayers":
                        token, selected = parameters.unpack()
                        start_remove_selected_layers(token, selected)
                    elif method == "ClearDownloadCache":
                        start_clear_download_cache(parameters.unpack()[0])
                    elif method == "StartChannelSwitch":
                        start_channel_switch(parameters.unpack()[0])
                    elif method == "DeleteDeployment":
                        start_delete_deployment(parameters.unpack()[0])
                    elif method == "StartImageSwitch":
                        start_image_switch(*parameters.unpack())
                    elif method == "Restart":
                        restart()
                    else:
                        start_operation("update" if method == "StartUpdate" else "rollback")
                except Exception as exception:
                    log.exception("Operation request failed: %s", method)
                    error = str(exception) or N_("Operation request failed.")
                GLib.idle_add(finish, error)
            def finish(error):
                self.busy = False
                if error:
                    invocation.return_dbus_error(AGENT_ID + ".Error", error)
                else:
                    log.info("Operation request finished: %s", method)
                    invocation.return_value(None)
                self.monitor.refresh()
                return False
            threading.Thread(target=worker, daemon=True).start()

    def run(self):
        self.loop.run()


class Client:
    READY_TIMEOUT_SECONDS = 15

    def __init__(self, changed, error):
        self.changed, self.error = changed, error
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.closed = False
        self.signal_subscription = self.connection.signal_subscribe(
            AGENT_ID, AGENT_ID, "Changed", AGENT_PATH, None,
            Gio.DBusSignalFlags.NONE, self._signal)
        self.watch = Gio.bus_watch_name_on_connection(self.connection, AGENT_ID, Gio.BusNameWatcherFlags.NONE, self._appeared, self._vanished)
        self.visible = False
        self.ready = False
        self.pending_calls = []

    def _appeared(self, *_):
        if self.closed:
            return
        self.ready = True
        self.call("GetSnapshot", callback=self._snapshot)
        self.set_visible(self.visible)
        pending, self.pending_calls = self.pending_calls, []
        for method, parameters, callback, error_callback, timeout_id in pending:
            GLib.source_remove(timeout_id)
            if not self.closed:
                self._send_call(method, parameters, callback, error_callback)

    def _vanished(self, *_):
        if self.closed:
            return
        self.ready = False
        # Start the installed service; activation through the normal CLI supports source runs.
        try:
            Gio.Subprocess.new(_application_command("--agent"), Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            if not self.closed:
                self.error(str(error))

    def _signal(self, connection, sender, path, interface, signal, parameters):
        if not self.closed:
            self._snapshot(parameters)

    def _snapshot(self, variant):
        if self.closed:
            return
        try:
            self.changed(json.loads(variant.unpack()[0]))
        except (ValueError, TypeError, IndexError):
            if not self.closed:
                self.error("The update monitor returned an invalid snapshot.")

    def call(self, method, parameters=None, callback=None, error_callback=None):
        if self.closed:
            return

        if not getattr(self, "ready", True):
            pending = [method, parameters, callback, error_callback, None]
            self.pending_calls.append(pending)
            pending[4] = GLib.timeout_add_seconds(
                self.READY_TIMEOUT_SECONDS, self._ready_timeout, pending)
            return
        self._send_call(method, parameters, callback, error_callback)

    def _ready_timeout(self, pending):
        if self.closed:
            return False
        try:
            self.pending_calls.remove(pending)
        except ValueError:
            return False
        log.warning("Session agent readiness timed out for %s", pending[0])
        (pending[3] or self.error)(format_message(N_("Could not read update status. Open Logs for details.")))
        return False

    def _send_call(self, method, parameters=None, callback=None, error_callback=None):

        def finished(connection, result):
            try:
                value = connection.call_finish(result)
                if self.closed:
                    return
                if callback:
                    callback(value)
            except GLib.Error as error:
                if not self.closed:
                    remote_name = Gio.DBusError.get_remote_error(error)
                    message = error.message
                    if remote_name:
                        message = message.removeprefix(f"GDBus.Error:{remote_name}: ")
                    (error_callback or self.error)(message)
        # Maintenance requests wait for their systemd unit to finish so the
        # reply can report its real result. Do not let GDBus expire the client
        # request while that unit is still running; Changed snapshots continue
        # to carry live progress independently of this reply.
        timeout = 2_147_483_647 if method in {
            "StartRollback", "CancelRollback", "CancelPending", "RemoveLayers", "RemoveSelectedLayers", "ClearDownloadCache", "StartChannelSwitch", "DeleteDeployment", "StartImageSwitch",
        } else 120000
        self.connection.call(AGENT_ID, AGENT_PATH, AGENT_ID, method, parameters, None, Gio.DBusCallFlags.NONE, timeout, None, finished)

    def set_visible(self, visible):
        if self.closed:
            return
        self.visible = visible
        self.call("WindowVisible", GLib.Variant("(b)", (visible,)))

    def updates_viewed(self, run):
        """Acknowledge the exact completed run shown in Updates in agent memory."""
        self.call("UpdatesViewed", GLib.Variant("(s)", (run,)))

    def close(self):
        if self.closed:
            return
        # Queue the visibility transition before closing callbacks. Any reply
        # arriving after this point is deliberately ignored.
        self.set_visible(False)
        self.closed = True
        self.ready = False
        for _method, _parameters, _callback, _error_callback, timeout_id in self.pending_calls:
            GLib.source_remove(timeout_id)
        self.pending_calls.clear()
        Gio.bus_unwatch_name(self.watch)
        self.connection.signal_unsubscribe(self.signal_subscription)
