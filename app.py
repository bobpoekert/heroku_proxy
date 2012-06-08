from tornado import netutil, ioloop, iostream, httpclient
from functools import partial
import socket
import ctypes
import os
import traceback

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

amount_transferred = 0
host = socket.gethostname()
mixpanel_token = '7fb5000c304c26e32ed1b6744cea1ddd'

def noop(*args):
    return

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

mixpanel_tracker = ioloop.PeriodicCallback(track_throughput, 1000)
mixpanel_tracker.start()

class Request(object):

    def __init__(self, stream, address):
        self.left = stream
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
        host = host.strip()
        print repr(host)
        if host in paths:
            self.left.write(paths[host], self.left.close)
        else:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
                self.right = iostream.IOStream(sock)
                self.right.set_close_callback(self.left.close)
                self.right.connect((socket.gethostbyname(host), 80), self.backend_connected)
            except:
                traceback.print_exc()
                self.left.write(not_found_response, self.left.close)
                self.right.close()

    def backend_connected(self):
        print 'backend connected'
        print repr(self.prefix)
        self.right.write(self.prefix, self.get_headers)

    def get_headers(self):
        print 'get headers'
        self.left.read_until(r'\r\n\r\n', self.proxy_headers)

    def proxy_headers(self, headers):
        print repr(headers)
        self.right.write(headers, self.send_cors)

    def send_cors(self):
        print 'sending cors'
        self.right.write(r'\r\nAccess-Control-Allow-Origin: *\r\n\r\n', self.start)

    def start(self):
        print 'started'
        self.right.writing = const(True)
        self.right._handle_write = self.set_right_ready
        self.left.reading = const(True)
        self.left._handle_read = self.set_left_ready

    def set_right_ready(self):
        self.right_ready = True
        if self.left_ready:
            self.shunt()

    def set_left_ready(self):
        self.left_ready = True
        if self.right_ready:
            self.shunt()

    def shunt(self):
        global amount_transferred
        self.prefix = None

        code = splice(self.left.socket.fileno(), None,
                self.right.socket.fileno(), None,
                4096, SPLICE_F_NONBLOCK)

        if code == 0 or code == -1:
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

