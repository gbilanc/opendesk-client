# Ottimizzazione RAM — opendesk-host (Windows)

Analisi e interventi per ridurre l'eccessivo uso di RAM della versione
Windows di `opendesk-host` (dimensione del problema rilevata in fase di
streaming video, dove il workload è dominato da copie full-frame RGB).

## Provvedimenti applicati

| # | File | Modifica | Impatto RAM |
|---|------|----------|-------------|
| 2 | `opendesk/core/screen_capture.py` | `frame_diff_ratio` / `compute_dirty_region` riscritti con `cv2.absdiff` + `cv2.max` (uint8) al posto della differenza `int16` numpy. Nuovo helper condiviso `_changed_mask`. | −4 temporanei full-frame per frame (a 1080p: da ~25 MB a ~12 MB di picco per iterazione); CPU 6× più veloce (misurato: 47 ms → 7.5 ms per frame 1080p) |
| 3 | `opendesk/core/screen_capture.py`, `platform_config.py`, `pyproject.toml` | Nuovo backend `CaptureMethod.DXGI` (Desktop Duplication native via `dxcam`, dipendenza `win32`-only). Su Windows è il metodo preferito se installato; fallback automatico su MSS/GDI. `grab()` restituisce `None` se lo schermo non è cambiato → nessun frame encode inutile. | Elimina la catena GDI BitBlt + copia BGRA→RGB di mss; zero copie mantenute da mss |
| 4 | `opendesk/services/pipeline.py` | `EncoderWorker._prev_frame` non fa più `.copy()` full-frame (keyframe + path tile + shape mismatch): il frame della coda è di proprietà esclusiva del consumer. | −6 MB per keyframe (30/s in movimento, prima: 180 MB/s di allocazioni) |
| 5 | `opendesk/services/stream_service.py` | `AudioManager` / `CameraManager` allocati **lazily** solo al primo uso; `stop_streaming()` li rilascia (`.release()` + `None`). | Opus codec + buffer audio in RAM solo durante la sessione audio |
| 6 | `opendesk/services/pipeline.py` | `EncoderWorker._do_tiles` usa `_changed_mask` (cv2) invece di `np.any(diff > threshold, axis=2)`. | Come step 2 (tile path) |
| 8 | `opendesk/core/file_transfer.py` | Verificato: chunk 64 KiB + file temporaneo, nessun buffering intero file. Nessuna modifica necessaria. | — |
| 9 | `opendesk/host_app.py` | FT-poll timer **adattivo**: 1000 ms da coda vuota, 200 ms durante i trasferimenti; `ChatPanel` distrutto con `deleteLater()` alla chiusura. | Minor wake-up CPU e liberazione RAM del pannello |
| 10 | `opendesk/services/stream_service.py` | A fine `stop_streaming()`, release esplicito audio/camera manager (ricreati on-demand). | RAM restituita al termine di ogni sessione |

## Scelte non applicate (con motivazione)

- **Step 7 (dimensioni code)**: `_FRAME_QUEUE_MAX=3` e `_PKT_QUEUE_MAX=30`
  sono già correttamente bounded — al massimo ~3 frame (18 MB @1080p) e
  una manciata di packetti JPEG. Nessuna modifica.
- **Buffer `dst` preallocati per `cv2.resize` in `CaptureWorker`**: non
  possibile — dopo lo step 4 l'encoder mantiene il riferimento all'array
  (`_prev_frame`); un buffer condiviso lo corromperebbe.

## Misurazione (step 1 / 11)

### Baseline quantificata (benchmark sintetico su Linux, 100 frame 1080p)

| Implementazione diff frame | CPU/frame | Temporanei pico/frame |
|---|---|---|
| OLD (numpy int16) | 47.1 ms | 24.9 MB |
| NEW (cv2 uint8) | **7.5 ms** (−84%) | **12.4 MB** (−50%) |

### Misure su Windows reale

Eseguire su una macchina Windows reale con `tools/measure_ram.py`:

```
pip install psutil
python tools/measure_ram.py --duration 300 --interval 2 --out ram_idle.csv
# (con client connesso e streaming attivo)
python tools/measure_ram.py --duration 300 --interval 2 --out ram_stream.csv
python tools/measure_ram.py --pid <PID> --duration 300 --out ram_post.csv  # dopo deploy
```

Confronta il delta mediano e il picco di Working Set tra baseline e post-deploy.

## Riepilogo atteso dei guadagni

Steady-state in streaming @1080p/30fps:

- Differencing: ~24 MB/s di temporanei numpy in meno
- Keyframe: ~180 MB/s di allocazioni in meno (GC churn, overflow di RSS)
- DXGI: catena GDI mss eliminata (working set del processo ridotto)
- Fine sessione: restituzione di RAM audio/camera/encoder
