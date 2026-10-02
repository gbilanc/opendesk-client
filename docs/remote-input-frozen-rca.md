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

#### 1b. Sul path DXGI il cursore non arrivava comunque all'encoder (gap del fix #1)

Il compositing del cursore in `_capture_dxgi` era **raggiunto solo quando
`cam.grab()` restituiva un frame**, ma Desktop Duplication consegna un frame
solo quando la superficie del desktop cambia; il cursore non fa parte della
superficie.  A desktop fermo (tipico subito dopo minimize/close/maximize di
una finestra) `grab()` restituisce `None` → nessun frame → il cursore non
veniva mai compositato → lo stream restava congelato anche con il fix #1.

**Fix:** in `_capture_dxgi` il path `grab() is None` ora:

- traccia l'ultima posizione globale del cursore emessa (`_last_cursor_gpos`);
- se il cursore si è mosso, cattura un frame via GDI (`_capture_mss`) con il
  cursore compositato: il diff frame invia da solo i tile vecchia/nuova
  posizione;
- emette comunque un frame periodico (~1/s, `_DXGI_IDLE_REFRESH_INTERVAL`)
  anche a desktop e cursore fermi, così il flusso resta vivo e le richieste
  di keyframe del client (watchdog / recovery) vengono evase anche in idle.

Questo mantiene il vantaggio di DXGI (niente BitBlt a 30 fps quando non
serve) limitando il refresh idle a ~1 fps, coerente con la policy adattiva
(`_min_fps = 1.0`).

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

## 2. UI Qt dell'host congelata alla chiusura/iconizzazione di una finestra qualsiasi (2026-10-01)

### Sintomo (report utente)

Su Windows, quando il client remoto chiude o iconizza una finestra
qualsiasi sul PC controllato, **la UI Qt dell'host si congela** (più
del freeze-percezione del punto 1: qui è il processo host a bloccarsi).

### Analisi

L'iniezione input remoto girava **sul main thread Qt**
(`_on_relay_message` → `StreamService.inject_mouse` →
`SendInput`/`SetCursorPos`): l'header di `_on_relay_message` loggava
già un warning `SLOW _on_relay_message` per gli stalli > 100 ms.  Quando
il click remoto chiude/iconizza una finestra, il target può entrare in
un loop modale o smettere di rispondere; in quella situazione le API di
input/injection sul main thread degradano la UI dell'host esattamente
quando serve riprendere il controllo.

Meccanismi secondari presidiati con questo fix:

- **Clipboard OLE** (`ClipboardSync._poll_clipboard`, lato client): la
  lettura `mimeData()` è sincrona e può bloccare il main thread se
  l'app proprietaria della clipboard è lenta/hung — tipico quando una
  finestra viene chiusa.  Ora con misura del tempo + backoff
  esponenziale del polling (500 ms → max 5 s) e warning
  `SLOW clipboard read` nel log.
- **Path idle DXGI**: il fallback GDI (`_capture_mss`) richiamato dal
  path `grab() is None` non deve più contare verso gli errori fatali
  del `CaptureWorker` (10 consecutivi → stop pipeline): un hiccup GDI
  transitorio (es. animazione di chiusura finestra) produce solo un
  frame mancante, come `grab()=None`.  Errori loggati a rate 1/s.

### Fix

1. `stream_service.InputInjectionWorker` — thread dedicato FIFO con
   coda bounded (512 eventi, drop-on-full con warning); le API
   `inject_mouse`/`inject_keyboard`/`sync_remote_caps_lock` si limitano
   a fare enqueue (mai bloccanti per la UI).  Fallback inline se lo
   streaming non è attivo.  Il timing SLOW resta loggato dal worker
   (`SLOW input inject`) senza impattare la UI.
2. `clipboard_sync` — backoff anti-freeze sul polling (solo lato
   client, dove ClipboardSync è attivo).
3. `screen_capture` — path idle DXGI tollerante a errori transitori.

### Verifiche

- `tests/test_input_worker.py`: 10 test (ordine FIFO, sopravvivenza a
  eccezioni, drop su coda piena, stop prompto, submit non bloccante con
  API hung, delega reale al worker, fallback inline, backoff clipboard).
- Suite completa: 150 passed, 4 skipped.

## Stato

- Keep-alive PING verso il relay (precedente): verifica 20 min di sessione
  continua senza kick del relay.
- Da riproduzione reale (host Windows + client): al prossimo episodio
  verificare nel log `SLOW input inject` / `SLOW clipboard read` e i
  dump stack dell'`HangWatchdog` per confermare il path esatto.

### Riproduzione guidata (2026-10-01)

`tests/repro_window_close_freeze.py` — scenario end-to-end senza rete:
pipeline con cattura DXGI/MSS reale, flood input remoto (~60 eventi/s
su `InputInjectionWorker`), minimizzazione e chiusura REALE di una
finestra via Win32 (`WM_SYSCOMMAND/SC_MINIMIZE`, `WM_CLOSE`), 4 fasi
(attivo / minimizza / chiudi / statico) con misura del beat del main
thread Qt e conteggio frame inviati.

Risultato su Windows (2 run, 3–6 s per fase):

- main thread **sempre reattivo**: max gap 0 ms in tutte le fasi,
  incluso durante la chiusura/iconizzazione sotto flood input
- stream **vivo anche a desktop statico** (idle DXGI ~1 fps + idle
  keyframe): 27–53 frame nelle fasi statiche
- nessun errore pipeline, nessun dump del watchdog

Il vecchio comportamento (UI congelata + stream morto dopo close) non
è più riproducibile con i fix in pasta.
