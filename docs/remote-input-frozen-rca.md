# RCA — "Sessione connessa ma input remoto morto" dopo chiusura/iconizzazione HostWindow (Windows)

## Sintomo

Con una sessione remota attiva su Windows, quando l'utente remoto chiude o
iconizza la finestra OpenDesk Host sul PC controllato:

- lo schermo remoto resta visibile (stream connesso)
- ma mouse e tastiera sembrano non avere più effetto
- la sessione resta connessa

## Diagnosi (log + test di riproduzione)

### 1. Il cursore non è compositato nei frame catturati (causa percettiva principale)

DXGI Desktop Duplication (`dxcam`) e MSS (GDI BitBlt) **non includono il
cursore** nei pixel catturati.  A schermo statico — che è ciò che accade
subito dopo la chiusura/iconizzazione della finestra host — l'unico
cambiamento visivo è il puntatore, che però non transita mai nell'encode:

- `CaptureWorker` cattura frame identici (diff ratio ≈ 0)
- `EncoderWorker._do_tiles` non trova tile cambiati → **nulla viene inviato**
- il client vede l'ultimo frame reale: immagine congelata

L'input invece arriva e viene iniettato (verificato con test in-process:
`SetCursorPos` raggiunge il target anche dopo `close()` e `showMinimized()`),
ma **non produce alcun feedback visibile** → l'utente remoto conclude che
l'input è morto.

**Fix:** `screen_capture.draw_cursor_on_frame()` — il cursore di sistema
(`GetCursorInfo`, bitmap via `GetIconInfo`+`GetDIBits`, cache per handle,
fallback a puntatore schematizzato) viene compositato sui pixel del frame
in `_capture_dxgi` e `_capture_mss`.  Il diff frame individua da solo le
aree posizione-vecchia/posizione-nuova e invia i tile corrispondenti: i
movimenti del puntatore generano traffico reale e feedback immediato,
senza modifiche al protocollo né al decoder client.

### 2. Spirale della morte del watchdog keyframe (causa aggravante)

Nei log: `Peer requested keyframe (host)` ogni ~6s per 90+ secondi
mentre host catturava/encodava normalmente.  Il watchdog client
(`_keyframe_watchdog` in `relay_client.py`) chiede un keyframe a ogni
tick da 3s quando non decodifica video per >5s:

- l'host risponde con un keyframe 1080p pieno (centinaia di KB)
- se il link è congestionato il keyframe viene droppato dal backpressure
  (`VIDEO_FRAME` **non** è nel `_BACKPRESSURE_BYPASS`)
- il client non lo riceve → lo richiede di nuovo → congestione alimentata
  dai keyframe stessi → immagine congelata per sempre

**Fix:** anti-flood con backoff esponenziale (3→6→12→… max 60s), reset a 3s
al primo video decodificato.  Il link può drenare e il flusso riprendere.

### 3. Ping-pong adaptive quality (churn CPU/latenza)

I log mostravano boost/restore CRF con ricreazione dell'encoder PyAV fino
a 3–4 volte in 10s: spike di latenza sull'encode e spreco di CPU proprio
quando la rete è già stretta.

**Fix:** throttle in `EncoderWorker._try_rebuild_encoder()` — minimo 2s
(`_REBUILD_MIN_INTERVAL`) tra un rebuild e l'altro; la richiesta resta
pendente e viene applicata al prossimo frame utile.

## Verifiche

- Test sintetico encoder-recreate + decoder: decode pulito anche dopo
  `release()`+ricreazione (15/15 frame) → la ricreazione non è la causa.
- Test end-to-end in-process (host + client via relay reale):
  video continua a fluire in tutte le fasi (visibile / close-to-tray /
  desktop fermo / minimized) — con il cursore compositato 67–77 frame/6s
  a mouse in movimento.
- Suite test: 129 passed; i 15 failure sono pre-esistenti (identici senza
  le modifiche; evdev/relay-socket su Windows).

## Stato

- Keep-alive PING verso il relay (precedente): verifica 20 min di sessione
  continua senza kick del relay.
