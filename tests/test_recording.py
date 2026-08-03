"""
Tests for screen recording and Wayland capture modules.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from opendesk.core.screen_recorder import RecordingStatus, ScreenRecorder


class TestScreenRecorder:
    def test_initial_state(self) -> None:
        rec = ScreenRecorder(output_dir="/tmp")
        assert not rec.is_recording
        assert rec.status.frames_written == 0

    def test_start_stop_recording(self) -> None:
        rec = ScreenRecorder(output_dir="/tmp")
        path = rec.start(filename="test_recording", width=320, height=180, fps=10)
        assert rec.is_recording
        assert path.endswith(".mp4")
        # Write at least one frame so the file is created
        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        rec.write_frame(frame)
        status = rec.stop()
        assert not status.active

    def test_write_frames(self) -> None:
        rec = ScreenRecorder(output_dir="/tmp")
        rec.start(filename="test_frames", width=320, height=180, fps=10)

        # Write some test frames
        for i in range(15):
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            frame[:, :, 0] = i * 16  # vary red
            result = rec.write_frame(frame)
            assert result

        status = rec.stop()
        assert status.frames_written == 15
        assert status.duration_sec > 0

        # Check file exists and has content
        if status.output_path:
            path = Path(status.output_path)
            assert path.exists()
            assert path.stat().st_size > 1000  # at least 1KB

    def test_cancel_recording(self) -> None:
        rec = ScreenRecorder(output_dir="/tmp")
        path = rec.start(filename="test_cancel", width=160, height=90, fps=5)
        # File doesn't exist until stop is called

        # Write one frame
        frame = np.zeros((90, 160, 3), dtype=np.uint8)
        rec.write_frame(frame)

        rec.cancel()
        assert not rec.is_recording
        # Cancelled file should not exist
        assert not Path(path).exists()

    def test_recording_status(self) -> None:
        import time
        now = time.time()
        status = RecordingStatus(
            active=True,
            output_path="/tmp/test.mp4",
            start_time=now - 5,  # 5 seconds ago
        )
        assert status.active
        assert status.elapsed > 4.0  # should be ~5 seconds

    def test_multiple_recordings(self) -> None:
        """Start/stop twice should work."""
        rec = ScreenRecorder(output_dir="/tmp")

        path1 = rec.start(filename="rec_multi1", width=160, height=90, fps=10)
        rec.write_frame(np.zeros((90, 160, 3), dtype=np.uint8))
        rec.stop()

        path2 = rec.start(filename="rec_multi2", width=160, height=90, fps=10)
        rec.write_frame(np.zeros((90, 160, 3), dtype=np.uint8))
        rec.stop()

        assert path1 != path2


class TestWaylandCapture:
    def test_availability_check(self) -> None:
        from opendesk.core.wayland_capture import WaylandScreenCast
        wsc = WaylandScreenCast()
        # Should return False on most CI/headless systems
        available = wsc.is_available()
        assert isinstance(available, bool)

    def test_session_creation(self) -> None:
        """async test for setup/shutdown."""
        import asyncio

        from opendesk.core.wayland_capture import WaylandScreenCast

        async def _test():
            wsc = WaylandScreenCast()
            if not wsc.is_available():
                return  # skip

            ok = await wsc.setup()
            # On Wayland with portal, this may succeed
            if ok:
                await wsc.shutdown()

        asyncio.run(_test())

    def test_benchmark_encoder(self) -> None:
        """Quick benchmark smoke test."""
        from opendesk.core.benchmark import benchmark_encoder
        result = benchmark_encoder(
            width=320, height=180, bitrate=500_000,
            num_frames=5, warmup=2,
        )
        assert result.width == 320
        assert result.height == 180
        assert result.bitrate == 500_000
        assert result.avg_encode_ms > 0
        assert result.fps > 0
