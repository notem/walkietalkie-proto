"""
The socks_shim module implements a lightweight shim between the Tor
Browser and the SocksPort to allow extraction of request start/stop times.

Limitations:

 * Currently only SOCKS session start/end times are measured. Things like HTTP
   pipelining will make the estimate inaccurate.

 * The information is only available on the client side. If the server needs
   to continue to send padding till a specified duration past the request end,
   the application request ("visit") termination time must be communicated to
   the server.

 * When multiple bridges are being used concurrently, there is no easy way to
   disambiguate which bridge the application session will end up using.
   Solving this correctly will require modifications to the tor core code, till
   then, only use 1 bridge at a time.
"""
from twisted.internet import reactor
from twisted.internet.protocol import Protocol, Factory
from twisted.internet.endpoints import TCP4ClientEndpoint, TCP4ServerEndpoint

# try to import C parser then fallback in pure python parser.
try:
    from http_parser.parser import HttpParser
except ImportError:
    from http_parser.pyparser import HttpParser

# WFPadTools imports
from obfsproxy.network.buffer import Buffer
import obfsproxy.common.log as logging

log = logging.get_obfslogger()


class _ShimClientProtocol(Protocol):
    _id = None
    _shim = None
    _server = None
    _parser = HttpParser()

    def __init__(self, factory, shim, server):
        self._shim = shim
        self._server = server

    def connectionMade(self):
        self._id = self._shim.notifyConnect()
        self._server._client = self
        self.writeToSocksPort(self._server._buf.read())

    def connectionLost(self, reason):
        self._shim.notifyDisconnect(self._id)

    def dataReceived(self, data):
        self._server.writeToClient(data)

    def writeToSocksPort(self, data):
        if data:
            # check if packet is a request with a URI
            try:
                nparsed = self._parser.execute(data, len(data))
                if nparsed != len(data):
                    raise Exception
                if self._parser.is_message_complete():
                    log.debug("[shim-server] {url} {path} {query} {method}"
                              .format(url=self._parser.get_url(),
                                      path=self._parser.get_path(),
                                      query=self._parser.get_query_string(),
                                      method=self._parser.get_method()))
                    self._shim.notifyURI(self._id,
                                         "".join([self._parser.get_url(), self._parser.get_path()]))
            except Exception:
                self._parser = HttpParser()

            # write out to socks
            self.transport.write(data)


class _ShimClientFactory(Factory):
    _shim = None
    _server = None

    def __init__(self, shim, server):
        self._shim = shim
        self._server = server

    def buildProtocol(self, addr):
        return _ShimClientProtocol(self, self._shim, self._server)


class _ShimServerProtocol(Protocol):
    _shim = None
    _socks_port = None
    _buf = None
    _client = None
    _factory = None

    connector = None

    def __init__(self, factory, shim, socks_port):
        self.factory = factory
        self._shim = shim
        self._socks_port = socks_port
        self._buf = Buffer()

    def connectionMade(self):
        log.warning('[shim]: making client connection')
        ep = TCP4ClientEndpoint(reactor, '127.0.0.1', self._socks_port)
        f = _ShimClientFactory(self._shim, self)
        d = ep.connect(f)
        d.addCallback(self.notifyConnector)
        d.addErrback(self.onConnectFailed)

    def notifyConnector(self, connector):
        self.connector = connector
        self._shim.setConnector(self.connector)

    def onConnectFailed(self, e):
        log.warning('[shim]: client connect failed: %s', e)

    def dataReceived(self, data):
        if self._client:
            self._client.writeToSocksPort(data)
        else:
            self._buf.write(data)

    def writeToClient(self, data):
        if data:
            self.transport.write(data)


class _ShimServerFactory(Factory):
    _shim = None
    _socks_port = None

    def __init__(self, shim, socks_port):
        self._shim = shim
        self._socks_port = socks_port

    def buildProtocol(self, addr):
        return _ShimServerProtocol(self, self._shim, self._socks_port)


class _ShimTestServerProtocol(Protocol):
    _shim = None

    def __init__(self, factory, shim):
        self._shim = shim

    def connectionMade(self):
        self._id = self._shim.notifyConnect()

    def connectionLost(self, reason):
        self._shim.notifyDisconnect(self._id)


class _ShimTestFactory(Factory):
    _shim = None

    def __init__(self, shim):
        self._shim = shim

    def buildProtocol(self, addr):
        return _ShimTestServerProtocol(self, self._shim)


class SocksShim(object):
    _id = None
    _port = None
    _socks_port = None

    _observers = None
    session_observer = None

    port_obj = None
    connector = None

    def __init__(self, shim_port=6665, socks_port=6666):
        self._port = shim_port
        self._socks_port = socks_port
        self._observers = []
        self._id = 0
        
    def listen(self):
        self._ep = TCP4ServerEndpoint(reactor, self._port, interface='127.0.0.1')
        if self._socks_port == -1:
            d = self._ep.listen(_ShimTestFactory(self))
        else:
            d = self._ep.listen(_ShimServerFactory(self, self._socks_port))
        d.addCallback(lambda d: self.setEndpoint(d))
        return d

    def setConnector(self, connector):
        self.connector = connector

    def setEndpoint(self, port_obj):
        self.port_obj = port_obj

    def setShimPort(self, new_port):
        self._port = new_port
        
    def setSocksPort(self, new_port):
        self._socks_port = new_port

    def getPort(self):
        return self._port

    def stopListening(self):
        if self.port_obj:
            self.port_obj.stopListening()

    def addSessionObserver(self, session_observer):
        self.session_observer = session_observer

    def notifyEndPadding(self):
        if self.session_observer:
            self.session_observer.paddingEnded()

    def notifyStartPadding(self):
        if self.session_observer:
            self.session_observer.paddingStarted()

    def registerObserver(self, observer):
        self._observers.append(observer)

    def deregisterObserver(self, observer):
        self._observers.remove(observer)

    def isRegistered(self, observer):
        return observer in self._observers

    def notifyURI(self, conn_id, uri):
        log.debug('[shim]: notifyURI: id=%d', conn_id)
        for o in self._observers:
            o.onURI(self._id, conn_id, uri)

    def notifyConnect(self):
        self._id += 1
        log.debug('[shim]: notifyConnect: id=%d', self._id)
        for o in self._observers:
            o.onConnect(self._id)
        return self._id

    def notifyDisconnect(self, conn_id):
        log.debug('[shim]: notifyDisconnect: id=%d', conn_id)
        for o in self._observers:
            o.onDisconnect(conn_id)

_instance = None


def new(shim_port=6665, socks_port=6666):
    global _instance
    if _instance:
        raise RuntimeError('SOCKS shim already running')
    _instance = SocksShim(shim_port, socks_port)


def get():
    global _instance
    if _instance is None:
        return None
    return _instance
