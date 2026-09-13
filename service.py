"""Application delivery and caller recovery metadata over the public job API."""
import json
import math
import os
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

# Serializes this ComfyUI process's model deliveries. The service remains the
# authority for duplicate request identities across processes and retries.
_delivery_lock = threading.Lock()
_configuration = None


def configure(machine_factory=None, restore_factory=None):
    """Supply independent host trust for an explicitly configured embedding.

    Factories receive the operation's absolute deadline. Restore must preserve
    the supplied endpoint and logical owner. Calling with no arguments restores
    normal installed-runtime selection. Configure before starting downloads.
    """
    global _configuration
    if machine_factory is None and restore_factory is None:
        _configuration = None
    elif callable(machine_factory) and callable(restore_factory):
        _configuration = (machine_factory, restore_factory)
    else:
        raise ValueError("supply both trusted machine and restoration factories")


def deliver(url, directory, filename):
    from abstraction.facade.client import Machine
    from abstraction.facade.jobs import Jobs
    from abstraction.download.request import rec as download
    from abstraction.job.acceptance import rec as job

    required = tuple(job.ADMISSION_GUARANTEES)
    timeout = float(os.environ.get("ABSTRACTION_COMFYUI_WAIT_SECONDS", "900"))
    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError("ABSTRACTION_COMFYUI_WAIT_SECONDS must be between 0 and 86400")
    deadline = time.monotonic() + timeout
    if not _delivery_lock.acquire(timeout=timeout):
        raise TimeoutError("waiting for another model delivery; no new work submitted")
    try:
        final = os.path.abspath(os.path.join(directory, filename))
        if os.path.exists(final):
            return
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError("the installed download profile requires an HTTP(S) model URL")
        request = download.Request(artifact=download.Artifact(),
                                   sources=[download.Source(scheme=scheme, locator=url)])
        spec = download.encode(request)
        if len(spec) > 16384:
            raise ValueError("model request exceeds the adapter recovery-record budget")
        record_path = final + ".oa-request.json"
        os.makedirs(os.path.dirname(final), exist_ok=True)
        fresh = not os.path.exists(record_path)
        if fresh:
            machine = (Machine(deadline=deadline) if _configuration is None
                       else _configuration[0](deadline=deadline))
            binding = machine.resolve_jobs(guarantees=required, scope="local")
            window = binding.GetHistoryWindow()
            record = dict(version=1, endpoint=binding.endpoint, owner=binding.owner,
                          epoch=window.history_epoch, key=uuid.uuid4().hex,
                          required=list(required), spec=spec.decode("utf-8"), final=final)
            # Exclusive creation refuses races and incomplete predecessor writes.
            # Caller metadata is durable before the first possible acceptance.
            with open(record_path, "x", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
        else:
            with open(record_path, encoding="utf-8") as source:
                record = json.loads(source.read(65537))
            if (record.get("version") != 1 or record.get("final") != final
                    or record.get("spec") != spec.decode("utf-8") or record.get("required") != list(required)
                    or any(not isinstance(record.get(k), str) or not record[k]
                           for k in ("endpoint", "owner", "epoch", "key"))):
                raise ValueError("retained request differs or is invalid; resolve it before changing this model")
            restore = Jobs.restore_installed if _configuration is None else _configuration[1]
            binding = restore(record["endpoint"], record["owner"],
                              required_guarantees=required, deadline=deadline)
        identity = job.RequestIdentity(key=record["key"], history_epoch=record["epoch"])
        submission = job.Submission(identity=identity, kind="download", spec=spec,
                                    required_guarantees=list(required))
        result = binding.Submit(submission) if fresh else binding.Reconcile(identity)
        if not fresh and result.outcome == "definitely_not_accepted":
            # Same identity and exact request only. The service may retain a
            # sealed refusal, which remains visible below.
            result = binding.Submit(submission)
        if result.outcome != "accepted":
            raise RuntimeError("download acceptance: " + result.outcome + "; retained request kept")
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("model wait expired; accepted work and recovery record retained")
            observed = binding.ObserveWork(identity)
            if observed.outcome != "observed":
                raise RuntimeError("download observation: " + observed.outcome)
            snapshot = observed.snapshot
            if snapshot.state == "complete":
                break
            if snapshot.state in ("failed", "cancelled"):
                raise RuntimeError("download state: " + snapshot.state + "; retained request kept")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(final), prefix=".oa-model-", delete=False) as output:
                temporary = output.name
                binding.CopyResult(identity, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, final)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)
        # Keep recovery metadata: deletion/re-delivery reuses the accepted work.
    finally:
        _delivery_lock.release()
