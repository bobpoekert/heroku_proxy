from tornado import netutil, ioloop, iostream, httpclient, stack_context
from functools import partial
import socket
import ctypes
import os, sys
import traceback
import re
import socket_error

libc = ctypes.cdll.LoadLibrary('libc.so.6')
splice_syscall = libc.splice

SPLICE_F_NONBLOCK = 0x02
SPLICE_F_MOVE = 0x01

try:
    chunk_size = os.pathconf('.', os.pathconf_names['PC_PIPE_BUF'])
except:
    print 'pathconf failed'
    import resource
    chunk_size = resource.getpagesize()

header = 'GET /'
opt_header = 'OPTIO'

def make_response(status, body, content_type='text/plain', extra_headers=None, length=True):
    res = 'HTTP/1.1 %s\r\n' % status
    res += 'Server: Bogus\r\n'
    res += 'Connection: close\r\n'
    if content_type:
        res += 'Content-Type: %s\r\n' % content_type
    if length:
        res += 'Content-Length: %d\r\n' % len(body)
    if extra_headers:
        res += extra_headers
    res += '\r\n'
    res += body
    return res

def file_response(fname, cache_forever=False, content_type='text/html'):
    data = open(fname, 'r').read()
    extra_headers = 'Content-Encoding: gzip\r\n'
    if cache_forever:
        extra_headers += 'Cache-Control: max-age=31556926\r\n'
    return make_response('200 OK', data, content_type=content_type, extra_headers=extra_headers)

error_response = make_response('400 Bad Request', 'Invalid Request')
not_found_response = make_response('404 Not Found', 'Not Found')
front_page = file_response('front_page.html.gz')
api_js = file_response('api.js.gz', cache_forever=True, content_type='text/javascript')
iframe = file_response('iframe.html.gz', cache_forever=True)
robots_txt = make_response('200 OK', open('robots.txt', 'r').read(), content_type='text/plain')

def preflight_response(headers):
    return make_response('200 OK', '', content_type=None, extra_headers='\r\n'.join([
        'Access-Control-Allow-Origin: *',
        'Access-Control-Allow-Headers: %s' % headers,
        'Access-Control-Allow-Methods: GET, OPTIONS']))

paths = {
    'favicon.ico':not_found_response,
    '':front_page,
    'iframe.html':iframe,
    'api.js':api_js,
    'robots.txt':robots_txt}

#def debug(fn):
#    def res(*args, **kwargs):
#        print repr(args), repr(kwargs)
#        return fn(*args, **kwargs)
#    return res

def debug(fn):
    return fn

valid_headers = re.compile('^(User-Agent|Connection|Accept.*|Authorization|If\-.*|Pragma|Range|TE|Upgrade):')

amount_read = 0
amount_written = 0

host = socket.gethostname()

def noop(*args):
    return

errno_loc = libc.__errno_location
errno_loc.restype = ctypes.POINTER(ctypes.c_int)

def get_errno():
    return errno_loc().contents.value

def splice(left, right):
    total = 0

    while 1:
        code = splice_syscall(left, 0, right, 0, chunk_size, SPLICE_F_NONBLOCK | SPLICE_F_MOVE)

        if code == -1:
            errno = get_errno()
            socket_error.raise_socket_error(errno)

        total += code

        if code < chunk_size:
            break

    return total

class Request(object):

    def __init__(self, stream, address):
        self.left = stream
        self.source_address = address
        self.right = None
        self.prefix = None

        self.data_available = False

        read, write = os.pipe()

        self.pipe_read = read
        self.pipe_write = write

        self.left.read_bytes(len(header), self.handle_body)

    def handle_body(self, data):
        if data == header:
            self.prefix = data
            self.left.read_until_regex(r'[ /]', self.handle_host)
        elif data == opt_header:
            self.left.read_until('\r\n', self.write_preflight)
        else:
            self.left.write(error_response, self.close_left)

    def write_preflight(self, line):
        if not line:
            self._write_preflight('*')
            return
        if 'Access-Control-Request-Headers:' in line:
            k, headers = line.split(':')
            self._write_preflight(headers)
            return
        self.left.read_until('\r\n', self.write_preflight)

    def _write_preflight(self, origin):
        res = preflight_response(origin)
        self.left.write(res, self.close_left)

    def close_left(self, *args):
        self.left.close()

    def handle_host(self, host):
        if host[-1] == '/':
            host = host[:-1]
        host = re.sub(r'[^a-zA-Z0-9\.\-\_]', '', host)
        self.host = host
        if host in paths:
            self.left.write(paths[host], self.left.close)
        else:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
                self.right = iostream.IOStream(sock)
                self.right.set_close_callback(self.left.close)
                self.left.set_close_callback(self.right.close)
                self.right.write = debug(self.right.write)
                self.right.connect((socket.gethostbyname(host), 80), self.get_header)
            except:
                traceback.print_exc()
                self.left.write(not_found_response, self.left.close)
                self.right.close()

    def get_header(self, data=None):
        if data == '\r\n':
            self.right.write('Host: %s\r\n\r\n' % self.host)
            self.right.read_until('\r\n\r\n', self.proxy_headers)
        else:
            if data:
                if ':' not in data:
                    if data == 'HTTP/1.1\r\n':
                        self.prefix += ' '
                    self.right.write(self.prefix+data)
                elif valid_headers.match(data):
                    self.right.write(data)
            self.left.read_until('\r\n', self.get_header)

    def proxy_headers(self, data):
        self.left.write(data[:-2])
        self.left.write('Access-Control-Allow-Origin: *\r\nAccess-Control-Allow-Headers: *\r\nAccess-Control-Allow-Methods: GET, OPTIONS\r\n\r\n', self.preflush)

    def preflush(self):
        if self.right._read_buffer_size > 0:
            empty_buffer = self.left._write_buffer
            self.left._write_buffer = self.right._read_buffer
            self.right._read_buffer = empty_buffer
            self.right._read_buffer_size = 0
            self.left._write_callback = stack_context.wrap(self.start)
            self.left._handle_write()
            self.left._add_io_state(self.left.io_loop.WRITE)
        else:
            self.start()

    def start(self):
        self.right.reading = self.reading
        self.right._handle_read = self.handle_read
        self.left.writing = self.writing
        self.left._handle_write = self.handle_write

    def closed(self):
        return self.right.closed() or self.left.closed()

    def reading(self):
        return not self.data_available and not self.closed()

    def writing(self):
        return self.data_available and not self.closed()

    def maybe_close(self):
        if self.left.closed():
            self.right.close()
            return True
        elif self.right.closed():
            self.left.close()
            return True

    def handle_read(self):
        global amount_read
        local_read = 0

        if self.maybe_close():
            return

        if not self.data_available:
            try:
                local_read += splice(self.right.socket.fileno(), self.pipe_write)
            except socket_error.EAGAIN:
                return
            except:
                self.left.close()
                self.right.close()
                traceback.print_exc()
                return

        if local_read == 0:
            self.duds += 1
            if self.duds > 100:
                self.right.close()
                self.left.close()
                return
        else:
            self.duds = 0

        amount_read += local_read
        self.data_available = True
        self.handle_write()

    def handle_write(self):
        global amount_written

        if self.maybe_close():
            return

        if self.data_available:
            try:
                amount_written += splice(self.pipe_read, self.left.socket.fileno())
                self.duds = 0
            except socket_error.EAGAIN:
                self.duds += 1
                if self.duds > 100:
                    self.right.close()
                    self.left.close()
                    return
            except:
                self.left.close()
                self.right.close()
                traceback.print_exc()
                return
            self.data_available = False

    def __del__(self):
        try:
            if not self.left.closed():
                self.left.close()
            if not self.right.closed():
                self.right.close()
        except:
            pass
        os.close(self.pipe_read)
        os.close(self.pipe_write)


class Server(netutil.TCPServer):

    def handle_stream(self, stream, address):
        try:
            Request(stream, address)
        except OSError:
            sys.exit(1)

if __name__ == '__main__':
    server = Server()
    server.listen(os.environ.get('PORT', 5000))
    ioloop.IOLoop.instance().start()

