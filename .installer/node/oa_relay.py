"""This ComfyUI's OpenAbstractions calls, made by its helper program.

library() is the IPC library ComfyUI's OA clients hand to Machine and
Jobs.restore_installed in place of their own: every native call OA's shared
transport makes (selecting the installed runtime, opening a connection,
writing and reading a frame, a cancellation signal) goes to the helper,
oa_helper.py, over its standard input and output, and the helper makes it.
The helper runs from this extension's interpreter copy (oa_interpreter.py,
which verifies the prebuilt sealed Desktop helper without writing),
so OpenAbstractions knows the connections by a program inside this
extension's folder (VISION 2026-10-07). The frames are OA's own, unchanged.

The extension registers this module as sys.modules["openabstractions_relay"],
where the OA nodes (adopter-comfyui, oa_route.py) look it up. The helper starts
at the first call and is started again after it stops; a call it was making
then fails with IO_ERROR. It exits when ComfyUI exits.

library() is None off Windows: the helper runs on Windows in this release
(oa_interpreter.UNSUPPORTED).

Standard library only on ComfyUI's side, besides abstraction.ipc. The helper
imports OA's own abstraction.ipc from oa_client/, which the extension
carries, and loads the native library the installed OpenAbstractions
runtime records (installed_library); ComfyUI's venv needs no OA package.
"""

import base64
import binascii
import ctypes
import itertools
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time

from . import oa_connect, oa_interpreter

LOG = logging.getLogger("openabstractions")
MODULE = "openabstractions_relay"
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER_SCRIPT = os.path.join(HERE, "oa_helper.py")
# The ComfyUI connector the extension MSI places beside this file
# (adopters/comfyui/connector): the program OpenAbstractions knows this
# ComfyUI by. Its native-relay operation makes the calls when it is present.
CONNECTOR = os.path.join(HERE, "comfyui-connector.exe")
# OA's own Python IPC client, which build_extension.py places in the payload
# (adopters/comfyui/extension.json, "include").
CLIENT_FOLDER = os.path.join(HERE, "oa_client")
LIBRARY_NAME = "abstraction_ipc.dll"
MAX_LINE = 4 * 1024 * 1024
MAX_DATA = 2 * 1024 * 1024 + 4
START_SECONDS = 15.0
# How long past a call's own deadline the relay waits for the helper's answer.
GRACE_SECONDS = 5.0
SHORT_SECONDS = 5.0
# The native status codes (abstraction.ipc).
OK, TIMEOUT, DISCONNECTED, IO_ERROR, INVALID_ARGUMENT = 0, 1, 2, 3, 4
# The variables the helper needs to find this account and its runtime; the
# rest of ComfyUI's environment stays here.
HELPER_ENVIRONMENT = frozenset({
    "ABSTRACTION_RUNTIME_ENDPOINT", "APPDATA", "COMSPEC", "LOCALAPPDATA", "PATH", "PATHEXT", "PROGRAMDATA",
    "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR",
})


class Lost(Exception):
    """The helper stopped, or broke the line protocol, before it answered."""


class Channel:
    """One running helper: requests go to its input, a thread reads its answers."""

    def __init__(self, process):
        self.process = process
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.waiting = {}
        self.alive = True
        self.hello = threading.Event()
        self.greeting = None
        threading.Thread(target=self._read, name="openabstractions-helper-reader", daemon=True).start()
        if process.stderr is not None:
            threading.Thread(target=self._log, name="openabstractions-helper-log", daemon=True).start()

    def _log(self):
        try:
            for line in iter(lambda: self.process.stderr.readline(4096), b""):
                LOG.warning("[openabstractions] helper: %s", line.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass
        finally:
            self.process.stderr.close()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    LOG.warning("[openabstractions] the helper wrote a line over %d bytes; it is stopped", MAX_LINE)
                    break
                try:
                    reply = json.loads(line)
                except ValueError:
                    LOG.warning("[openabstractions] the helper wrote a line that is not JSON; it is stopped")
                    break
                if not isinstance(reply, dict):
                    break
                if "hello" in reply:
                    self.greeting = reply
                    self.hello.set()
                    continue
                with self.lock:
                    slot = self.waiting.pop(reply.get("id"), None) if type(reply.get("id")) is int else None
                if slot is not None:
                    slot[1] = reply
                    slot[0].set()
                elif reply.get("id") is None:
                    LOG.warning("[openabstractions] the helper refused a line: %s", reply.get("message"))
        except (OSError, ValueError):
            pass
        finally:
            self.stop()
            self.process.stdout.close()

    def stop(self):
        with self.lock:
            self.alive = False
            waiting, self.waiting = list(self.waiting.values()), {}
        self.hello.set()
        for slot in waiting:
            slot[0].set()
        try:
            self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def call(self, op, seconds, **fields):
        """The helper's answer to one request, or Lost."""
        event = threading.Event()
        slot = [event, None]
        with self.lock:
            if not self.alive:
                raise Lost("the helper stopped")
            number = next(self.ids)
            self.waiting[number] = slot
        line = json.dumps(dict(fields, id=number, op=op), separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) > MAX_LINE:
            with self.lock:
                self.waiting.pop(number, None)
            raise ValueError("a relay message is over %d bytes" % MAX_LINE)
        try:
            with self.write_lock:
                self.process.stdin.write(line)
                self.process.stdin.flush()
        except (OSError, ValueError):
            self.stop()
            raise Lost("the helper stopped") from None
        if not event.wait(seconds):
            with self.lock:
                self.waiting.pop(number, None)
            raise TimeoutError(op)
        if slot[1] is None:
            raise Lost("the helper stopped")
        return slot[1]


def _in_runtime_folder(path, folder):
    """Whether path is a full path to a file of its own on a local drive,
    lying inside folder, asked of the file system by identity."""
    if not isinstance(path, str) or not os.path.isabs(path) or oa_interpreter.refused_path(path) is not None:
        return False
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    parent = os.path.dirname(os.path.realpath(path))
    while True:
        try:
            if os.path.samefile(parent, folder):
                return True
        except OSError:
            pass
        above = os.path.dirname(parent)
        if above == parent:
            return False
        parent = above


def installed_library(panel=None):
    """The installed OpenAbstractions runtime's native IPC library: the full
    path it records as ipc_library (installer/abstraction.wxs), else the file
    beside the program it records for its Panel, found as Connect finds the
    Panel (oa_connect.installed_panel). Either must lie in the runtime's own
    install folder, the folder above the Panel's tools folder; anything else
    is refused. Never one from ComfyUI's venv, and the extension carries
    none. Raises LookupError with the sentence ComfyUI shows."""
    panel = panel or oa_connect.installed_panel()
    if panel is None:
        raise LookupError("OpenAbstractions is not installed on this computer; download it from %s."
                          % oa_connect.RELEASE_PAGE)
    runtime = os.path.dirname(os.path.dirname(panel))
    # Import the source this extension carries, independent of ambient OA
    # packages in ComfyUI's venv. Registry selection has one client owner.
    from .oa_client.abstraction.ipc import _native
    candidate = _native.installed_library_path(_accept=lambda path: _in_runtime_folder(path, runtime))
    if candidate is not None:
        return candidate
    beside = os.path.join(os.path.dirname(panel), LIBRARY_NAME)
    if _in_runtime_folder(beside, runtime):
        return beside
    raise LookupError("The installed OpenAbstractions has no %s for applications; install a release that carries it "
                      "from %s." % (LIBRARY_NAME, oa_connect.RELEASE_PAGE))


def _ipc_location():
    """(the folder holding the abstraction.ipc this extension carries, the
    installed runtime's native library)."""
    if not os.path.isfile(os.path.join(CLIENT_FOLDER, "abstraction", "ipc", "__init__.py")):
        raise LookupError("This extension carries no OpenAbstractions IPC client; install it again.")
    return CLIENT_FOLDER, installed_library()


def connector():
    """The connector beside this file, or None."""
    return CONNECTOR if os.path.isfile(CONNECTOR) else None


def start_helper(program=None, location=None, popen=subprocess.Popen, environ=None):
    """Start the helper; return its Channel once it said hello. The
    connector's native-relay is the helper when the connector is present."""
    source = os.environ if environ is None else environ
    native = source.get("ABSTRACTION_COMFYUI_NATIVE_RELAY") or (connector() if program is None else None)
    if native:
        if not os.path.isabs(native) or not os.path.isfile(native):
            raise LookupError("The selected native application relay is unavailable.")
        arguments = [native, "native-relay"]
    else:
        program = program or oa_interpreter.ensure(HERE)
        root, library = location or _ipc_location()
        arguments = [program, "-I", "-B", HELPER_SCRIPT, "--path", root, "--library", library]
    process = popen(arguments,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=HERE,
                    close_fds=True, bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env={name: value for name, value in source.items() if name in HELPER_ENVIRONMENT})
    channel = Channel(process)
    channel.hello.wait(START_SECONDS)
    if not channel.greeting or channel.greeting.get("hello") is not True:
        message = (channel.greeting or {}).get("message") or "it did not start"
        channel.stop()
        raise Lost("the OpenAbstractions helper did not start: %s" % message)
    return channel


class RelayLibrary:
    """abstraction.ipc.Library's native calls, made by the helper.

    FrameTransport, Cancellation and Machine use it as they use Library. Each
    connection and signal the helper holds has a number here; one that belonged
    to a helper that stopped answers IO_ERROR."""

    def __init__(self, start=start_helper):
        self._start = start
        self._lock = threading.Lock()
        self._channel = None
        self._objects = {}
        self._numbers = itertools.count(1)
        self._last_failure = None
        self._replies = {}

    def _features(self):
        try:
            value = (self._current().greeting or {}).get("features", 0)
        except (Lost, TimeoutError) as error:
            from abstraction.ipc import FrameError
            raise FrameError(IO_ERROR, "native relay unavailable: %s" % error) from None
        return value if type(value) is int and 0 <= value <= 0xffffffff else 0

    def _session_call(self, endpoint, length, millis, signal, expected, frame, frame_size,
                      maximum, flags, answer, sent):
        """Forward the shared framed ABI, preserving status and submitted bytes."""
        fields = {"endpoint": bytes(endpoint)[:length].decode("utf-8"), "millis": millis,
                  "max_frame": maximum, "flags": flags,
                  "data": base64.b64encode(ctypes.string_at(frame, frame_size)).decode("ascii")}
        if expected is not None:
            value = expected._obj
            fields["server"] = {"principal_kind": value.principal_kind,
                "principal": value.principal[:value.principal_length].decode("utf-8"),
                "program": value.program[:value.program_length].decode("utf-8")}
        channel = None
        if signal is not None and getattr(signal, "value", signal):
            held = self._held(signal)
            if held is None:
                return IO_ERROR
            channel, fields["signal"] = held[0], held[1]
        try:
            reply = (channel or self._current()).call("session_call", millis / 1000.0 + GRACE_SECONDS, **fields)
        except Lost:
            return IO_ERROR
        except TimeoutError:
            return TIMEOUT
        count = reply.get("sent")
        sent._obj.value = count if type(count) is int and 0 <= count <= 0xffffffffffffffff else 0
        status = self._status(reply)
        if status != OK or flags & 1:
            return status
        try:
            data = base64.b64decode(reply.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            return IO_ERROR
        if len(data) > maximum:
            return IO_ERROR
        buffer = ctypes.create_string_buffer(data)
        with self._lock:
            number = next(self._numbers)
            self._replies[number] = (buffer, len(data))
        answer._obj.value = number
        return OK

    def _reply_data(self, answer, size):
        with self._lock:
            held = self._replies.get(getattr(answer, "value", answer))
            if held is None:
                size._obj.value = 0
                return 0
            size._obj.value = held[1]
            return ctypes.addressof(held[0])

    def _reply_release(self, answer):
        with self._lock:
            self._replies.pop(getattr(answer, "value", answer), None)

    # The helper.

    def _current(self):
        with self._lock:
            if self._channel is not None and self._channel.alive:
                return self._channel
            self._channel = None
            try:
                self._channel = self._start()
            except (Lost, LookupError, ImportError, OSError, oa_interpreter.InterpreterError) as error:
                if str(error) != self._last_failure:
                    LOG.warning("[openabstractions] the helper is unavailable: %s", error)
                    self._last_failure = str(error)
                raise Lost(str(error)) from None
            self._last_failure = None
            return self._channel

    def close(self):
        """Stop the helper (ComfyUI's exit does too)."""
        with self._lock:
            channel, self._channel = self._channel, None
            self._objects.clear()
        if channel is not None:
            channel.stop()

    def _remember(self, channel, remote, seconds=SHORT_SECONDS):
        with self._lock:
            number = next(self._numbers)
            self._objects[number] = (channel, remote, seconds)
        return number

    def _held(self, handle, forget=False):
        number = getattr(handle, "value", handle)
        with self._lock:
            held = self._objects.pop(number, None) if forget else self._objects.get(number)
        if held is None or not held[0].alive:
            return None
        return held

    @staticmethod
    def _status(reply):
        status = reply.get("status")
        return status if type(status) is int and 0 <= status < 2 ** 31 else IO_ERROR

    # Library's own surface.

    def cancellation(self):
        from abstraction.ipc import Cancellation
        return Cancellation(self)

    def select_runtime(self, *, timeout=5.0, deadline=None, cancellation=None):
        from abstraction.ipc import FrameError, ServerExpectation
        if not math.isfinite(timeout) or not 0 <= timeout <= 4294967:
            raise ValueError("invalid timeout")
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("invalid deadline")
        if cancellation is not None and cancellation._library is not self:
            raise ValueError("cancellation belongs to another library")
        seconds = (time.monotonic() + timeout if deadline is None else deadline) - time.monotonic()
        if seconds <= 0:
            raise FrameError(TIMEOUT, "runtime selection deadline expired")
        fields = {"millis": min(4294967295, max(1, math.ceil(seconds * 1000)))}
        try:
            if cancellation is not None:
                held = self._held(cancellation._handle)
                if held is None:
                    raise FrameError(IO_ERROR, "cancellation signal belongs to a helper that stopped")
                channel, fields["signal"] = held[0], held[1]
            else:
                channel = self._current()
            reply = channel.call("select_runtime", seconds + GRACE_SECONDS, **fields)
        except Lost as error:
            raise FrameError(IO_ERROR, "installed runtime selection failed: %s" % error) from None
        except TimeoutError:
            raise FrameError(TIMEOUT, "runtime selection deadline expired") from None
        if self._status(reply) != OK:
            raise FrameError(self._status(reply), reply.get("message") or "installed runtime selection failed")
        server = reply.get("server") or {}
        return ServerExpectation(server.get("principal_kind"), server.get("principal"), server.get("program"))

    def runtime_endpoint(self):
        from abstraction.ipc import FrameError
        try:
            reply = self._current().call("runtime_endpoint", SHORT_SECONDS)
        except (Lost, TimeoutError) as error:
            raise FrameError(IO_ERROR, "runtime bootstrap unavailable: %s" % error) from None
        endpoint = reply.get("endpoint")
        if self._status(reply) != OK or not isinstance(endpoint, str) or not endpoint:
            raise FrameError(self._status(reply) or IO_ERROR, reply.get("message") or "runtime bootstrap failed")
        return endpoint

    # The native ABI, as FrameTransport and Cancellation call it.

    def _cancellation_create(self, out):
        try:
            channel = self._current()
            reply = channel.call("cancellation_create", SHORT_SECONDS)
        except (Lost, TimeoutError):
            return IO_ERROR
        if self._status(reply) != OK or type(reply.get("signal")) is not int:
            return self._status(reply) or IO_ERROR
        out._obj.value = self._remember(channel, reply["signal"])
        return OK

    def _cancellation_signal(self, handle):
        held = self._held(handle)
        if held is not None:
            try:
                held[0].call("cancellation_signal", SHORT_SECONDS, signal=held[1])
            except (Lost, TimeoutError):
                pass

    def _cancellation_release(self, handle):
        held = self._held(handle, forget=True)
        if held is not None:
            try:
                held[0].call("cancellation_release", SHORT_SECONDS, signal=held[1])
            except (Lost, TimeoutError):
                pass

    def _open(self, endpoint, millis, signal, server, out):
        fields = {"endpoint": bytes(endpoint).decode("utf-8"), "millis": millis, "server": server}
        if signal is not None and getattr(signal, "value", signal):
            held = self._held(signal)
            if held is None:  # the signal belonged to a helper that stopped
                return IO_ERROR
            channel, fields["signal"] = held[0], held[1]
        else:
            channel = None
        seconds = millis / 1000.0 + GRACE_SECONDS
        try:
            channel = channel or self._current()
            reply = channel.call("open", seconds, **fields)
        except Lost:
            return IO_ERROR
        except TimeoutError:
            return TIMEOUT
        if self._status(reply) != OK:
            return self._status(reply)
        if type(reply.get("handle")) is not int:
            return IO_ERROR
        out._obj.value = self._remember(channel, reply["handle"], seconds)
        return OK

    def _open_cancelable(self, endpoint, length, millis, signal, out):
        return self._open(bytes(endpoint)[:length], millis, signal, None, out)

    def _open_verified(self, endpoint, length, millis, signal, expected, out):
        value = expected._obj
        server = {"principal_kind": value.principal_kind,
                  "principal": value.principal[:value.principal_length].decode("utf-8"),
                  "program": value.program[:value.program_length].decode("utf-8")}
        return self._open(bytes(endpoint)[:length], millis, signal, server, out)

    def _write(self, handle, buffer, size, moved):
        held = self._held(handle)
        if held is None:
            return IO_ERROR
        if not 0 <= size <= MAX_DATA:
            return INVALID_ARGUMENT
        data = base64.b64encode(ctypes.string_at(ctypes.addressof(buffer), size)).decode("ascii")
        try:
            reply = held[0].call("write", held[2], handle=held[1], data=data)
        except Lost:
            return IO_ERROR
        except TimeoutError:
            return TIMEOUT
        count = reply.get("moved")
        moved._obj.value = count if type(count) is int and 0 <= count <= size else 0
        return self._status(reply)

    def _read(self, handle, buffer, size, moved):
        held = self._held(handle)
        if held is None:
            return IO_ERROR
        try:
            reply = held[0].call("read", held[2], handle=held[1], size=min(size, MAX_DATA))
        except Lost:
            return IO_ERROR
        except TimeoutError:
            return TIMEOUT
        try:
            data = base64.b64decode(reply.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            return IO_ERROR
        if len(data) > size:
            return IO_ERROR
        ctypes.memmove(buffer, data, len(data))
        moved._obj.value = len(data)
        return self._status(reply)

    def _close(self, handle):
        held = self._held(handle, forget=True)
        if held is not None:
            try:
                held[0].call("close", SHORT_SECONDS, handle=held[1])
            except (Lost, TimeoutError):
                pass


_library = None
_library_lock = threading.Lock()


def library(platform=None):
    """The one RelayLibrary of this ComfyUI, or None off Windows."""
    global _library
    if (platform or sys.platform) != "win32":
        return None
    with _library_lock:
        if _library is None:
            _library = RelayLibrary()
        return _library


def program():
    """The program OpenAbstractions knows this ComfyUI's calls by, or None off Windows."""
    if sys.platform != "win32":
        return None
    native = os.environ.get("ABSTRACTION_COMFYUI_NATIVE_RELAY")
    if native:
        return native if os.path.isabs(native) and os.path.isfile(native) else None
    if connector() is not None:
        return connector()
    try:
        return oa_interpreter.ensure(HERE)
    except oa_interpreter.InterpreterError:
        return None


def register():
    """Make this module the one the OA nodes look up. When ComfyUI's own
    environment has no abstraction.ipc, the one this extension carries is
    found after everything else on sys.path, so the relay and the OA
    clients share its classes."""
    sys.modules.setdefault(MODULE, sys.modules[__name__])
    if os.path.isdir(CLIENT_FOLDER) and CLIENT_FOLDER not in sys.path:
        sys.path.append(CLIENT_FOLDER)
