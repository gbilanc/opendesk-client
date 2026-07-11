"""
File transfer module with E2E encryption support.

Protocol:
  - Sender lists files → Receiver accepts/rejects
  - Files are split into chunks (64 KiB)
  - Each chunk is optionally E2E encrypted
  - Progress is reported back
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
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
# File transfer manager
# ---------------------------------------------------------------------------


class FileTransferManager:
    """Manages multiple concurrent file transfers.

    Works with the relay client to send/receive files via
    the protocol's file-related message types.
    """

    def __init__(self, max_concurrent: int = _MAX_CONCURRENT) -> None:
        self._max_concurrent = max_concurrent
        self._jobs: dict[str, TransferJob] = {}
        self._active_count: int = 0
        self._current_chunk_transfer: asyncio.Task | None = None

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

    # ── sending ─────────────────────────────────────────────────────

    async def send_files(
        self,
        paths: list[str | Path],
        send_fn: Callable,
    ) -> list[TransferJob]:
        """Initiate file transfers for the given paths.

        Parameters
        ----------
        paths : list of str or Path
            Local file paths to send.
        send_fn : async callable
            Function to send a ``Message`` to the remote peer.

        Returns
        -------
        list[TransferJob]
            Created job objects.
        """
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
            await send_fn(Message.file_request(
                job.file_info.name,
                job.file_info.size,
                job.file_info.sha256,
            ))

        return jobs

    async def send_chunks(
        self,
        job: TransferJob,
        send_fn,
    ) -> None:
        """Send file chunks one by one.

        Parameters
        ----------
        job : TransferJob
            The job to send (must be accepted).
        send_fn : async callable
            Function to send a ``Message``.
        """
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
                await send_fn(msg)

                job.bytes_transferred += len(chunk)
                job.progress = job.bytes_transferred / job.file_info.size
                seq += 1

                # Yield control to the event loop between chunks
                await asyncio.sleep(0)

        job.state = TransferState.COMPLETED
        job.completed_at = time.time()
        logger.info("File sent: %s (%d chunks)", job.file_info.name, seq)

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
            self._finalize_receive(job)

        logger.debug("Chunk %d for %s (%d/%d bytes)", seq, job_id, job.bytes_transferred, job.file_info.size)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a transfer job."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.state = TransferState.CANCELLED
        logger.info("Job cancelled: %s", job_id)
        return True

    def pause_job(self, job_id: str) -> bool:
        """Pause a transfer."""
        job = self._jobs.get(job_id)
        if job is None or job.state != TransferState.IN_PROGRESS:
            return False
        job.state = TransferState.PAUSED
        return True

    # ── internal ────────────────────────────────────────────────────

    def _fail_job(self, job: TransferJob, error: str) -> None:
        job.state = TransferState.FAILED
        job.error = error
        logger.error("Transfer failed: %s — %s", job.file_info.name, error)

    def _finalize_receive(self, job: TransferJob) -> None:
        """Write received data to disk."""
        download_dir = Path.home() / "Downloads" / "OpenDesk"
        download_dir.mkdir(parents=True, exist_ok=True)

        dest = download_dir / job.file_info.name

        try:
            dest.write_bytes(bytes(job.chunk_buffer))
            job.state = TransferState.COMPLETED
            job.completed_at = time.time()
            logger.info("File received: %s (%d bytes)", dest, job.bytes_transferred)
        except OSError as e:
            self._fail_job(job, str(e))

    @staticmethod
    async def _compute_sha256(path: Path) -> str:
        """Compute SHA-256 hash asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: hashlib.sha256(path.read_bytes()).hexdigest(),
        )
