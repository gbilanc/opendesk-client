"""
File transfer module with E2E encryption support.

Protocol:
  - Sender lists files → Receiver accepts/rejects
  - Files are split into chunks (64 KiB)
  - Each chunk is optionally E2E encrypted
  - Progress is reported back

Architecture:
  FileTransferManager runs its own asyncio event loop in a background
  daemon thread.  All async operations (chunk transfers, hashing) are
  scheduled on that loop via ``run_coroutine_threadsafe``.  This avoids
  depending on a running event loop in the Qt main thread.

  UI updates are delivered via a thread-safe queue (``updates``).
  The Qt main thread should poll this queue periodically (e.g. with
  a QTimer) and process the events.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import BinaryIO

from opendesk.network.protocol import Message, MessageType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 64 * 1024  # 64 KiB
_MAX_CONCURRENT = 4
_MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024  # 4 GiB safety limit
_PART_SUFFIX = ".opendesk-part"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TransferDirection(Enum):
    SEND = auto()
    RECEIVE = auto()


class TransferState(Enum):
    PENDING = auto()
    ACCEPTED = auto()
    IN_PROGRESS = auto()
    PAUSED = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()


@dataclass
class FileInfo:
    """Metadata for a file to transfer."""

    path: str = ""
    name: str = ""
    size: int = 0
    mtime: float = 0.0
    sha256: str = ""

    @classmethod
    def from_path(cls, path: str | Path) -> FileInfo:
        path = Path(path)
        stat = path.stat()
        return cls(
            path=str(path),
            name=path.name,
            size=stat.st_size,
            mtime=stat.st_mtime,
        )


@dataclass
class TransferJob:
    """A single file transfer."""

    id: str
    file_info: FileInfo
    direction: TransferDirection
    state: TransferState = TransferState.PENDING
    progress: float = 0.0  # 0.0 … 1.0
    bytes_transferred: int = 0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    expected_seq: int = 0
    temp_path: str = ""
    final_path: str = ""


@dataclass
class _ReceiveTarget:
    """Open write handle + incremental SHA-256 for an incoming file."""

    handle: BinaryIO
    hasher: "hashlib._Hash"


# ---------------------------------------------------------------------------
# Background event loop
# ---------------------------------------------------------------------------


class _BgEventLoop:
    """A daemon thread running an asyncio event loop forever.

    Used by ``FileTransferManager`` to schedule async operations
    (chunk transfers, SHA computation) without needing a running
    loop in the Qt main thread.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Create and start the background event loop thread."""
        if self._thread and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="file-transfer-loop",
        )
        self._thread.start()
        logger.debug("Background event loop started")

    def stop(self) -> None:
        """Stop the background event loop."""
        if self._loop and self._thread and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2.0)
            self._loop.close()
        self._loop = None
        self._thread = None

    def run(self, coro) -> Future:
        """Schedule a coroutine on the background loop.

        Returns a thread-safe future that can be polled or awaited via
        ``result()`` from non-async callers.
        """
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("Background event loop not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# File transfer manager
# ---------------------------------------------------------------------------


class FileTransferManager:
    """Manages multiple concurrent file transfers.

    Uses its own background event loop for async operations so that
    it works correctly even when the Qt main thread has no running
    asyncio event loop.

    All public methods are thread-safe and can be called from any thread.

    UI update events are pushed into the ``updates`` queue (thread-safe).
    The format is:

    - ``("transfer", job_id)`` — a transfer job changed state.
      Read the updated job via ``get_job(job_id)``.
    - ``("listing", path, entries, error)`` — remote directory listing arrived.
    - ``("status", message)`` — a status message for the UI.

    The Qt main thread should poll ``updates`` with a QTimer (~200 ms).
    """

    def __init__(
        self,
        max_concurrent: int = _MAX_CONCURRENT,
        receive_root: str | Path | None = None,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._jobs: dict[str, TransferJob] = {}
        self._receive_targets: dict[str, _ReceiveTarget] = {}
        self._receive_root = (
            Path(receive_root or Path.home() / "Downloads" / "OpenDesk").expanduser().resolve()
        )

        # Background event loop for async operations
        self._bg_loop = _BgEventLoop()
        self._bg_loop.start()

        # Thread-safe queue for UI updates (polled by Qt main thread)
        self.updates: queue.Queue = queue.Queue()

    # ── lifecycle ───────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop the background event loop. Call on application exit."""
        for job_id in list(self._receive_targets):
            self._close_receive_target(job_id)
        self._bg_loop.stop()

    # ── properties ──────────────────────────────────────────────────

    @property
    def jobs(self) -> list[TransferJob]:
        return list(self._jobs.values())

    @property
    def active_jobs(self) -> list[TransferJob]:
        return [
            j
            for j in self._jobs.values()
            if j.state
            in (
                TransferState.PENDING,
                TransferState.ACCEPTED,
                TransferState.IN_PROGRESS,
            )
        ]

    @property
    def completed_jobs(self) -> list[TransferJob]:
        return [j for j in self._jobs.values() if j.state == TransferState.COMPLETED]

    def get_job(self, job_id: str) -> TransferJob | None:
        """Look up a transfer job by ID."""
        return self._jobs.get(job_id)

    # ── sending (upload) ────────────────────────────────────────────

    def send_files(
        self,
        paths: list[str | Path],
        send_fn: Callable,
        remote_dest_path: str = "",
    ) -> Future:
        """Initiate file transfers for the given paths.

        Parameters
        ----------
        paths : list of str or Path
            Local file paths to send.
        send_fn : callable
            Function to send a ``Message`` to the remote peer
            (e.g. ``lambda msg: relay.send_message(msg)``).
        remote_dest_path : str
            Remote directory where the files should be saved.
            Passed to the receiver via FILE_REQUEST payload.
        """
        # Register jobs on the caller thread so the background coroutine
        # never mutates ``self._jobs`` concurrently with UI accessors.
        jobs: list[TransferJob] = []
        for path in paths:
            path_obj = Path(path)
            if not path_obj.exists():
                logger.warning("File not found: %s", path)
                continue
            job = TransferJob(
                id=f"send-{uuid.uuid4().hex}",
                file_info=FileInfo.from_path(path_obj),
                direction=TransferDirection.SEND,
            )
            self._jobs[job.id] = job
            jobs.append(job)
        return self._bg_loop.run(self._send_files_async(jobs, send_fn, remote_dest_path))

    async def _send_files_async(
        self,
        jobs: list[TransferJob],
        send_fn: Callable,
        remote_dest_path: str = "",
    ) -> None:
        """Hash files, then send a FILE_REQUEST for each (background)."""
        for job in jobs:
            job.file_info.sha256 = await self._compute_sha256(Path(job.file_info.path))
            logger.info(
                "File transfer queued: %s (%d bytes)", job.file_info.name, job.file_info.size
            )

        # Send file request messages with the sender's job_id so the
        # receiver echoes it back in FILE_ACCEPT and both sides use
        # the same identifier for the transfer.  Include the remote
        # destination path so the receiver knows where to save.
        for job in jobs:
            send_fn(
                Message.file_request(
                    job.file_info.name,
                    job.file_info.size,
                    job.file_info.sha256,
                    job_id=job.id,
                    dest_path=remote_dest_path,
                )
            )
            self._push_update("transfer", job.id)

    def send_chunks(
        self,
        job: TransferJob,
        send_fn: Callable,
    ) -> None:
        """Send file chunks one by one in the background.

        Parameters
        ----------
        job : TransferJob
            The job to send (must be accepted).
        send_fn : callable
            Function to send a ``Message``.
        """
        self._bg_loop.run(self._send_chunks_async(job, send_fn))

    async def _send_chunks_async(
        self,
        job: TransferJob,
        send_fn: Callable,
    ) -> None:
        """Async implementation of send_chunks."""
        await self._stream_file_async(job, send_fn)

    async def _stream_file_async(self, job: TransferJob, send_fn: Callable) -> None:
        """Stream a local file in chunks, honouring cancellation.

        Runs on the background event loop.  Any error fails the job
        instead of leaving it stuck IN_PROGRESS.
        """
        path = Path(job.file_info.path)
        if not path.exists():
            send_fn(Message.file_error(job.id, "File missing"))
            self._fail_job(job, "File missing")
            return

        job.state = TransferState.IN_PROGRESS
        job.started_at = time.time()
        self._push_update("transfer", job.id)

        seq = 0
        try:
            with open(path, "rb") as f:
                while True:
                    if job.state == TransferState.CANCELLED:
                        logger.info("Send cancelled: %s", job.id)
                        return

                    chunk = f.read(_CHUNK_SIZE)
                    if not chunk:
                        break

                    send_fn(Message.file_chunk(job.id, seq, chunk, is_last=False))

                    job.bytes_transferred += len(chunk)
                    job.progress = job.bytes_transferred / job.file_info.size
                    seq += 1

                    # Throttle progress updates to avoid flooding the queue
                    if seq % 10 == 0:
                        self._push_update("transfer", job.id)

                    # Yield control between chunks
                    await asyncio.sleep(0)

            send_fn(Message.file_complete(job.id))
            job.state = TransferState.COMPLETED
            job.completed_at = time.time()
            self._push_update("transfer", job.id)
            logger.info("File sent: %s (%d chunks)", job.file_info.name, seq)
        except Exception as e:  # noqa: BLE001 — surface any failure to the job
            self._fail_job(job, f"Send failed: {e}")

    # ── receiving ───────────────────────────────────────────────────

    def handle_file_request(self, msg: Message) -> str | None:
        """Register an incoming transfer pending explicit local approval."""
        try:
            name = self._safe_filename(msg.payload.get("name", ""))
            size = int(msg.payload.get("size", 0))
        except (TypeError, ValueError):
            return None
        if size < 0 or size > _MAX_FILE_SIZE:
            logger.warning("Rejected incoming file %s: invalid size %d", name, size)
            return None

        job_id = str(msg.payload.get("job_id", "")) or f"recv-{uuid.uuid4().hex}"
        if job_id in self._jobs:
            logger.warning("Rejected duplicate incoming job: %s", job_id)
            return None

        job = TransferJob(
            id=job_id,
            file_info=FileInfo(
                name=name,
                size=size,
                sha256=str(msg.payload.get("sha256", "")),
                path=str(msg.payload.get("dest_path", "")),
            ),
            direction=TransferDirection.RECEIVE,
        )
        self._jobs[job_id] = job
        self._push_update("transfer", job_id)
        logger.info("Incoming file awaiting approval: %s (%d bytes) [job=%s]", name, size, job_id)
        return job_id

    def accept_incoming(self, job_id: str) -> bool:
        """Approve a pending incoming file and create its temporary target."""
        job = self._jobs.get(job_id)
        if job is None or job.direction != TransferDirection.RECEIVE:
            return False
        try:
            dest_dir = self._resolve_receive_dir(job.file_info.path)
            dest_dir.mkdir(parents=True, exist_ok=True)
            final_path = self._unique_destination(dest_dir, job.file_info.name)
            temp_path = final_path.with_name(f".{final_path.name}.{job.id}{_PART_SUFFIX}")
            handle = open(temp_path, "wb")
            job.final_path = str(final_path)
            job.temp_path = str(temp_path)
            job.file_info.path = str(dest_dir)
            job.state = TransferState.ACCEPTED
            job.started_at = time.time()
            self._receive_targets[job.id] = _ReceiveTarget(
                handle=handle, hasher=hashlib.sha256()
            )
            self._push_update("transfer", job.id)
            return True
        except OSError as e:
            self._fail_job(job, f"Cannot prepare destination: {e}")
            return False

    def reject_incoming(self, job_id: str, reason: str = "Rejected by local user") -> bool:
        """Reject a pending incoming transfer and remove partial data."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.state = TransferState.CANCELLED
        job.error = reason
        self._remove_partial(job)
        self._push_update("transfer", job.id)
        return True

    def handle_chunk(self, msg: Message) -> None:
        """Append one validated chunk directly to the temporary file."""
        job_id = str(msg.payload.get("job_id", ""))
        job = self._jobs.get(job_id)
        if job is None or job.direction != TransferDirection.RECEIVE:
            logger.warning("Chunk for unknown job: %s", job_id)
            return
        if job.state not in (TransferState.ACCEPTED, TransferState.IN_PROGRESS):
            self._fail_job(job, "Chunk received before local approval")
            return

        seq = msg.payload.get("seq")
        data = msg.payload.get("data", b"")
        if not isinstance(seq, int) or seq != job.expected_seq or not isinstance(data, bytes):
            self._fail_job(job, "Invalid or out-of-order file chunk")
            return
        if job.file_info.size and job.bytes_transferred + len(data) > job.file_info.size:
            self._fail_job(job, "Received more bytes than declared")
            return

        target = self._receive_targets.get(job.id)
        if target is None:
            self._fail_job(job, "Missing receive target")
            return
        try:
            target.handle.write(data)
            target.hasher.update(data)
        except OSError as e:
            self._fail_job(job, f"Write failed: {e}")
            return

        job.expected_seq += 1
        job.bytes_transferred += len(data)
        job.progress = job.bytes_transferred / job.file_info.size if job.file_info.size else 0.0
        job.state = TransferState.IN_PROGRESS
        if msg.payload.get("is_last", False):  # compatibility with legacy peers
            self.handle_file_complete(Message.file_complete(job.id))
        elif job.expected_seq % 10 == 0:
            self._push_update("transfer", job.id)

    def handle_file_complete(self, msg: Message) -> bool:
        """Validate size/hash and atomically promote a completed transfer."""
        job = self._jobs.get(str(msg.payload.get("job_id", "")))
        if job is None or job.direction != TransferDirection.RECEIVE:
            return False
        if job.file_info.size and job.bytes_transferred != job.file_info.size:
            self._fail_job(job, "Received size differs from declared size")
            self._remove_partial(job)
            return False
        target = self._receive_targets.pop(job.id, None)
        try:
            if target is not None:
                target.handle.close()
                checksum = target.hasher.hexdigest()
            else:
                checksum = self._sha256_file(Path(job.temp_path))
            if job.file_info.sha256 and checksum != job.file_info.sha256:
                self._fail_job(job, "SHA-256 verification failed")
                self._remove_partial(job)
                return False
            Path(job.temp_path).replace(job.final_path)
        except OSError as e:
            self._fail_job(job, f"Finalization failed: {e}")
            return False

        job.state = TransferState.COMPLETED
        job.progress = 1.0
        job.completed_at = time.time()
        self._push_update("transfer", job.id)
        logger.info("File received: %s (%d bytes)", job.final_path, job.bytes_transferred)
        return True

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a transfer and remove any partial receive file."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.state = TransferState.CANCELLED
        self._remove_partial(job)
        self._push_update("transfer", job_id)
        logger.info("Job cancelled: %s", job_id)
        return True

    def pause_job(self, job_id: str) -> bool:
        """Pause a transfer."""
        job = self._jobs.get(job_id)
        if job is None or job.state != TransferState.IN_PROGRESS:
            return False
        job.state = TransferState.PAUSED
        self._push_update("transfer", job_id)
        return True

    # ── internal ────────────────────────────────────────────────────

    def _push_update(self, kind: str, *args) -> None:
        """Push an update event into the thread-safe queue."""
        self.updates.put((kind, *args))

    def _fail_job(self, job: TransferJob, error: str) -> None:
        job.state = TransferState.FAILED
        job.error = error
        self._close_receive_target(job.id)
        self._push_update("transfer", job.id)
        logger.error("Transfer failed: %s — %s", job.file_info.name, error)

    @staticmethod
    def _safe_filename(value: object) -> str:
        name = Path(str(value)).name
        if not name or name in {".", ".."}:
            raise ValueError("Invalid file name")
        return name

    def _resolve_receive_dir(self, requested: str) -> Path:
        """Resolve the destination directory, falling back to the default.

        A requested path is honoured only if it is an existing directory
        inside the user's home.  Anything else (empty, a file, outside
        home) falls back to ``receive_root`` instead of failing the job.
        """
        if requested:
            candidate = Path(requested).expanduser().resolve()
            home = Path.home().resolve()
            if candidate.is_dir() and candidate.is_relative_to(home):
                return candidate
        return self._receive_root

    @staticmethod
    def _unique_destination(dest_dir: Path, name: str) -> Path:
        candidate = dest_dir / name
        index = 1
        while candidate.exists():
            candidate = dest_dir / f"{Path(name).stem} ({index}){Path(name).suffix}"
            index += 1
        return candidate

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _remove_partial(self, job: TransferJob) -> None:
        self._close_receive_target(job.id)
        if job.temp_path:
            try:
                Path(job.temp_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove partial transfer: %s", job.temp_path)

    def _close_receive_target(self, job_id: str) -> None:
        target = self._receive_targets.pop(job_id, None)
        if target is not None:
            try:
                target.handle.close()
            except OSError:
                logger.warning("Could not close partial transfer: %s", job_id)

    # ── directory listing ─────────────────────────────────────────────

    @staticmethod
    def list_directory(path: str | Path) -> tuple[list[dict], str]:
        """List the contents of a local directory.

        Parameters
        ----------
        path : str or Path
            Directory path to list.

        Returns
        -------
        (entries, error) tuple where:
        - entries is a list of dicts with keys: name, is_dir, size, mtime
        - error is an empty string on success, or an error message
        """
        path = Path(path)
        if not path.exists():
            return [], f"Path does not exist: {path}"
        if not path.is_dir():
            return [], f"Path is not a directory: {path}"

        entries: list[dict] = []
        try:
            for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                try:
                    stat = child.stat()
                    entries.append(
                        {
                            "name": child.name,
                            "is_dir": child.is_dir(),
                            "size": stat.st_size if child.is_file() else 0,
                            "mtime": stat.st_mtime,
                        }
                    )
                except OSError:
                    entries.append(
                        {
                            "name": child.name,
                            "is_dir": child.is_dir(),
                            "size": 0,
                            "mtime": 0.0,
                        }
                    )
        except PermissionError as e:
            return [], str(e)

        return entries, ""

    def handle_list_request(self, msg: Message) -> Message:
        """Handle an incoming FILE_LIST_REQUEST.

        Returns a FILE_LIST_RESPONSE message with the directory listing.
        """
        path = msg.payload.get("path", "/")
        entries, error = self.list_directory(path)
        return Message.file_list_response(path, entries, error=error)

    def request_remote_listing(
        self,
        path: str,
        send_fn: Callable,
    ) -> None:
        """Request a directory listing from the remote peer.

        The result will be delivered via the ``updates`` queue as a
        ``("listing", path, entries, error)`` event.
        """
        send_fn(Message.file_list_request(path))

    def handle_list_response(self, msg: Message) -> None:
        """Process an incoming FILE_LIST_RESPONSE.

        Pushes a ``("listing", path, entries, error)`` event onto
        the ``updates`` queue.
        """
        path = msg.payload.get("path", "")
        entries = msg.payload.get("entries", [])
        error = msg.payload.get("error", "")
        self._push_update("listing", path, entries, error)

    # ── download requests ─────────────────────────────────────────────

    def request_download(
        self,
        remote_path: str,
        send_fn: Callable,
        local_dest: str | Path | None = None,
    ) -> None:
        """Request to download a file from the remote peer.

        Parameters
        ----------
        remote_path : str
            Path of the file on the remote system.
        send_fn : callable
            Function to send a ``Message``.
        local_dest : str or Path, optional
            Local destination directory. If None, uses the default receive root.
        """
        name = Path(remote_path).name
        job_id = f"dl-{uuid.uuid4().hex}"

        file_info = FileInfo(path=remote_path, name=name)
        job = TransferJob(
            id=job_id,
            file_info=file_info,
            direction=TransferDirection.RECEIVE,
            state=TransferState.PENDING,
        )
        if local_dest:
            job.file_info.path = str(local_dest)
        self._jobs[job_id] = job
        self._push_update("transfer", job_id)

        send_fn(
            Message(
                MessageType.FILE_DOWNLOAD_REQUEST,
                {"remote_path": remote_path, "job_id": job_id},
            )
        )

    def handle_download_request(self, msg: Message, send_fn: Callable) -> None:
        """Handle an incoming FILE_DOWNLOAD_REQUEST from remote.

        Starts sending the requested file in chunks on the background loop.
        """
        remote_path = msg.payload.get("remote_path", "")
        job_id = msg.payload.get("job_id", "")

        path = Path(remote_path).expanduser().resolve()
        home = Path.home().resolve()
        if not path.is_relative_to(home):
            send_fn(Message.file_download_reject(job_id, "Path outside allowed directory"))
            return
        if not path.exists() or not path.is_file():
            send_fn(Message.file_download_reject(job_id, "File not found"))
            return

        file_info = FileInfo.from_path(path)
        job = TransferJob(
            id=job_id,
            file_info=file_info,
            direction=TransferDirection.SEND,
            state=TransferState.ACCEPTED,
        )
        self._jobs[job_id] = job
        self._push_update("transfer", job_id)

        # Start chunk transfer on background loop
        self._bg_loop.run(self._send_download_chunks_async(job, send_fn))

    async def _send_download_chunks_async(self, job: TransferJob, send_fn: Callable) -> None:
        """Send file chunks for a download request (background)."""
        send_fn(Message.file_download_accept(job.id))
        await self._stream_file_async(job, send_fn)

    def handle_download_accept(self, msg: Message) -> None:
        """Handle FILE_DOWNLOAD_ACCEPT — prepare the local temporary file."""
        job_id = msg.payload.get("job_id", "")
        job = self._jobs.get(job_id)
        if job and self.accept_incoming(job.id):
            job.state = TransferState.IN_PROGRESS
            self._push_update("transfer", job.id)

    def handle_download_reject(self, msg: Message) -> None:
        """Handle FILE_DOWNLOAD_REJECT."""
        job_id = msg.payload.get("job_id", "")
        reason = msg.payload.get("reason", "Rejected")
        job = self._jobs.get(job_id)
        if job:
            job.state = TransferState.CANCELLED
            job.error = reason
            self._push_update("transfer", job_id)

    @staticmethod
    async def _compute_sha256(path: Path) -> str:
        """Compute SHA-256 asynchronously without loading the file in RAM."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, FileTransferManager._sha256_file, path)
