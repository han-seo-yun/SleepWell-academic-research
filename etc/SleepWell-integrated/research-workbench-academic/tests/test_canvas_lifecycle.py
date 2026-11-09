import tkinter as tk
import unittest
import weakref

from matplotlib.figure import Figure
from researchbench.canvas import ResearchCanvas


class CanvasLifecycleTests(unittest.TestCase):
    def test_destroy_releases_photo_and_pending_draw_on_tk_thread(self):
        root = tk.Tk()
        root.withdraw()
        try:
            host = tk.Frame(root)
            canvas = ResearchCanvas(Figure(), master=host)
            photo = weakref.ref(canvas._tkphoto)
            canvas.draw_idle()
            self.assertIsNotNone(canvas._idle_draw_id)
            host.destroy()
            self.assertIsNone(canvas._tkphoto)
            self.assertIsNone(canvas._idle_draw_id)
            self.assertIsNone(photo())
            root.update_idletasks()
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
