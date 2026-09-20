"""Cross-platform per-user companion process lock; standard library only."""
from pathlib import Path
import sys


class InstanceLock:
    def __init__(self, path):
        self.handle = Path(path).open('a+b')
        try:
            if sys.platform == 'win32':
                import msvcrt
                self.handle.seek(0, 2)
                if self.handle.tell() == 0:
                    self.handle.write(b'\0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise

    def close(self):
        if sys.platform == 'win32':
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
