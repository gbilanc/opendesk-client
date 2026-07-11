"""
Local screen recording for session capture.

Records the remote session to a video file (MP4/H.264) with optional
audio.  Can be used for session replay or documentation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np

from opendesk.core.video_codec import VideoEncoder, EncoderConfig, QualityLevel

logger = logging.getLogger(__name__)

# Default output directory
_DEFAULT_OUTPUT = Path.home() / "Videos" / "OpenDesk"


@dataclass
class RecordingStatus:
    """Current recording state."""

    active: bool = False
    output_path: str = ""
    start_time: float = 0.0
    frames_written: int = 0
    duration_sec: float = 0.0
    file_size_mb: float = 0.0

    @property
    def elapsed(self) -> float:
        if self.start_time:
            return time.time() - self.start_time
        return 0.0


class ScreenRecorder:
    """Records the remote session to an MP4 file.

    Usage::

        rec = ScreenRecorder()
        rec.start("session_recording.mp4")
        for frame in frames:
            rec.write_frame(frame.data)
        rec.stop()
    """

    def __init__(self, output_dir: str | Path | None = None) -> None:
        self._output_dir = Path(output_dir) if output_dir else _DEFAULT_OUTPUT
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._encoder: VideoEncoder | None = None
        self._status = RecordingStatus()
        self._output_path: Path | None = None

    # ── properties ──────────────────────────────────────────────────

    @property
    def status(self) -> RecordingStatus:
        return self._status

    @property
    def is_recording(self) -> bool:
        return self._status.active

    # ── lifecycle ───────────────────────────────────────────────────

    def start(
        self,
        filename: str | None = None,
        width: int = 1280,
        height: int = 720,
        fps: float = 15.0,
        bitrate: int = 2_000_000,
    ) -> str:
        """Start recording to a file.

        Parameters
        ----------
        filename : str, optional
            Output filename.  Auto-generated if not provided.
        width, height : int
            Video resolution.
        fps : float
            Recording frame rate (lower = smaller files).
        bitrate : int
            Encoding bitrate.

        Returns
        -------
        str
            Path to the output file.
        """
        if self._status.active:
            logger.warning("Recording already in progress")
            return str(self._output_path or "")

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"opendesk_recording_{timestamp}.mp4"
        elif not filename.endswith(".mp4"):
            filename += ".mp4"

        self._output_path = self._output_dir / filename

        # Warn if file exists
        if self._output_path.exists():
            logger.warning("Overwriting existing recording: %s", self._output_path)

        # Create encoder
        config = EncoderConfig(
            width=width, height=height, fps=fps, bitrate=bitrate,
            quality=QualityLevel.HIGH,
        )
        self._encoder = VideoEncoder(config)

        # Open the output file for writing
        self._av_container = av.open(str(self._output_path), mode="w")
        rate_int = max(1, int(round(fps)))
        self._av_stream = self._av_container.add_stream("h264", rate=rate_int)
        self._av_stream.width = width
        self._av_stream.height = height
        self._av_stream.pix_fmt = "yuv420p"
        self._av_stream.bit_rate = bitrate
        self._av_stream.options = {"preset": "medium", "profile": "high"}

        self._status = RecordingStatus(
            active=True,
            output_path=str(self._output_path),
            start_time=time.time(),
        )
        self._frame_pts = 0

        logger.info("Recording started: %s (%dx%d, %.1f fps)", self._output_path, width, height, fps)
        return str(self._output_path)

    def write_frame(self, rgb_data: np.ndarray) -> bool:
        """Write a single RGB frame to the recording.

        Parameters
        ----------
        rgb_data : np.ndarray
            RGB uint8 array (H, W, 3).

        Returns
        -------
        bool
            ``True`` if the frame was written successfully.
        """
        if not self._status.active or self._encoder is None:
            return False

        try:
            # Convert numpy RGB to av.VideoFrame
            av_frame = av.VideoFrame.from_ndarray(rgb_data, format="rgb24")
            av_frame.pts = self._frame_pts
            self._frame_pts += 1

            # Encode and mux
            for packet in self._av_stream.encode(av_frame):
                self._av_container.mux(packet)

            self._status.frames_written += 1
            return True

        except Exception as e:
            logger.error("Failed to write frame: %s", e)
            return False

    def stop(self) -> RecordingStatus:
        """Stop recording and finalise the file.

        Returns
        -------
        RecordingStatus
            Final recording status.
        """
        if not self._status.active:
            return self._status

        try:
            # Flush encoder
            if self._av_stream is not None:
                for packet in self._av_stream.encode(None):
                    self._av_container.mux(packet)

            # Close the container
            if self._av_container is not None:
                self._av_container.close()

        except Exception as e:
            logger.error("Error finalising recording: %s", e)

        # Update status
        self._status.active = False
        self._status.duration_sec = time.time() - self._status.start_time

        # Get file size
        if self._output_path and self._output_path.exists():
            self._status.file_size_mb = self._output_path.stat().st_size / (1024 * 1024)

        logger.info(
            "Recording stopped: %d frames, %.1f sec, %.1f MB",
            self._status.frames_written,
            self._status.duration_sec,
            self._status.file_size_mb,
        )

        self._encoder = None
        self._av_container = None
        self._av_stream = None

        return self._status

    def cancel(self) -> None:
        """Cancel recording without saving."""
        self._status.active = False
        if self._output_path and self._output_path.exists():
            self._output_path.unlink(missing_ok=True)
            logger.info("Recording cancelled: %s removed", self._output_path)
        self._encoder = None
