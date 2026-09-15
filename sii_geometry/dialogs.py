from __future__ import annotations

from pathlib import Path


def select_directory(title: str, initial: Path | None = None) -> Path | None:
    """Abre el selector nativo; devuelve None si se cancela o no hay interfaz."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(
                parent=root,
                title=title,
                initialdir=str(initial) if initial and initial.exists() else None,
                mustexist=False,
            )
        finally:
            root.destroy()
        return Path(selected) if selected else None
    except Exception:
        return None
