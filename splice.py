import ctypes, os, socket


libc = ctypes.cdll.LoadLibrary('libc.so.6')
splice_syscall = libc.splice

SPLICE_F_NONBLOCK = 0x02
SPLICE_F_MOVE = 0x01

errno_loc = libc.__errno_location
errno_loc.restype = ctypes.POINTER(ctypes.c_int)

def splice(left, right):
    total = 0

    while 1:
        code = splice_syscall(left, 0, right, 0, chunk_size, SPLICE_F_NONBLOCK | SPLICE_F_MOVE)

        if code == -1:
            errno = get_errno()
            error = IOError()
            error.errno = errno
            raise error
        total += code

        if code < chunk_size:
            break

    return total
