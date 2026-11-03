"""Same lock API, separate native implementations; never a PID-only lock."""
import os

if os.name != 'nt':
    import fcntl
else:
    import msvcrt
    from .native import lock

    class WindowsLocks:
        LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

        @staticmethod
        def flock(fd, operation):
            if operation not in (6, 8):
                raise ValueError('只支持非阻塞排他锁。')
            lock(msvcrt.get_osfhandle(fd), unlock=operation == 8)

    fcntl = WindowsLocks()
