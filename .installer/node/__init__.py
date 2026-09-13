"""OpenAbstractions for ComfyUI.

The per-user "OpenAbstractions for ComfyUI" installer
(installer/apps/build_extension.py with adopters/comfyui/extension.json)
copies this folder into ComfyUI's custom_nodes as comfyui-openabstractions.
It adds the Connect and downloads sidebar entries and the existing OA image
and video nodes, with their workflows and pure client closure.

The ComfyUI connector beside this file (comfyui-connector.exe) is the
program OpenAbstractions knows this ComfyUI by: agents open and drive this
ComfyUI through it, and the OA nodes' calls go through its native-relay. When
ComfyUI's server listens, oa_listening.py reports its port to the connector.

Without the connector, it gives this ComfyUI a program of its own: when ComfyUI starts, the
interpreter copy in helper/ is made or refreshed for stock installs, or verified
read-only for the sealed Desktop pack (oa_interpreter.py), and the
OA nodes' calls go through the helper that runs from it (oa_relay.py,
oa_helper.py), which this package registers as sys.modules["openabstractions_relay"].
"""

import logging
import os
import sys

from . import oa_connect, oa_interpreter, oa_relay, oa_appmodel_bridge, oa_listening

# ComfyUI loads a custom node folder only when it names NODE_CLASS_MAPPINGS;
# WEB_DIRECTORY is the folder it serves under /extensions/<folder>/.
NODE_CLASS_MAPPINGS = {}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]

oa_relay.register()
_clients = os.path.join(os.path.dirname(__file__), 'oa_client')
if _clients in sys.path:
    sys.path.remove(_clients)
sys.path.insert(0, _clients)
if sys.platform == "win32":
    try:
        oa_interpreter.ensure(oa_relay.HERE)
    except oa_interpreter.InterpreterError as error:  # the extension never stops ComfyUI from starting
        logging.getLogger("openabstractions").warning("[openabstractions] no helper program: %s", error)
oa_connect.install()
# The graph page's routes; its bridge connects the graph channel the
# connector names while it serves this ComfyUI.
try:
    oa_appmodel_bridge.install()
except Exception as error:  # the extension never stops ComfyUI from starting
    logging.getLogger("openabstractions").warning("[openabstractions] no graph page: %s", error)
# The connector serves this ComfyUI to agents once it reports its port; the
# picture models OpenAbstractions prepared join ComfyUI's model folders.
try:
    oa_listening.add_model_folders()
    oa_listening.report()
except Exception as error:  # the extension never stops ComfyUI from starting
    logging.getLogger("openabstractions").warning("[openabstractions] %s", error)
# OA's local model store joins ComfyUI's model folders; workflows load with the files OA picks.
try:
    from . import oa_model_files
    oa_model_files.install()
except Exception as error:  # the extension never stops ComfyUI from starting
    logging.getLogger("openabstractions").warning("[openabstractions] model files: %s", error)
# The connector holds card leases for the models ComfyUI loads, so ComfyUI and
# OA's own engines take turns on the card (oa_lease.py, connector/card.go).
try:
    if oa_relay.connector() is not None:
        from . import oa_lease
        oa_lease.install()
except Exception as error:  # the extension never stops ComfyUI from starting
    logging.getLogger("openabstractions").warning("[openabstractions] card leases: %s", error)

# The relay owns app attribution before the functional adopter imports any
# clients. The installed runtime supplies the native library, never this pack.
# CARD stays off: the connector holds the card leases (oa_lease.py).
for _service in ('LOGGING', 'INVENTORY', 'SNAPSHOTS', 'DIAGNOSTICS', 'DOWNLOAD_OBSERVER',
                 'MODEL_DOWNLOADS', 'HUB'):
    # This installed profile is process-local. Explicit operator values,
    # including empty or invalid values, remain the functional adopter's input.
    os.environ.setdefault('ABSTRACTION_COMFYUI_' + _service, 'service')
from . import oa_nodes
NODE_CLASS_MAPPINGS.update(oa_nodes.NODE_CLASS_MAPPINGS)
