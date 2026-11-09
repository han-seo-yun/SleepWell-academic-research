"""Matplotlib canvas that defers transient zero-sized hidden-tab resize events."""
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


class ResearchCanvas(FigureCanvasTkAgg):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_tk_widget().bind('<Destroy>', self._release_tk_image, add='+')

    def _release_tk_image(self, event):
        # Destroy is delivered on Tk's owning thread. Release the PhotoImage now,
        # rather than let a later background worker collect this canvas cycle.
        if event.widget is not self._tkcanvas:
            return
        for attribute in ('_idle_draw_id', '_event_loop_id'):
            callback = getattr(self, attribute, None)
            if callback:
                try:
                    self._tkcanvas.after_cancel(callback)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
        self._tkphoto = None

    def resize(self, event):
        # Tk sends tiny intermediate geometry while rebuilding/switching pages.
        # Keep the last valid figure size; visible pages live in scrollable panels.
        if event.width < 320 or event.height < 240:
            return
        super().resize(event)
