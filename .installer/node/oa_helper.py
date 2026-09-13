"""The OpenAbstractions helper of this ComfyUI: the program OpenAbstractions knows it by.

ComfyUI starts it (oa_relay.py) from the interpreter copy oa_interpreter.py
places in helper/, so the connections it opens come from a program image
inside this extension's folder (VISION 2026-10-07). It relays OA's shared
native IPC calls, unchanged, over its standard input and output: ComfyUI's OA
clients encode their frames as ever, and this program opens the connections,
writes and reads the frames and holds the cancellation signals.

Each line is one JSON object of at most MAX_LINE bytes. A request carries
"id" and "op"; the reply carries the same "id", "status" (the native status
code, 0 OK) and the op's results. Frame bytes travel as base64 of at most
MAX_DATA bytes. A line that is too long or not a JSON object is answered with
"id": null and INVALID_ARGUMENT, and the helper reads on. The helper exits
when its standard input ends, which happens when ComfyUI exits.

    hello                              {"version", "pid"}   (written first, unasked)
    select_runtime {millis, signal}    {"server": {"principal_kind", "principal", "program"}}
    runtime_endpoint                   {"endpoint"}
    cancellation_create                {"signal"}
    cancellation_signal {signal}       {}
    cancellation_release {signal}      {}
    open {endpoint, millis, signal, server}  {"handle"}
    write {handle, data}               {"moved"}
    read {handle, size}                {"data"}
    close {handle}                     {}

Usage: python oa_helper.py --path <folder holding abstraction/ipc> --library <abstraction_ipc library>
It imports OA's IPC client from --path, the one ComfyUI's own OA clients use.
"""

import argparse
import base64
import binascii
import ctypes
import json
import os
import sys
import threading

VERSION = 1
MAX_LINE = 4 * 1024 * 1024
MAX_DATA = 2 * 1024 * 1024 + 4
MAX_PENDING = 64
MAX_OBJECTS = 256
# The native status codes (abstraction.ipc).
OK, TIMEOUT, DISCONNECTED, IO_ERROR, INVALID_ARGUMENT = 0, 1, 2, 3, 4
CANCELLED, PROOF_UNAVAILABLE = 7, 9


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _int(request, name, low, high):
    value = request.get(name)
    if type(value) is not int or not low <= value <= high:
        raise Refused(INVALID_ARGUMENT, "%s must be an integer from %d to %d" % (name, low, high))
    return value


def _text(request, name, limit=65536):
    value = request.get(name)
    if not isinstance(value, str) or not value or "\0" in value or len(value) > limit:
        raise Refused(INVALID_ARGUMENT, "%s must be nonempty text without NUL" % name)
    return value


class Relay:
    """The native calls, on handles and signals this helper holds for ComfyUI."""

    def __init__(self, library, ipc):
        self.library, self.ipc = library, ipc
        self.lock = threading.Lock()
        self.handles, self.signals, self.next_id = {}, {}, 1

    def _add(self, table, value):
        with self.lock:
            if len(self.handles) + len(self.signals) >= MAX_OBJECTS:
                return None
            number, self.next_id = self.next_id, self.next_id + 1
            table[number] = value
            return number

    def _get(self, table, request, name, optional=False):
        if optional and request.get(name) is None:
            return None
        number = _int(request, name, 1, 2 ** 53)
        with self.lock:
            value = table.get(number)
        if value is None:
            raise Refused(INVALID_ARGUMENT, "unknown " + name)
        return value

    def select_runtime(self, request):
        millis = _int(request, "millis", 1, 4294967295)
        token = self._get(self.signals, request, "signal", optional=True)
        server = self.library.select_runtime(timeout=millis / 1000.0, cancellation=token)
        return {"server": {"principal_kind": server.principal_kind, "principal": server.principal,
                           "program": server.program}}

    def runtime_endpoint(self, request):
        return {"endpoint": self.library.runtime_endpoint()}

    def session_call(self, request):
        """The existing shared framed ABI; all routing stays inside OA."""
        endpoint = _text(request, "endpoint").encode("utf-8")
        millis = _int(request, "millis", 1, 4294967295)
        maximum = _int(request, "max_frame", 1, MAX_DATA)
        flags = _int(request, "flags", 0, 7)
        encoded = request.get("data")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_DATA + 2) // 3):
            raise Refused(INVALID_ARGUMENT, "invalid frame")
        try:
            frame = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise Refused(INVALID_ARGUMENT, "invalid frame") from None
        if len(frame) > maximum:
            raise Refused(INVALID_ARGUMENT, "frame exceeds bound")
        token = self._get(self.signals, request, "signal", optional=True)
        expected = None
        if request.get("server") is not None:
            server = request["server"]
            if not isinstance(server, dict):
                raise Refused(INVALID_ARGUMENT, "invalid server")
            principal = _text(server, "principal").encode("utf-8")
            program = _text(server, "program").encode("utf-8")
            kind = _int(server, "principal_kind", 1, 2)
            expected = self.ipc._Expectation(ctypes.sizeof(self.ipc._Expectation), 1, kind, 0,
                principal, len(principal), program, len(program))
        answer, sent = ctypes.c_void_p(), ctypes.c_size_t()
        def call(signal):
            return self.library._session_call(endpoint, len(endpoint), millis, signal,
                ctypes.byref(expected) if expected is not None else None,
                frame, len(frame), maximum, flags, ctypes.byref(answer), ctypes.byref(sent))
        try:
            if token is None:
                status = call(None)
            else:
                with token._lock:
                    if token._closing or not token._handle.value:
                        return {"status": CANCELLED, "sent": 0}
                    token._opening += 1
                    signal = token._handle
                try:
                    status = call(signal)
                finally:
                    with token._lock:
                        token._opening -= 1
                        token._condition.notify_all()
            reply = {"status": status, "sent": sent.value}
            if status == OK and not flags & 1:
                size = ctypes.c_size_t()
                data = self.library._reply_data(answer, ctypes.byref(size))
                if size.value > maximum:
                    raise Refused(IO_ERROR, "native reply exceeds bound")
                reply["data"] = base64.b64encode(ctypes.string_at(data, size.value)).decode("ascii")
            return reply
        finally:
            if answer.value:
                self.library._reply_release(answer)

    def cancellation_create(self, request):
        token = self.ipc.Cancellation(self.library)
        number = self._add(self.signals, token)
        if number is None:
            token.close()
            raise Refused(IO_ERROR, "too many open objects")
        return {"signal": number}

    def cancellation_signal(self, request):
        self._get(self.signals, request, "signal").signal()
        return {}

    def cancellation_release(self, request):
        number = _int(request, "signal", 1, 2 ** 53)
        with self.lock:
            token = self.signals.pop(number, None)
        if token is not None:
            token.close()
        return {}

    def open(self, request):
        endpoint = _text(request, "endpoint").encode("utf-8")
        millis = _int(request, "millis", 1, 4294967295)
        token = self._get(self.signals, request, "signal", optional=True)
        server = request.get("server")
        if token is None:
            return self._open(endpoint, millis, None, server)
        # As FrameTransport holds it: the signal is not released while an open waits on it.
        with token._lock:
            if token._closing or not token._handle.value:
                raise Refused(CANCELLED, "cancellation signal closed")
            token._opening += 1
            signal = token._handle
        try:
            return self._open(endpoint, millis, signal, server)
        finally:
            with token._lock:
                token._opening -= 1
                token._condition.notify_all()

    def _open(self, endpoint, millis, signal, server):
        handle = ctypes.c_void_p()
        if server is None:
            status = self.library._open_cancelable(endpoint, len(endpoint), millis, signal, ctypes.byref(handle))
        else:
            if not isinstance(server, dict):
                raise Refused(INVALID_ARGUMENT, "server must be an object")
            principal = _text(server, "principal").encode("utf-8")
            program = _text(server, "program").encode("utf-8")
            kind = _int(server, "principal_kind", 1, 2)
            verified = getattr(self.library, "_open_verified", None)
            if verified is None:
                raise Refused(PROOF_UNAVAILABLE, "native server proof unavailable")
            expected = self.ipc._Expectation(ctypes.sizeof(self.ipc._Expectation), 1, kind, 0,
                                             principal, len(principal), program, len(program))
            status = verified(endpoint, len(endpoint), millis, signal, ctypes.byref(expected), ctypes.byref(handle))
        if status != OK:
            return {"status": status}
        number = self._add(self.handles, handle)
        if number is None:
            self.library._close(handle)
            raise Refused(IO_ERROR, "too many open objects")
        return {"handle": number}

    def write(self, request):
        data = request.get("data")
        if not isinstance(data, str) or len(data) > (MAX_DATA + 2) // 3 * 4:
            raise Refused(INVALID_ARGUMENT, "data must be base64 of at most %d bytes" % MAX_DATA)
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise Refused(INVALID_ARGUMENT, "data is not base64") from None
        if len(raw) > MAX_DATA:
            raise Refused(INVALID_ARGUMENT, "data must be base64 of at most %d bytes" % MAX_DATA)
        handle = self._get(self.handles, request, "handle")
        moved = ctypes.c_size_t()
        buffer = ctypes.create_string_buffer(raw, len(raw))
        status = self.library._write(handle, buffer, len(raw), ctypes.byref(moved))
        return {"status": status, "moved": moved.value}

    def read(self, request):
        size = _int(request, "size", 1, MAX_DATA)
        handle = self._get(self.handles, request, "handle")
        buffer = ctypes.create_string_buffer(size)
        moved = ctypes.c_size_t()
        status = self.library._read(handle, buffer, size, ctypes.byref(moved))
        count = min(moved.value, size)
        return {"status": status, "data": base64.b64encode(buffer.raw[:count]).decode("ascii")}

    def close(self, request):
        number = _int(request, "handle", 1, 2 ** 53)
        with self.lock:
            handle = self.handles.pop(number, None)
        if handle is not None:
            self.library._close(handle)
        return {}

    def close_all(self):
        with self.lock:
            handles, self.handles = list(self.handles.values()), {}
            signals, self.signals = list(self.signals.values()), {}
        for token in signals:
            token.signal()
        for handle in handles:
            self.library._close(handle)


# Ops answered on the reading thread: they never wait, and a signal reaches a
# call that waits on another thread.
IMMEDIATE = frozenset({"cancellation_signal", "cancellation_create"})
OPS = IMMEDIATE | {"session_call", "select_runtime", "runtime_endpoint", "cancellation_release", "open", "write", "read", "close"}


class Server:
    """Reads requests from input and writes replies to output, one line each."""

    def __init__(self, relay, input, output, frame_error=None):
        self.relay, self.input, self.output = relay, input, output
        self.frame_error = frame_error
        self.write_lock = threading.Lock()
        self.pending = threading.BoundedSemaphore(MAX_PENDING)

    def send(self, reply):
        line = json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.write_lock:
            try:
                self.output.write(line)
                self.output.flush()
            except (OSError, ValueError):
                pass

    def answer(self, request):
        number = request.get("id")
        try:
            op = request.get("op")
            if op not in OPS:
                raise Refused(INVALID_ARGUMENT, "unknown op")
            reply = getattr(self.relay, op)(request)
            reply.setdefault("status", OK)
        except Refused as refused:
            reply = {"status": refused.status, "message": str(refused)}
        except Exception as error:
            status = getattr(error, "status", None)
            if self.frame_error is None or not isinstance(error, self.frame_error) or type(status) is not int:
                status = IO_ERROR
            reply = {"status": status, "message": str(error)}
        reply["id"] = number
        self.send(reply)

    def _threaded(self, request):
        try:
            self.answer(request)
        finally:
            self.pending.release()

    def read_line(self):
        """One line, or None at the end of input; b"" for a line over MAX_LINE,
        whose rest is read and dropped."""
        line = self.input.readline(MAX_LINE + 1)
        if not line:
            return None
        if len(line) > MAX_LINE or not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):
                line = self.input.readline(MAX_LINE + 1)
            return b""
        return line

    def serve(self):
        while True:
            line = self.read_line()
            if line is None:
                return
            try:
                request = json.loads(line) if line else None
            except ValueError:
                request = None
            if not isinstance(request, dict) or not (request.get("id") is None or type(request.get("id")) is int):
                self.send({"id": None, "status": INVALID_ARGUMENT,
                           "message": "each line is one JSON object of at most %d bytes" % MAX_LINE})
                continue
            if request.get("op") in IMMEDIATE:
                self.answer(request)
            elif self.pending.acquire(blocking=False):
                threading.Thread(target=self._threaded, args=(request,), daemon=True).start()
            else:
                self.send({"id": request.get("id"), "status": IO_ERROR, "message": "too many calls at once"})


def main(argv=None):
    parser = argparse.ArgumentParser(prog="oa_helper.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", required=True)
    parser.add_argument("--library", required=True)
    args = parser.parse_args(argv)
    output = sys.stdout.buffer
    if not os.path.isabs(args.path) or not os.path.isabs(args.library):
        output.write(b'{"hello":false,"message":"--path and --library must be absolute"}\n')
        return 2
    sys.path.insert(0, args.path)
    try:
        import abstraction.ipc as ipc
        library = ipc.Library(args.library)
    except Exception as error:
        output.write(json.dumps({"hello": False, "message": "the OA IPC client did not load: %r" % (error,)})
                     .encode("utf-8") + b"\n")
        output.flush()
        return 1
    relay = Relay(library, ipc)
    server = Server(relay, sys.stdin.buffer, output, ipc.FrameError)
    server.send({"hello": True, "version": VERSION, "pid": os.getpid(),
                 "features": library._features() if library._features is not None else 0})
    try:
        server.serve()
    finally:
        relay.close_all()
    return 0


if __name__ == "__main__":
    # Calls still waiting in native code end with the process.
    code = main()
    sys.stdout.flush()
    os._exit(code)
