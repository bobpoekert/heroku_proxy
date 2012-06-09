from tornado import netutil, ioloop, iostream, httpclient
from functools import partial
import socket
import ctypes
import os
import traceback
import re

libc = ctypes.cdll.LoadLibrary('libc.so.6')
splice = libc.splice

SPLICE_F_NONBLOCK = 0x02

header_bytes = len('GET /')

error_response = 'HTTP/1.1 400 Internal Error\r\nServer: Bogus 0\r\nConnection: Close\r\n\r\nInvalid Request'
not_found_response = 'HTTP/1.1 404 Not Found\r\nConnection: Close\r\n\r\nNot Found'

paths = {'favicon.ico':not_found_response, '':not_found_response}

def const(c):
    def res(*args):
        return c
    return res

valid_headers = re.compile('^(User-Agent|Connection|Accept.*):')

amount_transferred = 0
host = socket.gethostname()
mixpanel_token = '7fb5000c304c26e32ed1b6744cea1ddd'

def noop(*args):
    return

errno_loc = libc.__errno_location
errno_loc.restype = ctypes.POINTER(ctypes.c_int)

def get_errno():
    return errno_loc().contents.value

def handle_close(stream, innerfunc=None):
    def callback():
        if stream.error:
            print stream.error
        if innerfunc:
            innerfunc()
    stream.set_close_callback(callback)

import json, time, base64
def track_throughput():
    global amount_transferred
    data = json.dumps({
        'event':'proxy_throughput',
        'properties':{
            'amount':amount_transferred,
            'time':int(time.time()),
            'token':mixpanel_token,
            'host':host}})
    client = httpclient.AsyncHTTPClient()
    client.fetch('http://api.mixpanel.com/track/?data=%s' % base64.b64encode(data), noop)
    amount_transferred = 0

mixpanel_tracker = ioloop.PeriodicCallback(track_throughput, 1800000)
mixpanel_tracker.start()

class Request(object):

    def __init__(self, stream, address):
        self.left = stream
        handle_close(self.left)
        self.source_address = address
        self.right = None
        self.prefix = None

        self.left_ready = False
        self.right_ready = False

        self.left.read_bytes(header_bytes, self.handle_body)

    def handle_body(self, data):
        if data[-1] != '/':
            self.left.write(error_response, stream.close)
            print repr(data)
            return
        self.prefix = data
        self.left.read_until_regex(r'[ /]', self.handle_host)

    def handle_host(self, host):
        if host[-1] == '/':
            host = host[:-1]
        host = re.sub(r'[^a-zA-Z0-9\.\-\_]', '', host)
        print repr(host)
        self.host = host
        if host in paths:
            self.left.write(paths[host], self.left.close)
        else:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
                self.right = iostream.IOStream(sock)
                handle_close(self.right, self.left.close)
                self.right.connect((socket.gethostbyname(host), 80), self.backend_connected)
            except:
                traceback.print_exc()
                self.left.write(not_found_response, self.left.close)
                self.right.close()

    def backend_connected(self):
        print 'backend connected'
        print repr(self.prefix)
        self.right.write(self.prefix, self.get_header)

    def get_header(self, data=None):
        if data == '\r\n':
            self.right.write('Host: %s\r\n' % self.host)
            self.right.write(data)
            self.right.read_until('\r\n\r\n', self.proxy_headers)
        else:
            if data and (':' not in data or valid_headers.match(data)):
                self.right.write(data)
            self.left.read_until('\r\n', self.get_header)

    def proxy_headers(self, data):
        print repr(data)
        self.left.write(data[:-2])
        self.left.write('Access-Control-Allow-Origin: *\r\n\r\n', self.start)

    def start(self):
        print 'started'
        self.right.writing = const(True)
        self.right._handle_write = self.set_right_ready
        self.left.reading = const(True)
        self.left._handle_read = self.set_left_ready

    def set_right_ready(self):
        print 'right ready'
        self.right_ready = True
        if self.left_ready:
            self.shunt()

    def set_left_ready(self):
        print 'left ready'
        self.left_ready = True
        if self.right_ready:
            self.shunt()

    def shunt(self):
        print "shunt"
        global amount_transferred
        self.prefix = None

        code = splice(self.left.socket.fileno(), None,
                self.right.socket.fileno(), None,
                4096, SPLICE_F_NONBLOCK)

        print code

        if code == 0 or code == -1:
            if code == -1:
                print "socket error: %s" % str(get_errno())
            self.right.close()
            self.left.close()
            return

        amount_transferred += code

        if self.left.closed():
            self.right.close()
            return

        if self.right.closed():
            self.left.close()
            return

        self.left_ready = False
        self.right_ready = False

class Server(netutil.TCPServer):

    def handle_stream(self, stream, address):
        Request(stream, address)

if __name__ == '__main__':
    server = Server()
    server.listen(os.environ.get('PORT', 5000))
    ioloop.IOLoop.instance().start()

