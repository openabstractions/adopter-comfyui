"""Which program this ComfyUI's OpenAbstractions calls leave from.

OpenAbstractions knows a caller by the file its process runs from, and a
venv's shared interpreter is never an application (VISION 2026-10-07). When
the "OpenAbstractions for ComfyUI" extension runs in this ComfyUI, it
registers its relay as sys.modules["openabstractions_relay"]
(adopters/comfyui/node/oa_relay.py): every call this package makes then goes
through the extension's helper program, an interpreter copy in the
extension's own folder, and OpenAbstractions knows the calls by it. Without
the extension this ComfyUI's own interpreter makes them, as in the
OA-built ComfyUI, whose embedded Python lies in its own folder.

Each Machine, Jobs.restore_installed and Library this package makes takes
its library from here.
"""

import os
import sys

RELAY = "openabstractions_relay"


def configure_startup(environ=None):
    """Bind an explicitly selected development runtime before installers run.

    The pair is private launch DATA. Account and executable expectation come
    from the facade selector; every call retains the admitted relay library.
    An absent pair leaves installed discovery unchanged.
    """
    env = os.environ if environ is None else environ
    runtime = env.get("ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM")
    endpoint = env.get("ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT")
    if runtime is None and endpoint is None:
        return False
    if not runtime or not endpoint:
        raise ValueError("development runtime requires program and endpoint")
    relay = library()
    if relay is None:
        raise ValueError("development runtime requires the admitted relay")
    from abstraction.facade.client import Machine, select_development
    from abstraction.facade.jobs import Jobs
    from abstraction.ipc import FrameTransport
    import abstraction.facade as wire
    from . import (service, oa_logging, oa_card, oa_diagnostics, oa_inventory,
                   oa_download_observer)
    selected, expected = select_development(runtime, endpoint)

    def machine(deadline=None, **waiting):
        return Machine(selected, server=expected, library=relay, deadline=deadline, **waiting)

    def restore(address, owner, **waiting):
        return Jobs.restore(address, owner, server=expected, library=relay, **waiting)

    def resolver(deadline):
        return wire.ResolverClient(FrameTransport(relay, selected, server=expected, deadline=deadline))

    service.configure(machine, restore)
    oa_logging.configure(machine)
    oa_card.configure(machine, relay.cancellation)
    oa_diagnostics.configure(resolver)
    oa_inventory.configure(machine)
    oa_download_observer.configure(machine)
    return True


def library():
    """The relay's IPC library, or None for the OA SDK's own."""
    relay = sys.modules.get(RELAY)
    return None if relay is None else relay.library()


def native():
    """library(), or the OA SDK's own Library when no relay runs."""
    found = library()
    if found is not None:
        return found
    from abstraction.ipc import Library
    return Library()


def program():
    """The helper program the relay's calls come from, or None without one."""
    relay = sys.modules.get(RELAY)
    return None if relay is None else relay.program()
