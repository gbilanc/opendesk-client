"""
Tests for screen capture and video codec modules.
"""

from __future__ import annotations

import numpy as np

from opendesk.core.screen_capture import compute_dirty_region, frame_diff_ratio
from opendesk.core.video_codec import EncoderConfig, QualityLevel, VideoDecoder, VideoEncoder


class TestFrameDifferencing:
    def test_identical_frames(self) -> None:
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        b = a.copy()
        assert frame_diff_ratio(b, a) == 0.0

    def test_completely_different(self) -> None:
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        b = np.ones((100, 100, 3), dtype=np.uint8) * 255
        assert frame_diff_ratio(b, a) == 1.0

    def test_no_previous_frame(self) -> None:
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        assert frame_diff_ratio(a, None) == 1.0

    def test_partial_change(self) -> None:
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        b = np.zeros((100, 100, 3), dtype=np.uint8)
        b[25:75, 25:75] = (128, 128, 128)
        ratio = frame_diff_ratio(b, a)
        assert 0.2 < ratio < 0.3  # ~25% of pixels changed

    def test_dirty_region(self) -> None:
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        b = np.zeros((100, 100, 3), dtype=np.uint8)
        b[30:70, 20:80] = (200, 200, 200)
        region = compute_dirty_region(b, a)
        assert region is not None
        x0, y0, x1, y1 = region
        assert x0 <= 20
        assert y0 <= 30
        assert x1 >= 80
        assert y1 >= 70

    def test_no_dirty_region(self) -> None:
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        b = a.copy()
        assert compute_dirty_region(b, a) is None


class TestVideoCodec:
    def test_encoder_initialisation(self) -> None:
        config = EncoderConfig(width=320, height=180, fps=15, bitrate=200_000)
        enc = VideoEncoder(config)
        assert enc.config.width == 320
        assert enc.config.height == 180
        enc.release()

    def test_encode_frame(self) -> None:
        config = EncoderConfig(width=320, height=180, fps=15, bitrate=200_000)
        enc = VideoEncoder(config)

        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        frame[50:130, 100:220] = (200, 100, 50)

        packets = enc.encode(frame)
        assert len(packets) >= 1
        assert len(packets[0].data) > 0
        assert packets[0].is_keyframe  # first frame is always keyframe
        assert packets[0].width == 320
        assert packets[0].height == 180
        enc.release()

    def test_encode_decode_roundtrip(self) -> None:
        config = EncoderConfig(width=320, height=180, fps=15, bitrate=200_000)
        enc = VideoEncoder(config)
        dec = VideoDecoder()

        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        frame[50:130, 100:220] = (200, 100, 50)

        packets = enc.encode(frame)
        decoded = dec.decode(
            packets[0].data,
            320,
            180,
            is_keyframe=packets[0].is_keyframe,
        )

        assert decoded is not None
        assert decoded.shape == (180, 320, 3)
        assert decoded.dtype == np.uint8

        enc.release()
        dec.release()

    def test_quality_levels(self) -> None:
        config = EncoderConfig(width=320, height=180, fps=15, bitrate=200_000)
        enc = VideoEncoder(config)

        # Test switching quality
        enc.set_quality(QualityLevel.HIGH)
        assert enc.actual_bitrate >= 8_000_000

        enc.set_quality(QualityLevel.LOW)
        assert enc.actual_bitrate <= 1_000_000

        enc.release()

    def test_multiple_frames(self) -> None:
        """Encoding multiple frames produces distinct packets."""
        config = EncoderConfig(width=160, height=90, fps=10, bitrate=100_000)
        enc = VideoEncoder(config)

        frames_data = []
        for i in range(5):
            frame = np.zeros((90, 160, 3), dtype=np.uint8)
            frame[:, :, 0] = i * 50  # vary red channel
            packets = enc.encode(frame)
            frames_data.extend(packets)

        assert len(frames_data) >= 5

        # At least one subsequent frame should be a delta frame
        has_delta = any(not p.is_keyframe for p in frames_data[1:])
        assert has_delta, "Expected at least one delta frame"

        enc.release()

    def test_flush(self) -> None:
        """Flush should return remaining packets."""
        config = EncoderConfig(width=160, height=90, fps=10, bitrate=100_000)
        enc = VideoEncoder(config)

        frame = np.zeros((90, 160, 3), dtype=np.uint8)
        enc.encode(frame)

        flush_packets = enc.flush()
        # Flush may return 0 packets depending on the encoder state
        assert isinstance(flush_packets, list)
        enc.release()


class TestInputInjection:
    def test_platform_factory(self) -> None:
        """The factory should return a backend for the current platform."""
        from opendesk.core.input_injection import create_input_backend

        backend = create_input_backend()
        assert backend is not None
        backend.release()
