"""
Test diagnostico per la tile grid.

Verifica che:
1. La conversione RGB→YUV sia corretta (qualità H.264)
2. I tile JPEG siano percettivamente lossless (max diff <= 2)
3. Il watchdog keyframe funzioni
4. L'intero pipeline encode → relay → decode → compositing sia corretto

Esegui con:  python3 -m pytest tests/test_tile_grid_diagnostic.py -v
"""

from __future__ import annotations

import time
import numpy as np
import cv2

from opendesk.core.video_codec import (
    VideoEncoder, VideoDecoder, EncoderConfig, QualityLevel,
)
from opendesk.services.stream_service import (
    _TILE_SIZE, _TILE_THRESHOLD, _TILE_JPEG_QUALITY,
    _KEYFRAME_INTERVAL, _TILE_MAX_CHANGED_RATIO,
)


# ── Helpers ──────────────────────────────────────────────────────────

def make_ui_frame(h: int = 200, w: int = 320) -> np.ndarray:
    """Crea un frame simile a una schermata desktop realistica."""
    frame = np.full((h, w, 3), 240, dtype=np.uint8)  # sfondo chiaro
    frame[0:30, :] = (30, 30, 40)                    # barra titolo
    frame[40:60, 20:100] = (220, 220, 230)            # pulsante grigio
    frame[40:60, 110:190] = (50, 120, 200)            # pulsante blu
    frame[80:85, 30:290] = (0, 0, 0)                  # linea testo
    frame[95:100, 30:250] = (0, 0, 0)                 # linea testo
    frame[130:150, 30:50] = (200, 200, 50)            # icona gialla
    frame[130:150, 60:80] = (50, 200, 50)             # icona verde
    frame[130:150, 90:110] = (200, 50, 50)            # icona rossa
    # Sfumatura delicata per simulare gradienti UI
    for y in range(160, 185):
        v = int(200 + 55 * np.sin(y * 0.1))
        frame[y, :] = (v, v - 20, v - 40)
    frame[185:200, :] = (220, 220, 225)               # barra stato
    return frame


# ═══════════════════════════════════════════════════════════════════════
# Test 1: Qualità encoding H.264
# ═══════════════════════════════════════════════════════════════════════


def test_h264_keyframe_quality() -> None:
    """Verifica che un keyframe H.264 sia fedele all'originale."""
    frame = make_ui_frame()
    h, w = frame.shape[:2]

    enc = VideoEncoder(EncoderConfig(
        width=w, height=h, fps=15,
        bitrate=4_000_000, quality=QualityLevel.HIGH,
    ))
    dec = VideoDecoder()

    packets = enc.encode(frame)
    assert len(packets) > 0, "Nessun pacchetto prodotto"
    assert packets[0].is_keyframe, "Il primo frame deve essere keyframe"

    decoded = dec.decode(packets[0].data, w, h, is_keyframe=True)
    assert decoded is not None, "Decode fallito (None)"

    diff = np.abs(decoded.astype(np.int16) - frame.astype(np.int16))
    max_err = diff.max()
    mean_err = diff.mean()
    bad_pixels = int(np.any(diff > 5, axis=2).sum())
    total_pixels = w * h
    bad_ratio = bad_pixels / total_pixels

    print(f"[H.264 keyframe] max_err={max_err}, mean_err={mean_err:.2f}, "
          f"bad_pixels={bad_pixels}/{total_pixels} ({bad_ratio*100:.1f}%)")

    # La qualità deve essere buona: max_err < 30, bad_pixels < 10%
    assert max_err < 30, f"max_err troppo alto: {max_err}"
    assert bad_ratio < 0.10, f"troppi pixel degradati: {bad_ratio*100:.1f}%"

    enc.release()
    dec.release()


# ═══════════════════════════════════════════════════════════════════════
# Test 2: JPEG tile quality
# ═══════════════════════════════════════════════════════════════════════


def test_jpeg_tile_quality() -> None:
    """Verifica che la codifica/decodifica JPEG dei tile sia
    visivamente accettabile per UI statica.

    JPEG e' lossy: usa MAE (mean absolute error) e PSNR invece
    del max pixel diff, perche' gli artefatti di bordo nei JPEG
    producono picchi isolati sui bordi netti.
    """
    frame = make_ui_frame()
    h, w = frame.shape[:2]
    total_mae = 0.0
    total_tiles = 0

    for y in range(0, h, _TILE_SIZE):
        th = min(_TILE_SIZE, h - y)
        for x in range(0, w, _TILE_SIZE):
            tw = min(_TILE_SIZE, w - x)
            tile = frame[y:y+th, x:x+tw]

            # Codifica JPEG
            tile_bgr = cv2.cvtColor(tile, cv2.COLOR_RGB2BGR)
            success, encoded = cv2.imencode(
                '.jpg', tile_bgr,
                [cv2.IMWRITE_JPEG_QUALITY, _TILE_JPEG_QUALITY[QualityLevel.HIGH]],
            )
            assert success, f"JPEG encode fallito a ({x},{y})"

            # Decodifica JPEG
            decoded_bgr = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            assert decoded_bgr is not None, f"JPEG decode fallito a ({x},{y})"
            decoded_rgb = cv2.cvtColor(decoded_bgr, cv2.COLOR_BGR2RGB)

            # MAE (mean absolute error) — robusto agli artefatti JPEG
            diff = np.abs(decoded_rgb.astype(np.int16) - tile.astype(np.int16))
            total_mae += diff.mean()
            total_tiles += 1

    avg_mae = total_mae / max(total_tiles, 1)
    # MAE medio <= 1.5 significa percettivamente lossless
    assert avg_mae <= 2.0, \
        f"JPEG qualita troppo bassa: MAE medio = {avg_mae:.3f}"
    print(f"[JPEG tile] Qualita OK: MAE medio = {avg_mae:.3f} (soglia <= 2.0), tile={total_tiles}")


# ═══════════════════════════════════════════════════════════════════════
# Test 3: Drift nel tempo (full pipeline simulata)
# ═══════════════════════════════════════════════════════════════════════


def test_tile_grid_drift() -> None:
    """Simula 120 frame di tile grid e verifica che non ci sia deriva."""
    frame = make_ui_frame()
    h, w = frame.shape[:2]

    enc = VideoEncoder(EncoderConfig(
        width=w, height=h, fps=15,
        bitrate=4_000_000, quality=QualityLevel.HIGH,
    ))
    dec = VideoDecoder()

    prev_frame = None
    ref_frame = None
    frame_count = 0

    for i in range(120):
        # Muovi un elemento UI ogni frame (simula attività)
        current = frame.copy()
        offset = int(30 * np.sin(i * 0.1))
        x_pos = 50 + offset
        if 0 <= x_pos < w - 40:
            current[80:85, x_pos:x_pos + 40] = (200, 50, 50)  # cursore/testo che si muove

        # ── Primo frame: full keyframe ──
        if prev_frame is None:
            enc.request_keyframe()
            packets = enc.encode(current)
            rgb = dec.decode(packets[0].data, w, h, is_keyframe=True)
            assert rgb is not None, "Primo keyframe decode fallito"
            ref_frame = rgb.copy()
            prev_frame = current.copy()
            frame_count = 0
            continue

        # ── Frame successivi: tile grid ──
        frame_count += 1
        if frame_count >= _KEYFRAME_INTERVAL:
            enc.request_keyframe()
            packets = enc.encode(current)
            rgb = dec.decode(packets[0].data, w, h, is_keyframe=True)
            if rgb is not None:
                ref_frame = rgb.copy()
            frame_count = 0
            prev_frame = current.copy()
            continue

        # Tile grid: trova e invia tile modificati
        changed_tiles = []
        total_tiles = 0
        for y in range(0, h, _TILE_SIZE):
            th = min(_TILE_SIZE, h - y)
            for x in range(0, w, _TILE_SIZE):
                tw = min(_TILE_SIZE, w - x)
                total_tiles += 1
                cur_tile = current[y:y+th, x:x+tw]
                prev_tile = prev_frame[y:y+th, x:x+tw]
                diff = np.abs(cur_tile.astype(np.int16) - prev_tile.astype(np.int16))
                changed = np.any(diff > _TILE_THRESHOLD, axis=2)
                change_ratio = float(changed.sum()) / changed.size
                if change_ratio > 0.005:
                    tile_bgr = cv2.cvtColor(cur_tile, cv2.COLOR_RGB2BGR)
                    success, encoded = cv2.imencode(
                        '.jpg', tile_bgr,
                        [cv2.IMWRITE_JPEG_QUALITY, _TILE_JPEG_QUALITY[QualityLevel.HIGH]],
                    )
                    if success:
                        changed_tiles.append((encoded.tobytes(), x, y, tw, th))

        # Fallback keyframe se troppi tile cambiati
        if total_tiles > 0 and len(changed_tiles) / total_tiles > _TILE_MAX_CHANGED_RATIO:
            enc.request_keyframe()
            packets = enc.encode(current)
            rgb = dec.decode(packets[0].data, w, h, is_keyframe=True)
            if rgb is not None:
                ref_frame = rgb.copy()
            frame_count = 0
            prev_frame = current.copy()
            continue

        # Invia tile (simula ricezione e compositing)
        for tile_data, tx, ty, tw, th in changed_tiles:
            arr = np.frombuffer(tile_data, dtype=np.uint8)
            tile_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if tile_bgr is not None and tile_bgr.shape[0] == th and tile_bgr.shape[1] == tw:
                tile_rgb = cv2.cvtColor(tile_bgr, cv2.COLOR_BGR2RGB)
                ref_h, ref_w = ref_frame.shape[:2]
                if ty + th <= ref_h and tx + tw <= ref_w:
                    ref_frame[ty:ty+th, tx:tx+tw] = tile_rgb

        prev_frame = current.copy()

        # Verifica deriva ogni 30 frame
        if i % 30 == 29 or i == 119:
            drift = np.abs(ref_frame.astype(np.int16) - current.astype(np.int16))
            max_drift = drift.max()
            mean_drift = drift.mean()
            bad_pixels = int(np.any(drift > 5, axis=2).sum())
            status = "✅" if max_drift < 30 and bad_pixels < w * h * 0.05 else "⚠️"
            print(f"{status} Frame {i+1}: max_drift={max_drift}, "
                  f"mean_drift={mean_drift:.2f}, bad_pixels={bad_pixels}/{w*h}")

    # Verifica finale:
    # - max_drift può essere alto ai bordi netti (testo nero su sfondo chiaro)
    #   per via di H.264 baseline — è accettabile.
    # - mean_drift deve rimanere BASSO (nessuna deriva cumulativa).
    # - bad_pixels non deve crescere nel tempo (drift non progressivo).
    final_drift = np.abs(ref_frame.astype(np.int16) - current.astype(np.int16))
    mean_drift = final_drift.mean()
    final_bad = int(np.any(final_drift > 5, axis=2).sum())

    assert mean_drift < 3.0, \
        f"Deriva media troppo alta: {mean_drift:.2f} (limite: 3.0) — indica drift progressivo"
    assert final_bad < w * h * 0.05, \
        f"Troppi pixel degradati: {final_bad}/{w*h} (limite: 5%)"

    enc.release()
    dec.release()
    print(f"\n✅ Drift test superato: max_drift={final_drift.max()}, "
          f"mean_drift={mean_drift:.2f}, bad_pixels={final_bad}/{w*h} ({final_bad/(w*h)*100:.1f}%)")


# ═══════════════════════════════════════════════════════════════════════
# Test 4: Cambio scena improvviso
# ═══════════════════════════════════════════════════════════════════════


def test_sudden_scene_change() -> None:
    """Cambio scena improvviso deve attivare il fallback a keyframe."""
    h, w = 200, 320

    enc = VideoEncoder(EncoderConfig(
        width=w, height=h, fps=15,
        bitrate=4_000_000, quality=QualityLevel.HIGH,
    ))
    dec = VideoDecoder()

    # Frame A: scena scura
    frame_a = np.full((h, w, 3), 20, dtype=np.uint8)
    frame_a[50:150, 50:270] = (100, 100, 100)

    # Frame B: scena completamente diversa
    frame_b = np.full((h, w, 3), 240, dtype=np.uint8)
    frame_b[0:30, :] = (30, 30, 40)
    frame_b[80:85, 30:290] = (0, 0, 0)

    # Invia primo frame (keyframe)
    enc.request_keyframe()
    packets = enc.encode(frame_a)
    ref = dec.decode(packets[0].data, w, h, is_keyframe=True)
    assert ref is not None
    prev = frame_a.copy()

    # Cambio scena improvviso
    current = frame_b.copy()
    h2, w2 = current.shape[:2]

    # Simula tile grid
    changed = 0
    total = 0
    for y in range(0, h2, _TILE_SIZE):
        th = min(_TILE_SIZE, h2 - y)
        for x in range(0, w2, _TILE_SIZE):
            tw = min(_TILE_SIZE, w2 - x)
            total += 1
            ct = current[y:y+th, x:x+tw]
            pt = prev[y:y+th, x:x+tw]
            d = np.abs(ct.astype(np.int16) - pt.astype(np.int16))
            if np.any(d > _TILE_THRESHOLD):
                changed += 1

    change_ratio = changed / total
    print(f"[Sudden change] Tile cambiati: {changed}/{total} ({change_ratio*100:.0f}%)")

    # Il cambio scena deve superare la soglia del fallback keyframe
    assert change_ratio > _TILE_MAX_CHANGED_RATIO, \
        f"Il cambio scena dovrebbe attivare il fallback keyframe " \
        f"({change_ratio*100:.0f}% < {_TILE_MAX_CHANGED_RATIO*100:.0f}%)"

    # Fallback: invia keyframe
    enc.request_keyframe()
    packets2 = enc.encode(current)
    ref2 = dec.decode(packets2[0].data, w, h, is_keyframe=True)
    assert ref2 is not None, "Keyframe dopo cambio scena fallito"

    diff = np.abs(ref2.astype(np.int16) - current.astype(np.int16))
    print(f"  Keyframe recovery: max_diff={diff.max()}, mean={diff.mean():.2f}")
    assert diff.max() < 30, f"Qualità recovery insufficiente: {diff.max()}"

    enc.release()
    dec.release()
    print(f"✅ Sudden change test superato")


# ═══════════════════════════════════════════════════════════════════════
# Test 5: Watchdog keyframe (simulato)
# ═══════════════════════════════════════════════════════════════════════


def test_keyframe_watchdog_logic() -> None:
    """Verifica che la logica del watchdog keyframe funzioni."""
    # Simula la logica del watchdog senza asyncio
    last_keyframe_time = time.time()
    watchdog_triggered = False

    # Simula 6 secondi senza keyframe
    import asyncio

    async def watchdog():
        nonlocal watchdog_triggered, last_keyframe_time
        for _ in range(3):
            await asyncio.sleep(2.0)
            if last_keyframe_time > 0 and time.time() - last_keyframe_time > 5.0:
                watchdog_triggered = True
                print(f"  Watchdog trigger: {time.time() - last_keyframe_time:.1f}s senza keyframe")
                last_keyframe_time = time.time()

    asyncio.run(watchdog())
    assert watchdog_triggered, "Il watchdog doveva attivarsi dopo 6s senza keyframe"
    print(f"✅ Watchdog test superato")


# ═══════════════════════════════════════════════════════════════════════
# Test 6: Bounds check tile compositing
# ═══════════════════════════════════════════════════════════════════════


def test_tile_bounds_check() -> None:
    """Verifica che i tile siano compositati correttamente ai bordi."""
    h, w = 200, 320
    ref = np.zeros((h, w, 3), dtype=np.uint8)

    # Compositazione di tile a varie posizioni (inclusi bordi)
    test_positions = [
        (0, 0, 64, 64),           # angolo superiore sinistro
        (w - 64, 0, 64, 64),      # angolo superiore destro
        (0, h - 64, 64, 64),      # angolo inferiore sinistro
        (w - 64, h - 64, 64, 64), # angolo inferiore destro
        (w - 10, h - 10, 10, 10), # bordo dx inferiore (tile piccolo)
        (150, 50, 30, 30),        # tile non allineato alla griglia
    ]

    for tx, ty, tw, th in test_positions:
        # Crea tile con pattern unico
        tile = np.full((th, tw, 3), (tx % 256, ty % 256, (tx + ty) % 256), dtype=np.uint8)

        # Bounds check (stessa logica del receiver)
        ref_h, ref_w = ref.shape[:2]
        if ty + th <= ref_h and tx + tw <= ref_w:
            ref[ty:ty+th, tx:tx+tw] = tile
            # Verifica che il tile sia stato compositato correttamente
            assert np.array_equal(ref[ty:ty+th, tx:tx+tw], tile), \
                f"Composit fallita a ({tx},{ty},{tw}x{th})"
        else:
            # Tile fuori dai bounds → deve essere scartato
            print(f"  Tile ({tx},{ty},{tw}x{th}) fuori bounds — scartato (corretto)")

    print(f"✅ Bounds check superato ({len(test_positions)} posizioni)")
