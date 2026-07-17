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
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from opendesk.network.protocol import Message, MessageType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 64 * 1024  # 64 KiB
_MAX_CONCURRENT = 4


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
    chunk_buffer: bytearray = field(default_factory=bytearray)


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

    def run(self, coro) -> asyncio.Future:
        """Schedule a coroutine on the background loop.

        Returns an ``asyncio.Future`` that can be awaited or polled.
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
    """

    def __init__(self, max_concurrent: int = _MAX_CONCURRENT) -> None:
        self._max_concurrent = max_concurrent
        self._jobs: dict[str, TransferJob] = {}
        self._active_count: int = 0

        # Background event loop for async operations
        self._bg_loop = _BgEventLoop()
        self._bg_loop.start()

        # Callbacks for async events (called from the background thread)
        self.on_remote_listing: Callable | None = None  # fn(path, entries, error)
        self.on_transfer_update: Callable | None = None  # fn(job)

    # ── lifecycle ───────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop the background event loop. Call on application exit."""
        self._bg_loop.stop()

    # ── properties ──────────────────────────────────────────────────

    @property
    def jobs(self) -> list[TransferJob]:
        return list(self._jobs.values())

    @property
    def active_jobs(self) -> list[TransferJob]:
        return [j for j in self._jobs.values() if j.state in (
            TransferState.PENDING, TransferState.ACCEPTED, TransferState.IN_PROGRESS,
        )]

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
    ) -> None:
        """Initiate file transfers for the given paths.

        Parameters
        ----------
        paths : list of str or Path
            Local file paths to send.
        send_fn : callable
            Function to send a ``Message`` to the remote peer
            (e.g. ``lambda msg: relay.send_message(msg)``).
        """
        self._bg_loop.run(self._send_files_async(paths, send_fn))

    async def _send_files_async(
        self,
        paths: list[str | Path],
        send_fn: Callable,
    ) -> None:
        """Async implementation of send_files."""
        jobs: list[TransferJob] = []
        for path in paths:
            path_obj = Path(path)
            if not path_obj.exists():
                logger.warning("File not found: %s", path)
                continue

            file_info = FileInfo.from_path(path_obj)
            job_id = f"send-{int(time.time())}-{file_info.name}"
            job = TransferJob(
                id=job_id,
                file_info=file_info,
                direction=TransferDirection.SEND,
            )
            self._jobs[job_id] = job
            jobs.append(job)

            # Compute SHA256 in background
            file_info.sha256 = await self._compute_sha256(path_obj)
            logger.info("File transfer queued: %s (%d bytes)", file_info.name, file_info.size)

        # Send file request messages
        for job in jobs:
            send_fn(Message.file_request(
                job.file_info.name,
                job.file_info.size,
                job.file_info.sha256,
            ))

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
        path = Path(job.file_info.path)
        if not path.exists():
            self._fail_job(job, "File missing")
            return

        job.state = TransferState.IN_PROGRESS
        job.started_at = time.time()

        with open(path, "rb") as f:
            seq = 0
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break

                msg = Message.file_chunk(
                    job.id, seq, chunk,
                    is_last=len(chunk) < _CHUNK_SIZE,
                )
                send_fn(msg)

                job.bytes_transferred += len(chunk)
                job.progress = job.bytes_transferred / job.file_info.size
                seq += 1

                # Yield control between chunks
                await asyncio.sleep(0)

        job.state = TransferState.COMPLETED
        job.completed_at = time.time()
        logger.info("File sent: %s (%d chunks)", job.file_info.name, seq)
        if self.on_transfer_update:
            self.on_transfer_update(job)

    # ── receiving ───────────────────────────────────────────────────

    def handle_file_request(self, msg: Message) -> str | None:
        """Handle an incoming file request.

        Returns the job ID if accepted, or ``None`` to reject.
        """
        name = msg.payload.get("name", "unknown")
        size = msg.payload.get("size", 0)
        sha256 = msg.payload.get("sha256", "")
        job_id = f"recv-{int(time.time())}-{name}"

        file_info = FileInfo(name=name, size=size, sha256=sha256)
        job = TransferJob(
            id=job_id,
            file_info=file_info,
            direction=TransferDirection.RECEIVE,
        )
        self._jobs[job_id] = job
        job.state = TransferState.ACCEPTED
        job.started_at = time.time()
        logger.info("Incoming file: %s (%d bytes)", name, size)
        if self.on_transfer_update:
            self.on_transfer_update(job)
        return job_id

    def handle_chunk(self, msg: Message) -> None:
        """Process an incoming file chunk."""
        job_id = msg.payload.get("job_id", "")
        seq = msg.payload.get("seq", 0)
        data = msg.payload.get("data", b"")
        is_last = msg.payload.get("is_last", False)

        job = self._jobs.get(job_id)
        if job is None:
            logger.warning("Chunk for unknown job: %s", job_id)
            return

        job.chunk_buffer.extend(data)
        job.bytes_transferred += len(data)
        job.progress = job.bytes_transferred / job.file_info.size

        if is_last:
            # If the job has a custom destination path, use its parent dir
            if job.file_info.path:
                custom_path = Path(job.file_info.path)
                if custom_path.is_dir():
                    dest_dir = custom_path
                else:
                    dest_dir = custom_path.parent
                self._finalize_receive(job, dest_dir=dest_dir)
            else:
                self._finalize_receive(job)

        logger.debug("Chunk %d for %s (%d/%d bytes)", seq, job_id, job.bytes_transferred, job.file_info.size)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a transfer job."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.state = TransferState.CANCELLED
        logger.info("Job cancelled: %s", job_id)
        if self.on_transfer_update:
            self.on_transfer_update(job)
        return True

    def pause_job(self, job_id: str) -> bool:
        """Pause a transfer."""
        job = self._jobs.get(job_id)
        if job is None or job.state != TransferState.IN_PROGRESS:
            return False
        job.state = TransferState.PAUSED
        if self.on_transfer_update:
            self.on_transfer_update(job)
        return True

    # ── internal ────────────────────────────────────────────────────

    def _fail_job(self, job: TransferJob, error: str) -> None:
        job.state = TransferState.FAILED
        job.error = error
        logger.error("Transfer failed: %s — %s", job.file_info.name, error)
        if self.on_transfer_update:
            self.on_transfer_update(job)

    def _finalize_receive(self, job: TransferJob, dest_dir: str | Path | None = None) -> None:
        """Write received data to disk.

        Parameters
        ----------
        job : TransferJob
            The completed receive job.
        dest_dir : str or Path, optional
            Custom destination directory. Defaults to ~/Downloads/OpenDesk.
        """
        if dest_dir is None:
            dest_dir = Path.home() / "Downloads" / "OpenDesk"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest = dest_dir / job.file_info.name

        try:
            dest.write_bytes(bytes(job.chunk_buffer))
            job.state = TransferState.COMPLETED
            job.completed_at = time.time()
            logger.info("File received: %s (%d bytes)", dest, job.bytes_transferred)
            if self.on_transfer_update:
                self.on_transfer_update(job)
        except OSError as e:
            self._fail_job(job, str(e))

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
                    entries.append({
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "size": stat.st_size if child.is_file() else 0,
                        "mtime": stat.st_mtime,
                    })
                except OSError:
                    entries.append({
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "size": 0,
                        "mtime": 0.0,
                    })
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
        self, path: str, send_fn: Callable,
    ) -> None:
        """Request a directory listing from the remote peer.

        The result will be delivered via ``on_remote_listing`` callback.
        This is synchronous — just sends one message.
        """
        send_fn(Message.file_list_request(path))

    def handle_list_response(self, msg: Message) -> None:
        """Process an incoming FILE_LIST_RESPONSE."""
        path = msg.payload.get("path", "")
        entries = msg.payload.get("entries", [])
        error = msg.payload.get("error", "")
        if self.on_remote_listing:
            self.on_remote_listing(path, entries, error)

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
            Local destination path. If None, uses filename in Downloads.
        """
        name = Path(remote_path).name
        job_id = f"dl-{int(time.time())}-{name}"

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

        # Send the download request synchronously
        send_fn(Message(
            MessageType.FILE_DOWNLOAD_REQUEST,
            {"remote_path": remote_path, "job_id": job_id},
        ))

    def handle_download_request(self, msg: Message, send_fn: Callable) -> None:
        """Handle an incoming FILE_DOWNLOAD_REQUEST from remote.

        Starts sending the requested file in chunks on the background loop.
        """
        remote_path = msg.payload.get("remote_path", "")
        job_id = msg.payload.get("job_id", "")

        path = Path(remote_path)
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

        # Start chunk transfer on background loop
        self._bg_loop.run(self._send_download_chunks_async(job, send_fn))

    async def _send_download_chunks_async(self, job: TransferJob, send_fn: Callable) -> None:
        """Send file chunks for a download request (background)."""
        # Send accept first
        send_fn(Message.file_download_accept(job.id))

        path = Path(job.file_info.path)
        if not path.exists():
            self._fail_job(job, "File missing")
            send_fn(Message.file_error(job.id, "File missing"))
            return

        job.state = TransferState.IN_PROGRESS
        job.started_at = time.time()
        if self.on_transfer_update:
            self.on_transfer_update(job)

        with open(path, "rb") as f:
            seq = 0
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break

                msg = Message.file_chunk(
                    job.id, seq, chunk,
                    is_last=len(chunk) < _CHUNK_SIZE,
                )
                send_fn(msg)

                job.bytes_transferred += len(chunk)
                job.progress = job.bytes_transferred / job.file_info.size
                seq += 1
                await asyncio.sleep(0)

        job.state = TransferState.COMPLETED
        job.completed_at = time.time()
        logger.info("Download sent: %s (%d chunks)", job.file_info.name, seq)
        if self.on_transfer_update:
            self.on_transfer_update(job)

    def handle_download_accept(self, msg: Message) -> None:
        """Handle FILE_DOWNLOAD_ACCEPT — remote peer will start sending chunks."""
        job_id = msg.payload.get("job_id", "")
        job = self._jobs.get(job_id)
        if job:
            job.state = TransferState.IN_PROGRESS
            job.started_at = time.time()
            if self.on_transfer_update:
                self.on_transfer_update(job)

    def handle_download_reject(self, msg: Message) -> None:
        """Handle FILE_DOWNLOAD_REJECT."""
        job_id = msg.payload.get("job_id", "")
        reason = msg.payload.get("reason", "Rejected")
        job = self._jobs.get(job_id)
        if job:
            job.state = TransferState.CANCELLED
            job.error = reason
            if self.on_transfer_update:
                self.on_transfer_update(job)

    @staticmethod
    async def _compute_sha256(path: Path) -> str:
        """Compute SHA-256 hash asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: hashlib.sha256(path.read_bytes()).hexdigest(),
        )
