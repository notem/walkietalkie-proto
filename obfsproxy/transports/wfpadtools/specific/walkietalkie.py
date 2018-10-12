"""
The module implements Tamaraw WF countermeasure proposed by Wang and Goldberg.
"""
from obfsproxy.transports.wfpadtools import const
from obfsproxy.transports.wfpadtools.wfpad import WFPadTransport

import os
import pickle

import obfsproxy.common.log as logging
from obfsproxy.transports.wfpadtools import histo

from twisted.internet import reactor
from twisted.internet.protocol import Protocol, Factory
from twisted.internet.endpoints import TCP4ServerEndpoint


log = logging.get_obfslogger()


class WalkieTalkieTransport(WFPadTransport):
    """Implementation of the Walkie-Talkie countermeasure.

    It extends the BasePadder by choosing a constant probability distribution
    for time, and a constant probability distribution for packet lengths. The
    minimum time for which the link will be padded is also specified.
    """
    def __init__(self):
        super(WalkieTalkieTransport, self).__init__()

        self._burst_directory = const.WT_BASE_DIR
        self._decoy_directory = const.WT_DECOY_DIR

        # Set constant length for messages
        self._length = const.MPU
        self._lengthDataProbdist = histo.uniform(self._length)

        # padding sequence to be used
        self._pad_seq = []

        # Start listening for URL signals
        self._port = const.WT_PORT
        self._initializeWTListener()

        self._initializeWTState()

    @classmethod
    def register_external_mode_cli(cls, subparser):
        """Register CLI arguments for Walkie-Talkie parameters."""
        subparser.add_argument("--psize",
                               required=False,
                               type=int,
                               help="Length of messages to be transmitted"
                                    " (Default: MTU).",
                               dest="psize")
        subparser.add_argument("--port",
                               required=False,
                               type=int,
                               help="Network port to listen for webpage identification notifications.",
                               dest="port")
        subparser.add_argument("--decoy",
                               required=False,
                               type=str,
                               help="Directory containing webpage decoy burst sequences.",
                               dest="decoy")
        subparser.add_argument("--bursts",
                               required=False,
                               type=str,
                               help="Directory containing webpage burst sequences.",
                               dest="bursts")
        super(WalkieTalkieTransport, cls).register_external_mode_cli(subparser)

    @classmethod
    def validate_external_mode_cli(cls, args):
        """Assign the given command line arguments to local variables."""
        super(WalkieTalkieTransport, cls).validate_external_mode_cli(args)

        if args.psize:
            cls._length = args.psize
        if args.port:
            cls._port = args.port
        if args.decoy:
            cls._decoy_directory = args.decoy
        if args.bursts:
            cls._burst_directory = args.bursts

    def onSessionStarts(self, sessId):
        WFPadTransport.onSessionStarts(self, sessId)

    def _initializeWTListener(self):
        if self.weAreClient:
            self._listener = WalkieTalkieListener(self._port, self)
            log.warning("[walkie-talkie - %s] starting WT listener on port %d", self.end, self._port)
            self._listener.listen()

    def _initializeWTState(self):
        self._burst_count = 0
        self._pad_count = 0
        if self.weAreClient:    # client begins in Talkie mode
            self._talkie = True
        else:                   # bridge begins in Walkie mode
            self._talkie = False

    def receiveSessionPageId(self, id):
        """Handle receiving new session page information (url)"""
        log.debug("[walkie-talkie] received url id for new session, %s", id)
        self._setPadSequence(id)
        # if we are the client, relay the session page information to the bridge's PT
        if self.weAreClient:
            self.sendControlMessage(const.OP_WT_PAGEID, [id])

    def _setPadSequence(self, id):
        """Load the burst sequence and decoy burst sequence for a particular webpage
        then compute the number of padding packets required for each incoming/outgoing burst"""
        rbs = self._loadSequence(id, self._burst_directory)
        dbs = self._loadSequence(id, self._decoy_directory)
        pad_seq = []
        for index, _ in enumerate(dbs):
            if len(rbs) < index:
                break
            pair = (max(0, dbs[index][0] - rbs[index][0]),
                    max(0, dbs[index][1] - rbs[index][1]))
            pad_seq.append(pair)
        self._pad_seq = pad_seq

    def _loadSequence(self, id, directory):
        """Load a burst sequence from a pickle file"""
        seq = []
        fname = os.path.join(directory, id, ".pkl")
        if os.path.exists(fname):
            with open(fname) as fi:
                seq = pickle.load(fi)
        else:
            log.info('[walkie-talkie - %s] unable to load sequence for %s', self.end, id)
        return seq

    def _is_padding(self, data):
        """Check if all messages in some data are padding messages"""
        # Make sure there actually is data to be parsed
        if (data is None) or (len(data) == 0):
            return True
        # Try to extract protocol messages
        msgs = []
        try:
            msgs = self._msgExtractor.extract(data)
        except Exception, e:
            log.exception("[wfpad - %s] Exception extracting "
                          "messages from stream: %s", self.end, str(e))
        for msg in msgs:
            # If any message in data is not a padding packet, return false.
            if not (msg.flags & const.FLAG_PADDING):
                return False
        return True

    def whenReceivedUpstream(self, data):
        """Switch to talkie mode if outgoing packet is first in a new burst
        dont consider padding messages when mode switching"""
        if not self._talkie:
            self._burst_count += 1
            self._pad_count = 0
            self._talkie = True
            log.info('[walkie-talkie - %s] switching to Talkie mode', self.end)

    def whenReceivedDownstream(self, data):
        """Switch to walkie mode if incoming packet is first in a new burst
        dont consider padding messages when mode switching"""
        if self._talkie:
            self._burst_count += 1
            self._pad_count = 0
            self._talkie = False
            log.info('[walkie-talkie - %s] switching to Walkie mode', self.end)

    def sendIgnore(self, paddingLength=None):
        """Overwrite sendIgnore (sendPadding) function so
        as to control when and how many padding messages are sent"""
        if self._talkie:    # only send padding when in Talkie mode
            burst_pair_no = self._burst_count//2
            pad_pair = self._pad_seq[burst_pair_no] if burst_pair_no < len(self._pad_seq) else (0, 0)
            pad_target = pad_pair[0] if self.weAreClient else pad_pair[1]
            if self._pad_count < pad_target:
                WFPadTransport.sendIgnore(self, paddingLength)
                self._pad_count += 1
                log.debug("[walkie-talkie] sent burst padding. count = %d", self._pad_count)


class WalkieTalkieClient(WalkieTalkieTransport):
    """Extend the TamarawTransport class."""

    def __init__(self):
        """Initialize a TamarawClient object."""
        WalkieTalkieTransport.__init__(self)


class WalkieTalkieServer(WalkieTalkieTransport):
    """Extend the TamarawTransport class."""

    def __init__(self):
        """Initialize a TamarawServer object."""
        WalkieTalkieTransport.__init__(self)


class WalkieTalkieListener(object):
    """The walkie-talkie listener listens for incoming connection from the tor crawler/browser
    The crawler/browser should send the url/webpage identifier to this listener when beginning a browsing session
    This allows the proxy to identify what decoy should be used for mold-padding
    """
    class _ServerProtocol(Protocol):
        """
        """
        def __init__(self, factory, transport):
            self._factory = factory
            self._transport = transport

        def connectionMade(self):
            log.warning('[wt-listener]: making connection with crawler')

        def connectionLost(self, reason):
            log.warning('[wt-listener]: lost connection to crawler')

        def dataReceived(self, data):
            if data:
                log.debug('[wt-listener]: received new webpage session notification from crawler')
                self._transport.receiveSessionPageId(data)

    class _ServerFactory(Factory):
        """
        """
        _listener = None

        def __init__(self, listener):
            self._listener = listener

        def buildProtocol(self, addr):
            return self._ServerProtocol(self, self._listener)

    def __init__(self, port, transport):
        self._transport = transport
        self._port = port
        self._ep = TCP4ServerEndpoint(reactor, self._port, interface="127.0.0.1")
        self._crawler = None

    def listen(self):
        try:
            d = self._ep.listen(self._ServerFactory(self))
            d.addCallback(lambda a: self.setCrawler(a))
        except Exception, e:
            log.exception("[walkie-talkie - %s] Error when listening on port %d:", self._transport.end, self._port, e)

    def setCrawler(self, obj):
        self._crawler = obj
