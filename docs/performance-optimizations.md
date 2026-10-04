# Ottimizzazioni performance — OpenDesk Client

Raccolta degli interventi di performance applicati al client dopo la
revisione del 2026-10.  L'obiettivo principale era eliminare i blocchi del
main thread Qt durante la creazione della sessione (vedi `stall_dump.txt`)
e ridurre il lavoro inutile nelle hot-path capture → encode → send.

## Riepilogo

| # | File | Modifica | Impatto |
|---|------|----------|---------|
| 1 | `opendesk/crypto/auth.py` | Parametri Argon2id portati al minimo OWASP (`t=2, m=19 MiB, p=1`) | Hash da ~94 ms a ~47 ms (−50%); memoria −70% |
| 2 | `opendesk/ui/session_info.py`, `opendesk/ui/main_window.py` | Rimosso il **doppio hash** e la sessione orfana nella creazione di "Nuova sessione" | Metà del lavoro Argon2 eliminato; niente `PendingSession` leakate |
| 3 | `opendesk/utils/async_task.py` (nuovo), `opendesk/ui/main_window.py` | Hash Argon2 residuo eseguito **fuori dal main thread** (`run_async`) | UI mai congelata durante la creazione sessione |
| 4 | `opendesk/services/pipeline.py` | `EncoderWorker._do_tiles` a 3 fasi: rileva i tile cambiati, decide keyframe, poi codifica | Nessun JPEG codificato e scartato quando scatta il keyframe (>30% tile) |
| 5 | `opendesk/core/video_codec.py` | `_try_open_codec` con `@lru_cache` | Probe degli encoder HW eseguito una sola volta per processo |
| 6 | `opendesk/network/relay_client.py` | `msg.encode()` spostato sul thread chiamante (`NetworkWorker`) | L'event loop asyncio esegue solo `write`/`drain` |
| 7 | `opendesk/ui/viewer.py` | Copia numpy → bytearray senza `.tobytes()` intermedio | −6 MB di temporaneo per frame @1080p |
| 8 | `opendesk/crypto/auth.py` | `secrets.choice()` al posto di `random` per session ID e OTP | Fix di sicurezza (CSPRNG), nessun impatto perf |

## Dettaglio

### 1–3. Creazione sessione (Argon2)

Il `stall_dump.txt` mostrava il main thread bloccato dentro
`argon2.low_level.hash_secret` per ~2.5 s su Windows durante
`AuthManager.create_session()`.  Tre cause concorrenti:

1. **Parametri troppo costosi.** La configurazione precedente
   (`time_cost=3, memory_cost=65536, parallelism=4`) era ~3× il minimo
   raccomandato OWASP.  Portata a `time_cost=2, memory_cost=19456,
   parallelism=1`.  Gli hash esistenti vengono ri-generati al prossimo
   login riuscito tramite `needs_rehash`.
2. **Doppio hash.** `SessionInfoWidget.refresh_session()` creava un
   `PendingSession` (hash Argon2) e subito dopo `MainWindow` ne creava un
   secondo con la stessa password: due hash sul main thread e una sessione
   orfana in `_pending_sessions`.  Ora il widget genera solo la password;
   la sessione la crea una volta `ConnectionService`.
3. **Hash sul main thread.** L'hash residuo di "Nuova sessione" viene ora
   calcolato su un thread dedicato tramite `run_async()` e consegnato al
   main thread con `QueuedConnection`.  `AuthManager.create_session()`
   accetta un `password_hash` precalcolato per evitare di ricalcolarlo.

`run_async()` mantiene un riferimento forte al `QObject` bridge finché il
risultato non è stato consegnato, per evitare che la GC Python lo raccolga
prima dell'evento queued.

### 4. Tile a 3 fasi (`EncoderWorker._do_tiles`)

Prima l'intero tile grid veniva iterato codificando in JPEG ogni tile
cambiato; se poi più del 30% dei tile era cambiato, si scartava tutto e si
inviava un keyframe.  Ora:

1. si individua l'insieme dei tile cambiati **senza codificare**;
2. si aggiorna il change ratio e, se > `_TILE_MAX_CHANGED_RATIO`, si invia
   il keyframe e si esce;
3. altrimenti si codificano e inviano solo i tile cambiati.

### 5. Cache dei probe codec

`VideoEncoder._choose_codec()` prova fino a 8 encoder hardware con
`av.add_stream()`.  Questa verifica veniva ripetuta a ogni rebuild
dell'encoder (adaptive quality boost/restore) e in `default_codec()`.
La disponibilità degli encoder è statica per processo, quindi
`_try_open_codec` è ora memoizzata con `@lru_cache(maxsize=32)`.

### 6. Serializzazione fuori dall'event loop

`_RelaySession.send_message()` serializza ora il messaggio con
`msg.encode()` sul thread chiamante (tipicamente `NetworkWorker`) e
schedula sul loop asyncio solo `_write_async(data)`, che esegue
`writer.write()` + `await drain()`.  In precedenza il msgpack di frame e
keyframe grandi girava sul thread dell'event loop, allungando i tempi di
drain e introducendo jitter.

### 7. Rendering senza temporaneo

`RemoteViewer.display_frame()` copiava il frame numpy in un bytearray con
`buf[:] = rgb_data.tobytes()`, creando un `bytes` temporaneo grande quanto
il frame (~6 MB @1080p, ~180 MB/s a 30 fps).  Ora la copia avviene
direttamente con `np.frombuffer(buf).reshape(...)[:] = rgb_data`.

## Verifica

- `tests/test_async_task.py` copre: consegna del risultato sul main thread,
  propagazione errori, non-blocking del chiamante, hash precalcolato e
  assenza di sessioni orfane.
- `tests/test_tile_grid_diagnostic.py` verifica qualità e assenza di deriva
  del path tile.
- Suite: `QT_QPA_PLATFORM=offscreen python -m pytest tests/` → 147 passed
  (2 fail ambientali legati a `/dev/uinput`).

## Finding non applicati

| Priorità | Posizione | Nota |
|---|---|---|
| 🟠 | `ui/main_window.py`, `host_app.py` | La sessione iniziale all'avvio è ancora creata in modo sincrono (prima che la finestra sia mostrata): impatto ~47 ms, non percepito. |
| 🟡 | `core/video_codec.py` `VideoDecoder.decode` | `frames[-1].to_rgb().to_ndarray()` crea un `VideoFrame` intermedio; `to_ndarray(format="rgb24")` risparmierebbe una conversione. |
| 🟡 | `core/screen_capture.py` `compute_dirty_region` | `np.argwhere` alloca un array per pixel cambiato; `cv2.boundingRect` sarebbe più efficiente (funzione attualmente non usata in hot-path). |
| 🔵 | `ui/viewer.py` | Il ring buffer copia ancora il frame; il decoder produce array nuovi, quindi si potrebbe referenziare direttamente. |
