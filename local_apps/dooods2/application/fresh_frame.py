"""Read the newest frame without blocking the caller on capture cleanup."""

import threading


class FreshestFrame(threading.Thread):
    """Own a capture on one thread; never release it concurrently with read()."""

    def __init__(self, capture, name="FreshestFrame", on_release=None):
        if not capture.isOpened():
            raise ValueError("Capture is not open")
        self.capture = capture
        self._on_release = on_release
        self.cond = threading.Condition()
        self.running = False
        self.frame = None
        self.latestnum = 0
        self.callback = None
        # A native backend read may never return. It must not keep Python alive.
        super().__init__(name=name, daemon=True)
        self.start()

    def start(self):
        with self.cond:
            self.running = True
        super().start()

    def release(self, timeout=None):
        """Request stop, wake waiting readers, and wait at most timeout seconds.

        The capture is released by its reader thread after read returns. A stuck
        backend cannot be safely force-stopped from another Python thread.
        """
        with self.cond:
            self.running = False
            self.cond.notify_all()
        if threading.current_thread() is not self:
            self.join(timeout=1.0 if timeout is None else max(0.0, timeout))

    def run(self):
        counter = 0
        try:
            while True:
                with self.cond:
                    if not self.running:
                        break
                rv, img = self.capture.read()
                counter += 1
                with self.cond:
                    self.frame = img if rv else None
                    self.latestnum = counter
                    self.cond.notify_all()
                    running = self.running
                if running and self.callback:
                    self.callback(img)
        finally:
            try:
                self.capture.release()
            finally:
                if self._on_release is not None:
                    self._on_release()
                with self.cond:
                    self.running = False
                    self.cond.notify_all()

    def read(self, wait=True, seqnumber=None, timeout=None):
        with self.cond:
            if wait:
                if seqnumber is None:
                    seqnumber = self.latestnum + 1
                seqnumber = max(seqnumber, 1)
                self.cond.wait_for(
                    lambda: self.latestnum >= seqnumber or not self.running,
                    timeout=timeout,
                )
            if not self.running:
                return self.latestnum, None
            return self.latestnum, self.frame
