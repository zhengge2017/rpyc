"""
rpyc plug-in server (threaded or forking)
"""
import sys
import os
import socket
import time
import threading
import errno
import logging
import Queue
from rpyc.core import SocketStream, Channel, Connection
from rpyc.utils.registry import UDPRegistryClient
from rpyc.utils.authenticators import AuthenticationError
from rpyc.lib import safe_import
signal = safe_import("signal")


class ThreadPoolFull(Exception):
    """raised when the ThreadPoolServer's is overloaded (all threads in the 
    thread pool are used)"""
    pass


class Server(object):
    """Base server implementation
    
    :param service: the :class:`service <service.Service>` to expose
    :param hostname: the host to bind to. Default is IPADDR_ANY, but you may 
                     want to restrict it only to ``localhost`` in some setups
    :param ipv6: whether to create an IPv6 or IPv4 socket. The default is IPv4
    :param port: the TCP port to bind to
    :param backlog: the socket's backlog (passed to ``listen()``)
    :param reuse_addr: whether or not to create the socket with the 
                       ``SO_REUSEADDR`` option set. 
    :param authenticator: the :ref:`authenticators` to use. If ``None``, no
                          authentication is performed.
    :param registrar: the :class:`registrar <rpyc.utils.registry.RegistryClient>` 
                      to use. If ``None``, a default 
                      `rpyc.utils.registry.UDPRegistryClient` will be used
    :param auto_register: whether or not to register using the *registrar*.
                          By default, the server will attempt to register only
                          if a registrar was explicitly given. 
    :param protocol_config: the :data:`configuration dictionary <rpyc.core.protocol.DEFAULT_CONFIG>` 
                            that is passed to the RPyC connection
    :param logger: the ``logger`` to use (of the built-in ``logging`` module).
                   If ``None``, a default logger will be created.
    """
    
    def __init__(self, service, hostname = "", ipv6 = False, port = 0, 
            backlog = 10, reuse_addr = True, authenticator = None, registrar = None,
            auto_register = None, protocol_config = {}, logger = None):
        self.active = False
        self._closed = False
        self.service = service
        self.authenticator = authenticator
        self.backlog = backlog
        if auto_register is None:
            self.auto_register = bool(registrar)
        self.protocol_config = protocol_config
        self.clients = set()

        if ipv6:
            if hostname == "localhost" and sys.platform != "win32":
                # on windows, you should bind to localhost even for ipv6
                hostname = "localhost6"
            self.listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        else:
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        if reuse_addr and sys.platform != "win32":
            # warning: reuseaddr is not what you'd expect on windows!
            # it allows you to bind an already bound port, results in 
            # "unexpected behavior"
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.listener.bind((hostname, port))
        sockname = self.listener.getsockname()
        self.host, self.port = sockname[0], sockname[1]

        if logger is None:
            logger = logging.getLogger("%s/%d" % (self.service.get_service_name(), self.port))
        self.logger = logger
        if registrar is None:
            registrar = UDPRegistryClient(logger = self.logger)
        self.registrar = registrar

    def close(self):
        """Closes (terminates) the server and all of its clients. If applicable, 
        also unregisters from the registry server"""
        if self._closed:
            return
        self._closed = True
        self.active = False
        if self.auto_register:
            try:
                self.registrar.unregister(self.port)
            except Exception:
                self.logger.exception("error unregistering services")
        self.listener.close()
        self.logger.info("listener closed")
        for c in set(self.clients):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            c.close()
        self.clients.clear()

    def fileno(self):
        """returns the listener socket's file descriptor"""
        return self.listener.fileno()

    def accept(self):
        """accepts an incoming socket connection (blocking)"""
        while True:
            try:
                sock, addrinfo = self.listener.accept()
            except socket.timeout:
                pass
            except socket.error:
                ex = sys.exc_info()[1]
                if ex[0] == errno.EINTR:
                    pass
                else:
                    raise EOFError()
            else:
                break

        sock.setblocking(True)
        self.logger.info("accepted %s:%s", addrinfo[0], addrinfo[1])
        self.clients.add(sock)
        self._accept_method(sock)

    def _accept_method(self, sock):
        """this method should start a thread, fork a child process, or
        anything else in order to serve the client. once the mechanism has
        been created, it should invoke _authenticate_and_serve_client with
        `sock` as the argument"""
        raise NotImplementedError

    def _authenticate_and_serve_client(self, sock):
        try:
            if self.authenticator:
                addrinfo = sock.getpeername()
                h = addrinfo[0]
                p = addrinfo[1]
                try:
                    sock, credentials = self.authenticator(sock)
                except AuthenticationError:
                    self.logger.info("[%s]:%s failed to authenticate, rejecting connection", h, p)
                    return
                else:
                    self.logger.info("[%s]:%s authenticated successfully", h, p)
            else:
                credentials = None
            try:
                self._serve_client(sock, credentials)
            except Exception:
                self.logger.exception("client connection terminated abruptly")
                raise
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            sock.close()
            self.clients.discard(sock)

    def _serve_client(self, sock, credentials):
        addrinfo = sock.getpeername()
        h = addrinfo[0]
        p = addrinfo[1]
        if credentials:
            self.logger.info("welcome [%s]:%s (%r)", h, p, credentials)
        else:
            self.logger.info("welcome [%s]:%s", h, p)
        try:
            config = dict(self.protocol_config, credentials = credentials)
            conn = Connection(self.service, Channel(SocketStream(sock)),
                config = config, _lazy = True)
            conn._init_service()
            conn.serve_all()
        finally:
            self.logger.info("goodbye [%s]:%s", h, p)

    def _bg_register(self):
        interval = self.registrar.REREGISTER_INTERVAL
        self.logger.info("started background auto-register thread "
            "(interval = %s)", interval)
        tnext = 0
        try:
            while self.active:
                t = time.time()
                if t >= tnext:
                    tnext = t + interval
                    try:
                        self.registrar.register(self.service.get_service_aliases(),
                            self.port)
                    except Exception:
                        self.logger.exception("error registering services")
                time.sleep(1)
        finally:
            if not self._closed:
                self.logger.info("background auto-register thread finished")

    def start(self):
        """Starts the server (blocking). Use :meth:`close` to stop"""
        self.listener.listen(self.backlog)
        self.logger.info("server started on [%s]:%s", self.host, self.port)
        self.active = True
        if self.auto_register:
            t = threading.Thread(target = self._bg_register)
            t.setDaemon(True)
            t.start()
        #if sys.platform == "win32":
        # hack so we can receive Ctrl+C on windows
        self.listener.settimeout(0.5)
        try:
            try:
                while True:
                    self.accept()
            except EOFError:
                pass # server closed by another thread
            except KeyboardInterrupt:
                print("")
                self.logger.warn("keyboard interrupt!")
        finally:
            self.logger.info("server has terminated")
            self.close()


class ThreadedServer(Server):
    """
    A server that spawns a thread for each connection. Works on any platform
    that supports threads.
    
    Parameters: see :class:`Server`
    """
    def _accept_method(self, sock):
        t = threading.Thread(target = self._authenticate_and_serve_client, args = (sock,))
        t.setDaemon(True)
        t.start()


class ThreadPoolServer(Server):
    """This server is threaded like the ThreadedServer but reuses threads so that
    recreation is not necessary for each request. The pool of threads has a fixed
    size that can be set with the 'nbThreads' argument. Otherwise, the default is 20"""
    
    def __init__(self, *args, **kwargs):
        '''Initializes a ThreadPoolServer. In particular, instantiate the thread pool.'''
        # get the number of threads in the pool
        nbthreads = 20
        if 'nbThreads' in kwargs:
            nbthreads = kwargs['nbThreads']
            del kwargs['nbThreads']
        # init the parent
        Server.__init__(self, *args, **kwargs)
        # create a queue where requests will be pending until a thread is ready
        self._client_queue = Queue.Queue(nbthreads)
        # declare the pool as already active
        self.active = True
        # setup the thread pool
        for i in range(nbthreads):
            t = threading.Thread(target = self._authenticate_and_serve_clients, args=(self._client_queue,))
            t.daemon = True
            t.start()
    
    def _authenticate_and_serve_clients(self, queue):
        '''Main method run by the threads of the thread pool. It gets work from the
        internal queue and calls the _authenticate_and_serve_client method'''
        while self.active:
            try:
                sock = queue.get(True, 1)
                self._authenticate_and_serve_client(sock)
            except Queue.Empty:
                # we've timed out, let's just retry. We only use the timeout so that this
                # thread can stop even if there is nothing in the queue
                pass
            except Exception, e:
                # "Caught exception in Worker thread" message
                self.logger.info("failed to serve client, caught exception : %s", str(e))
                # wait a bit so that we do not loop too fast in case of error
                time.sleep(.2)
    
    def _accept_method(self, sock):
        '''Implementation of the accept method : only pushes the work to the internal queue.
        In case the queue is full, raises an AsynResultTimeout error'''
        try:
            # try to put the request in the queue
            self._client_queue.put_nowait(sock)
        except Queue.Full:
            # queue was full, reject request
            raise ThreadPoolFull("server is overloaded")


class ForkingServer(Server):
    """
    A server that forks a child process for each connection. Available on 
    POSIX compatible systems only.
    
    Parameters: see :class:`Server`
    """
    
    def __init__(self, *args, **kwargs):
        if not signal:
            raise OSError("ForkingServer not supported on this platform")
        Server.__init__(self, *args, **kwargs)
        # setup sigchld handler
        self._prevhandler = signal.signal(signal.SIGCHLD, self._handle_sigchld)

    def close(self):
        Server.close(self)
        signal.signal(signal.SIGCHLD, self._prevhandler)

    @classmethod
    def _handle_sigchld(cls, signum, unused):
        try:
            while True:
                pid, dummy = os.waitpid(-1, os.WNOHANG)
                if pid <= 0:
                    break
        except OSError:
            pass
        # re-register signal handler (see man signal(2), under Portability)
        signal.signal(signal.SIGCHLD, cls._handle_sigchld)

    def _accept_method(self, sock):
        pid = os.fork()
        if pid == 0:
            # child
            try:
                try:
                    self.logger.debug("child process created")
                    signal.signal(signal.SIGCHLD, self._prevhandler)
                    self.listener.close()
                    self.clients.clear()
                    self._authenticate_and_serve_client(sock)
                except:
                    self.logger.exception("child process terminated abnormally")
                else:
                    self.logger.debug("child process terminated")
            finally:
                self.logger.debug("child terminated")
                os._exit(0)
        else:
            # parent
            sock.close()

