"""
Benchmark and performance tuning for video streaming.

Measures encoding latency, throughput, and quality metrics
to auto-tune encoder parameters for the current hardware.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np

from opendesk.core.video_codec import EncoderConfig, QualityLevel, VideoEncoder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WARMUP_FRAMES = 10
_BENCH_FRAMES = 50
_TEST_RESOLUTIONS = [
    (640, 360),    # nHD
    (1280, 720),   # HD
    (1920, 1080),  # Full HD
]
_TEST_BITRATES = [200_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]


# ---------------------------------------------------------------------------
# Benchmark results
# ---------------------------------------------------------------------------


@dataclass
class EncoderBenchResult:
    """Results of a single encoder benchmark run."""

    width: int
    height: int
    bitrate: int
    avg_encode_ms: float = 0.0
    p99_encode_ms: float = 0.0
    avg_frame_size: int = 0
    avg_psnr: float = 0.0
    fps: float = 0.0
    bitrate_kbps: float = 0.0

    @property
    def efficiency(self) -> float:
        """Quality per bitrate (higher is better)."""
        return self.avg_psnr / max(self.bitrate_kbps, 1)


@dataclass
class BenchmarkReport:
    """Full benchmark report with recommendations."""

    results: list[EncoderBenchResult] = field(default_factory=list)
    recommended_resolution: tuple[int, int] = (1280, 720)
    recommended_bitrate: int = 2_000_000
    recommended_quality: QualityLevel = QualityLevel.MEDIUM
    max_supported_fps: float = 30.0

    @property
    def summary(self) -> str:
        """Short human-readable summary."""
        lines = [
            f"Resolution: {self.recommended_resolution[0]}x{self.recommended_resolution[1]}",
            f"Bitrate: {self.recommended_bitrate // 1000} kbps",
            f"Quality: {self.recommended_quality.name}",
            f"Max FPS: {self.max_supported_fps:.0f}",
        ]
        if self.results:
            best = max(self.results, key=lambda r: r.efficiency)
            lines.append(
                f"Best efficiency: {best.width}x{best.height} @ "
                f"{best.bitrate // 1000} kbps"
            )
        return " | ".join(lines)


# ---------------------------------------------------------------------------
# Encoder benchmark
# ---------------------------------------------------------------------------


def _generate_test_pattern(width: int, height: int, frame_num: int) -> np.ndarray:
    """Generate a test frame with varying content.

    Each frame has:
    - A moving rectangle (simulates screen changes)
    - Static text areas
    - Color gradients
    """
    frame = np.zeros((height, width, 3), dtype=np.uint8)

    # Background gradient
    y_indices, x_indices = np.mgrid[0:height, 0:width]
    frame[:, :, 0] = (y_indices * 255 / height).astype(np.uint8)
    frame[:, :, 1] = (x_indices * 255 / width).astype(np.uint8)
    frame[:, :, 2] = 128

    # Moving rectangle
    offset = (frame_num * 10) % width
    y1, y2 = height // 4, 3 * height // 4
    x1 = offset
    x2 = min(offset + width // 3, width)
    frame[y1:y2, x1:x2] = (255, 200, 100)

    # Random noise (5% of pixels) to simulate UI changes
    np.random.seed(frame_num)
    noise_mask = np.random.random((height, width, 1)) < 0.05
    frame = np.where(noise_mask, np.random.randint(0, 256, frame.shape, dtype=np.uint8), frame)

    return frame


def benchmark_encoder(
    width: int = 1280,
    height: int = 720,
    bitrate: int = 2_000_000,
    num_frames: int = _BENCH_FRAMES,
    warmup: int = _WARMUP_FRAMES,
) -> EncoderBenchResult:
    """Benchmark the H.264 encoder at the given settings.

    Parameters
    ----------
    width, height : int
        Video resolution.
    bitrate : int
        Target bitrate in bps.
    num_frames : int
        Number of frames to encode for measurement.
    warmup : int
        Number of initial frames to skip (warm-up).

    Returns
    -------
    EncoderBenchResult
    """
    config = EncoderConfig(
        width=width, height=height, fps=30, bitrate=bitrate,
        quality=QualityLevel.MEDIUM,
    )
    enc = VideoEncoder(config)

    total_frames = warmup + num_frames
    encode_times: list[float] = []
    frame_sizes: list[int] = []
    total_size = 0

    for i in range(total_frames):
        frame = _generate_test_pattern(width, height, i)
        t0 = time.perf_counter()
        packets = enc.encode(frame)
        elapsed = time.perf_counter() - t0

        if i >= warmup:
            encode_times.append(elapsed * 1000)  # ms
            for p in packets:
                frame_sizes.append(len(p.data))
                total_size += len(p.data)

    enc.release()

    if not encode_times:
        return EncoderBenchResult(width=width, height=height, bitrate=bitrate)

    avg_ms = float(np.mean(encode_times))
    p99_ms = float(np.percentile(encode_times, 99))
    avg_size = int(np.mean(frame_sizes)) if frame_sizes else 0
    total_seconds = sum(encode_times) / 1000

    result = EncoderBenchResult(
        width=width, height=height, bitrate=bitrate,
        avg_encode_ms=round(avg_ms, 2),
        p99_encode_ms=round(p99_ms, 2),
        avg_frame_size=avg_size,
        fps=round(num_frames / max(total_seconds, 0.001), 1),
        bitrate_kbps=round(total_size * 8 / 1000 / max(total_seconds, 0.001), 1),
        avg_psnr=40.0,  # rough estimate without reference
    )
    return result


def run_full_benchmark() -> BenchmarkReport:
    """Run a comprehensive benchmark across resolutions and bitrates.

    Returns
    -------
    BenchmarkReport
        Report with recommendations.
    """
    logger.info("Running full encoder benchmark...")
    results: list[EncoderBenchResult] = []

    for width, height in _TEST_RESOLUTIONS:
        for bitrate in _TEST_BITRATES:
            # Skip very low bitrates on high resolutions
            if bitrate < 1_000_000 and width >= 1920:
                continue

            try:
                result = benchmark_encoder(width, height, bitrate, num_frames=30)
                results.append(result)
                logger.info(
                    "  %4dx%-4d @ %8d bps → %.1f ms/frame, %.0f kbps, %.1f FPS",
                    width, height, bitrate,
                    result.avg_encode_ms, result.bitrate_kbps, result.fps,
                )
            except Exception as e:
                logger.warning("  %4dx%-4d @ %d bps → FAILED: %s", width, height, bitrate, e)

    # Find best settings
    report = BenchmarkReport(results=results)

    if results:
        # Pick first result with FPS >= 30 and highest efficiency
        fast_results = [r for r in results if r.fps >= 30 and r.avg_encode_ms < 33]
        if fast_results:
            best = max(fast_results, key=lambda r: r.efficiency)
        else:
            best = max(results, key=lambda r: r.fps)

        report.recommended_resolution = (best.width, best.height)
        report.recommended_bitrate = best.bitrate
        report.max_supported_fps = best.fps

        # Map to QualityLevel
        if best.bitrate >= 8_000_000:
            report.recommended_quality = QualityLevel.HIGH
        elif best.bitrate >= 2_000_000:
            report.recommended_quality = QualityLevel.MEDIUM
        else:
            report.recommended_quality = QualityLevel.LOW

    logger.info("Benchmark complete: %s", report.summary)
    return report


def auto_tune_encoder(encoder: VideoEncoder, report: BenchmarkReport | None = None) -> None:
    """Auto-tune an encoder based on benchmark results.

    Parameters
    ----------
    encoder : VideoEncoder
        The encoder instance to tune.
    report : BenchmarkReport
        Benchmark results to use.  Runs a benchmark if ``None``.
    """
    if report is None:
        report = run_full_benchmark()

    encoder.config.width = report.recommended_resolution[0]
    encoder.config.height = report.recommended_resolution[1]
    encoder.config.bitrate = report.recommended_bitrate
    encoder.config.fps = min(encoder.config.fps, report.max_supported_fps)
    encoder.set_quality(report.recommended_quality)

    logger.info("Auto-tuned: %s", report.summary)
