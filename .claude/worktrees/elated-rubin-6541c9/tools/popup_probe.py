"""Detect and dump the TumarCSP price popup (title-independent).

Snapshot all windows now, then YOU trigger the popup (run a bid in another
terminal). The popup is identified structurally — a NEW top-level window that
contains an Edit field — so Alt-Tab/tooltip/IME noise is ignored. Dumps the full
control tree (type/text/class/auto_id/rect) so the auto-filler selectors can be
fixed exactly. Paste everything from "=== POPUP WINDOW ===" down.

    python tools/popup_probe.py
"""
from __future__ import annotations

import sys
import time

NOISE_CLASS = {
    "XamlExplorerHostIslandWindow",          # Alt-Tab / Task view
    "Windows.UI.Core.CoreWindow",
    "Windows.UI.Input.InputSite.WindowClass",
    "tooltips_class32", "IME", "MSCTFIME UI", "Shell_TrayWnd",
}
NOISE_TITLE = ("переключение задач", "task switching", "powershell",
               "probe", "program manager", "панель задач")


def _snap(Desktop, backend: str) -> dict:
    out: dict = {}
    try:
        for w in Desktop(backend=backend).windows(top_level_only=True, visible_only=True):
            try:
                out[w.handle] = (w.window_text(), w.class_name(), w)
            except Exception:
                continue
    except Exception:
        pass
    return out


PRICE_NEEDLES = ("ценов", "предлож", "заполн", "price", "баға", "offer")


def _looks_like_popup(w, title: str) -> bool:
    """True if this window is plausibly the price popup. Avoids control_type
    reads (those allocate memory in the target process and are DENIED when Tumar
    runs at a higher privilege — WinError 5). Uses title + class_name only."""
    low = (title or "").lower()
    if any(n in low for n in PRICE_NEEDLES):
        return True
    try:
        # plain child enum reads class_name (GetClassName, no remote memory)
        for c in w.descendants():
            try:
                if "edit" in (c.class_name() or "").lower():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _dump(title: str, cls: str, w) -> None:
    print(f"\n=== POPUP WINDOW  title={title!r}  class={cls!r} ===")
    try:
        print("  window rect:", w.rectangle())
    except Exception:
        pass

    # uia tree (what the old filler used)
    print("  --- uia descendants ---")
    try:
        for c in w.descendants():
            try:
                ei = c.element_info
                try:
                    rect = str(c.rectangle())
                except Exception:
                    rect = ""
                print(f"  type={getattr(ei, 'control_type', '?')!r} "
                      f"text={c.window_text()!r} "
                      f"class={getattr(ei, 'class_name', '')!r} "
                      f"auto_id={getattr(ei, 'automation_id', '')!r} {rect}")
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        print("  uia descendants() failed:", e)

    # win32 tree (what the NEW filler uses — class_name based, Edit/Button)
    print("  --- win32 descendants (class_name based — this is what matters) ---")
    try:
        from pywinauto import Application
        hwnd = w.handle
        app = Application(backend="win32").connect(handle=hwnd, timeout=2)
        dlg = app.window(handle=hwnd)
        for c in dlg.descendants():
            try:
                try:
                    rect = str(c.rectangle())
                except Exception:
                    rect = ""
                print(f"  class={c.class_name()!r} text={c.window_text()!r} "
                      f"ctrl_id={getattr(c, 'control_id', lambda: '?')()!r} {rect}")
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        print("  win32 descendants() failed:", e)


def main() -> int:
    if sys.platform != "win32":
        print("Windows only.")
        return 2
    try:
        from pywinauto import Desktop
    except Exception as e:  # noqa: BLE001
        print("pip install pywinauto first:", e)
        return 2

    try:
        import ctypes
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        elevated = False
    print(f"ELEVATED (running as Administrator): {elevated}")
    if not elevated:
        print("  !! NOT elevated. If TumarCSP is elevated, the win32 control tree "
              "below will be empty / fail with WinError 5. Relaunch this probe from "
              "an Administrator terminal for a valid test.")
    base = set(_snap(Desktop, "uia")) | set(_snap(Desktop, "win32"))
    print(f"Baseline: {len(base)} windows.")
    print(">>> NOW trigger the popup (run the bid in another terminal). "
          "Waiting up to 150s for a NEW window WITH a text field...")

    noted: set = set()
    for i in range(600):  # ~150s
        cur = {**_snap(Desktop, "win32"), **_snap(Desktop, "uia")}
        for h, (t, c, w) in cur.items():
            if h in base:
                continue
            low = (t or "").lower()
            if c in NOISE_CLASS or any(x in low for x in NOISE_TITLE):
                base.add(h)
                continue
            if _looks_like_popup(w, t):
                _dump(t, c, w)
                return 0
            if h not in noted:
                print(f"  (new window, no text field yet — watching): title={t!r} class={c!r}")
                noted.add(h)
        if i and i % 40 == 0:
            print(f"  ...waiting ({i // 4}s)")
        time.sleep(0.25)

    print("\nNo popup-like window (with a text field) appeared in 150s.")
    print("ALL visible top-level windows right now:")
    for backend in ("uia", "win32"):
        print(f"  [{backend}]")
        for h, (t, c, w) in _snap(Desktop, backend).items():
            print(f"     hwnd={h} title={t!r} class={c!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
