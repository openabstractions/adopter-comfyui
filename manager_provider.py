"""The node's download provider for a Manager that offers a provider seam.

A ComfyUI-Manager whose manager_downloader has set_download_provider (the
prototype in research/adoption/comfyui/l2-provider-seam.patch) calls a provider
instead of having its model-install bindings rebound. The node registers
PROVIDER there, and nothing in Manager is monkeypatched.

PROVIDER.download_url declines custom-node code installs (registry archives and
3.x copy_install files) by returning False, and Manager downloads those itself.
Model files go through the same path as the rebinding: the job service.
PROVIDER.download_repo declines unless
ABSTRACTION_COMFYUI_SNAPSHOTS=service. Failures after admission raise, and
Manager reports the install as failed. An OA that cannot be reached before any
work is recorded returns False with one log line, and Manager downloads.
"""


class ManagerDownloadProvider:
    def __init__(self, node):
        self._node = node

    def download_url(self, url, dest_folder, filename):
        node = self._node
        if not node._is_model_destination(dest_folder):
            node.LOG.info("[abstraction] %s is a custom-node file install; ComfyUI-Manager downloads it", filename)
            return False
        try:
            node._through_the_abstraction(url, dest_folder, filename)
        except Exception as e:
            if node._unreachable(e):
                node._falls_back(filename, e)
                return False
            node.LOG.error("[abstraction] download failed for %s: %r", url, e)
            raise
        return True

    def download_repo(self, repo_id, local_dir):
        node = self._node
        if not node.snapshot_status().adopted:
            return False
        node.download_repo_in_bytes(repo_id, local_dir)
        return True
