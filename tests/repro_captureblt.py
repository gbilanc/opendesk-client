"""Test BitBlt SRCCOPY con/senza CAPTUREBLT su desktop DWM sotto carico.

Misura la durata di ogni BitBlt mentre le finestre reali del desktop
vengono minimizzate/ripristinate (DWM sotto carico), e salva un PNG di
controllo per verificare che la cattura SRCCOPY-solo sia completa.
"""

from __future__ import annotations

import ctypes
import statistics
import sys
import threading
import time

from PySide6.QtWidgets import QApplication

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000


def measure(mode: str, churn_stop: threading.Event) -> None:
    """BitBlt continuo misurando le durate (mode: 'mss' o 'nocap')."""
    import mss

    # bitblt manuale per controllare i flag
    srcdc = user32.GetWindowDC(0)
    memdc = gdi32.CreateCompatibleDC(srcdc)
    bmi = ctypes.c_buffer(40)
    ctypes.memset(bmi, 0, 40)
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[0] = 40  # biSize
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[1] = 1280  # width
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[2] = -800  # height
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_uint16))[6] = 1  # planes
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_uint16))[7] = 32  # bpp
    bmp = gdi32.CreateDIBSection(
        memdc, bmi, 0, ctypes.byref(ctypes.c_void_p()), None, 0
    )
    gdi32.SelectObject(memdc, bmp)

    flags = SRCCOPY | CAPTUREBLT if mode == "mss" else SRCCOPY
    durations: list[float] = []
    while not churn_stop.is_set():
        t0 = time.perf_counter()
        gdi32.BitBlt(memdc, 0, 0, 1280, 800, srcdc, 0, 0, flags)
        durations.append(time.perf_counter() - t0)
        time.sleep(0.005)  # ~30fps effettivi max

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, srcdc)
    n = len(durations)
    print(
        f"{mode:6s}: n={n:4d}  mediana={statistics.median(durations) * 1000:6.1f}ms  "
        f"p95={sorted(durations)[int(n * 0.95)] * 1000:7.1f}ms  "
        f"max={max(durations) * 1000:7.1f}ms"
    )


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    churn_stop = threading.Event()

    print("--- BitBlt sotto churn DWM (finestre reali) ---")
    churn_stop.clear()
    import subprocess

    # churn in subprocess per isolare
    churn_proc = subprocess.Popen(
        [sys.executable, "tests/debug_window_churn.py", "18"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)

    t1 = threading.Thread(target=measure, args=("mss", churn_stop), daemon=True)
    t1.start()
    time.sleep(8)
    # swap: ferma mss-thread e avvia nocap
    churn_stop.set()
    t1.join(timeout=3)
    churn_stop.clear()
    t2 = threading.Thread(target=measure, args=("nocap", churn_stop), daemon=True)
    t2.start()
    time.sleep(8)
    churn_stop.set()
    t2.join(timeout=3)
    churn_proc.wait(timeout=10)

    # PNG di controllo con SRCCOPY-only
    from PySide6.QtGui import QImage

    srcdc = user32.GetWindowDC(0)
    memdc = gdi32.CreateCompatibleDC(srcdc)
    bmi = ctypes.c_buffer(40)
    ctypes.memset(bmi, 0, 40)
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[0] = 40
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[1] = 1280
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[2] = -800
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_uint16))[6] = 1
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_uint16))[7] = 32
    buf = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(memdc, bmi, 0, ctypes.byref(buf), None, 0)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, 1280, 800, srcdc, 0, 0, SRCCOPY)
    gdi32.GdiFlush()
    img = QImage(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), 1280, 800, 1280 * 4,
                 QImage.Format.Format_ARGB32)
    img.mirrored(False, True).copy().save("bitblt_nocap.png")
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, srcdc)
    print("PNG di controllo: bitblt_nocap.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
