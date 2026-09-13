"""Opt-in presence for the OA build of ComfyUI.

The OA build opts in by starting ComfyUI with its fixed OA user directory.
Importing this custom node never writes application state or starts ComfyUI.
Presence claims that OA's Comfy integration loaded; it does not probe HTTP.
"""

import atexit
import logging
import os
from pathlib import Path
import threading

try:
    from . import oa_route
except ImportError:  # a test loaded this file outside its package: no relay
    oa_route = None

LOG = logging.getLogger("abstraction")
APPLICATION = "comfyui"
INTERFACE = "oa-integration-loaded"
PROTOCOL = "oa-comfyui"
CONTRACT = "openabstractions.comfyui/integration-ready@1"
LEASE_MS = 60_000
RENEW_SECONDS = 20
CALL_TIMEOUT_SECONDS = 2.0


def _configured_user_directory(user_directory=None, local_app_data=None):
    """Return the exact OA-owned user directory when this install is opted in."""
    if user_directory is None:
        try:
            from comfy.cli_args import args
            user_directory = getattr(args, "user_directory", None)
        except Exception:
            return None
    local_app_data = local_app_data or os.environ.get("LOCALAPPDATA")
    if not local_app_data or not user_directory:
        return None
    expected = Path(local_app_data) / "OpenAbstractions" / "ComfyUI" / "user"
    actual = Path(user_directory)
    if not actual.is_absolute():
        return None
    if os.path.normcase(os.path.abspath(str(actual))) != os.path.normcase(os.path.abspath(str(expected))):
        return None
    return actual


def _facade_applications():
    from abstraction.facade import Scope
    from abstraction.facade.client import Machine
    return Machine(timeout=CALL_TIMEOUT_SECONDS, library=oa_route and oa_route.library()).resolve_applications(
        scope=Scope.LOCAL)


def _presence(instance):
    from abstraction.facade import ApplicationInterface, ApplicationPresence
    return ApplicationPresence(
        application=APPLICATION,
        instance=instance,
        interfaces=[ApplicationInterface(name=INTERFACE, protocol=PROTOCOL, contract=CONTRACT)],
        contexts=[],
        lease_ms=LEASE_MS,
    )


def _outcome(change):
    value = getattr(change, "outcome", None)
    return getattr(value, "value", value)


class PresenceLease:
    """Renew one caller-bound instance without delaying ComfyUI startup."""

    def __init__(self, applications_factory=_facade_applications, presence_factory=_presence,
                 interval=RENEW_SECONDS, stop_event=None, logger=LOG):
        self._applications_factory = applications_factory
        self._presence_factory = presence_factory
        self._interval = interval
        self._stop = stop_event or threading.Event()
        self._logger = logger
        self._instance = ""

    @property
    def instance(self):
        return self._instance

    def _announce(self):
        change = self._applications_factory().announce(self._presence_factory(self._instance))
        outcome = _outcome(change)
        if outcome == "applied" and getattr(change, "instance", ""):
            self._instance = change.instance
        elif outcome == "stale":
            self._instance = ""
        elif outcome not in ("applied", None):
            self._logger.info("[abstraction] ComfyUI presence is unavailable (%s)", outcome)

    def _withdraw(self):
        if not self._instance:
            return
        instance, self._instance = self._instance, ""
        try:
            change = self._applications_factory().withdraw(APPLICATION, instance)
            if _outcome(change) not in ("applied", "unknown", "stale", None):
                self._logger.info("[abstraction] ComfyUI presence withdrawal was refused (%s)", _outcome(change))
        except Exception as error:
            self._logger.debug("[abstraction] ComfyUI presence will expire after shutdown: %s", error)

    def run(self):
        try:
            while not self._stop.is_set():
                try:
                    self._announce()
                except Exception as error:
                    # Presence is optional. Missing OA runtime or rights never
                    # block ComfyUI or alter its normal startup path.
                    self._logger.info("[abstraction] ComfyUI presence unavailable: %s", error)
                if self._stop.wait(self._interval):
                    break
        finally:
            self._withdraw()

    def stop(self):
        self._stop.set()


_lock = threading.Lock()
_lease = None
_thread = None


def start_if_configured(user_directory=None, local_app_data=None, applications_factory=_facade_applications,
                        presence_factory=_presence, interval=RENEW_SECONDS):
    """Start best-effort presence only for the OA build's fixed user directory."""
    global _lease, _thread
    try:
        directory = _configured_user_directory(user_directory, local_app_data)
        if directory is None:
            return False
        with _lock:
            if _lease is not None:
                return True
            lease = PresenceLease(applications_factory, presence_factory, interval)
            thread = threading.Thread(target=lease.run, name="oa-comfyui-presence", daemon=True)
            _lease, _thread = lease, thread
            thread.start()
            atexit.register(stop)
        return True
    except Exception as error:
        LOG.info("[abstraction] ComfyUI OA presence was not started: %s", error)
        return False


def stop():
    """Request withdrawal during orderly interpreter shutdown."""
    global _lease, _thread
    with _lock:
        lease = _lease
        thread = _thread
        _lease = None
        _thread = None
    if lease is not None:
        lease.stop()
    if thread is not None and thread is not threading.current_thread():
        # Shutdown may wait briefly for the bounded public IPC call and best-
        # effort withdrawal. A dead app's lease still expires on its own.
        thread.join(timeout=CALL_TIMEOUT_SECONDS * 2 + 0.5)
