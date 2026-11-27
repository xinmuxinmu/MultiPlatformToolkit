"""Keep Python's cyclic garbage collection on Qt's GUI thread."""
import gc
from PySide6.QtCore import QObject, QTimer


class GuiGarbageCollector(QObject):
    def __init__(self, application):
        super().__init__(application)
        self.was_enabled = gc.isenabled()
        gc.disable()
        self.timer = QTimer(self)
        self.timer.timeout.connect(gc.collect)
        self.timer.start(8000)
        application.aboutToQuit.connect(self.restore)

    def restore(self):
        self.timer.stop()
        gc.collect()
        if self.was_enabled: gc.enable()


def install_gui_gc(application):
    if not hasattr(application, "_toolkit_gui_gc"):
        application._toolkit_gui_gc = GuiGarbageCollector(application)
