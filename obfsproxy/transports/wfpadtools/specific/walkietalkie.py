"""
The module implements the Walkie-Talkie WF countermeasure proposed by Wang .
"""
from obfsproxy.transports.wfpadtools import const
from obfsproxy.transports.wfpadtools.wfpad import WFPadTransport

import os
import pickle
import time

import obfsproxy.common.log as logging
from obfsproxy.transports.wfpadtools import histo
from obfsproxy.transports.wfpadtools.common import deferLater

from twisted.internet import reactor
from twisted.internet.protocol import Protocol, Factory
from twisted.internet.endpoints import TCP4ServerEndpoint

import struct

log = logging.get_obfslogger()


class WalkieTalkieTransport(WFPadTransport):
    """Implementation of the Walkie-Talkie countermeasure.

    It extends the BasePadder by choosing a constant probability distribution
    for time, and a constant probability distribution for packet lengths. The
    minimum time for which the link will be padded is also specified.
    """
    def __init__(self):
        super(WalkieTalkieTransport, self).__init__()

        self._burst_directory = const.WT_BASE_DIR if "BURST_DIR" not in os.environ else os.environ["BURST_DIR"]
        self._decoy_directory = const.WT_DECOY_DIR if "DECOY_DIR" not in os.environ else os.environ["DECOY_DIR"]

        # Set constant length for messages
        self._length = const.MPU
        self._lengthDataProbdist = histo.uniform(self._length)

        # padding sequence to be used
        self._ref_seq = []
        self._dec_seq = []
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

    def _initializeWTListener(self):
        if self.weAreClient:
            self._listener = WalkieTalkieListener(self._port, self)
            log.warning("[walkie-talkie - %s] starting WT listener on port %d", self.end, self._port)
            self._listener.listen()

    def _initializeWTState(self):
        self._burst_count = 0
        self._pad_count = 0
        self._packets_seen = 0
	
	# next packet should notify server of WT burst start
        self._notify_bridge = False

        # client begins each burst with padding enabled
        # for the server, padding is disabled until the first outgoing packet is seen
        #  this mechanism is best effort to insure server 
        #  does not send bursts' padding as the burst begins
        if self.weAreClient:
            self._active = True
        else:
            self._active = False

    def receiveSessionPageId(self, id):
        """Handle receiving new session page information (url)"""
        log.debug("[walkie-talkie] received url id for new session, %s", id)
        id = id.split("://")[1] if '://' in id else id  # remove protocol text
        self._setPadSequence(id)
        # if we are the client, relay the session page information to the bridge's PT
        if self.weAreClient:
            log.debug("[walkie-talkie - %s] relaying session url to bridge", self.end)
            self.sendControlMessage(const.OP_WT_PAGE_ID, [id])

    def _setPadSequence(self, id):
        """Load the burst sequence and decoy burst sequence for a particular webpage
        then compute the number of padding packets required for each incoming/outgoing burst"""
        self._ref_seq = self._loadSequence(id, self._burst_directory)
        self._dec_seq = self._loadSequence(id, self._decoy_directory)
        log.info('[walkie-talkie - %s] site reference sequence %s', self.end, str(self._ref_seq))
        log.info('[walkie-talkie - %s] site decoy sequence %s', self.end, str(self._dec_seq))
        pad_seq = []
        for index, _ in enumerate(self._dec_seq):
            if index >= len(self._ref_seq):
                real_in = 0
                real_out = 0
            else:
                real_in = self._ref_seq[index][0]
                real_out = self._ref_seq[index][1]
            pair = (max(0, self._dec_seq[index][0] - real_in),
                    max(0, self._dec_seq[index][1] - real_out))
            pad_seq.append(pair)
        self._pad_seq = pad_seq
        log.info('[walkie-talkie - %s] new padding sequence %s', self.end, str(pad_seq))
        self._burst_count = 0

    def _loadSequence(self, id, directory):
        """Load a burst sequence from a pickle file"""
        seq = []
        fname = os.path.join(directory, id+".pkl")
        if os.path.exists(fname):
            with open(fname) as fi:
                seq = pickle.load(fi)
        else:
            log.debug('[walkie-talkie - %s] unable to load sequence for %s from %s', self.end, id, directory)
        return seq

    def whenReceivedUpstream(self, data):
        """enable padding when real outgoing packets are seen"""
        if not self._active:
            self._active = True

    def whenReceivedDownstream(self, data):
        """disable padding when incoming packets are seen"""
        self._packets_seen += 1
        if self._active:
            self._active = False

    def startTalkieBurst(self):
        """Ready for the next walkie-talkie burst.
        1) The PT should iterate the burst count so as to load the correct decoy pair.
        2) num of pad messages sent should be reset to zero"""
        log.info('[walkie-talkie - %s] number of packets seen %s', self.end, self._packets_seen)
        if self._packets_seen > 0:
            self._packets_seen = 0
            self._burst_count += 1
            self._pad_count = 0
            log.debug('[walkie-talkie - %s] next Walkie-Talkie '
                     'burst no.{d}, padding={p}'.format(d=self._burst_count, p=self.getCurrentBurstPaddingTarget()), self.end)
            if self.weAreClient:
                self._active = True
                self._notify_bridge = True
            else:
                self._active = False

    def whenFakeBurstEnds(self):
        """FakeBurstEnd packets are sent by the cooperating node during tail-padding
        When such a packet is received, the receiving node should switch to Talkie mode and begin
        sending the next padding burst"""
        self._pad_count = 0
        self._burst_count += 1
        self._active = True
        log.info('[walkie-talkie - %s] ready to send next fake burst.', self.end)

        pad_target = self.getCurrentBurstPaddingTarget(fakeBurst=True)
        if pad_target > 0:
            self._sendFakeBurst(pad_target)
        else:
            log.debug("[walkie-talkie - %s] no more bursts left in decoy!", self.end)
            self.onEndPadding()

    def _sendFakeBurst(self, pad_target):
        """Send a burst of dummy packets.
        The final packet in the burst is a control message to flag the end of burst"""
        log.debug("[walkie-talkie - %s] send fake burst padding: (%d).", self.end, pad_target)
        while pad_target > 1:
            self.sendIgnore()
            pad_target -= 1
        # the FAKE BURST END control message fills the final packet in the burst
        self.sendControlMessage(const.OP_WT_BURST_END, [])
        
        if self.weAreClient:
            bcount = self._burst_count-1 if self._burst_count-1 >= 0 else 0
            server_pad = self._pad_seq[bcount][1] if bcount < len(self._dec_seq) else 0
            if not server_pad > 0:
                self.whenFakeBurstEnds()

    def getCurrentBurstPaddingTarget(self, fakeBurst=False):
        """Retrieve the number of padding messages that should be sent
        so as to achieve WT mold-padding for the current burst.
        If there are no decoy burst pairs left in the decoy sequence,
        return (0, 0) to indicate that no padding should be done"""
        seq = self._pad_seq if not fakeBurst else self._dec_seq
        bcount = self._burst_count-1 if self._burst_count-1 >= 0 else 0
        pad_pair = seq[bcount] if bcount < len(seq) else (0, 0)
        pad_target = pad_pair[0] if self.weAreClient else pad_pair[1]
        return pad_target

    def flushBuffer(self):
        """Overwrite WFPadTransport flushBuffer.
        In case the buffer is not empty, the buffer is flushed and we send
        these data over the wire. However, if the buffer is empty we immediately
        send the required number of padding messages for the burst.
        """
        dataLen = len(self._buffer)

        # If data buffer is empty and the PT is currently in talkie mode, send padding immediately
        if dataLen <= 0:
            # don't send padding if not in Talkie mode
            if self._active:
                pad_target = self.getCurrentBurstPaddingTarget() - self._pad_count
                log.debug("[walkie-talkie - %s] buffer is empty, send mold padding (%d).", self.end, pad_target)
                while pad_target > 0:
                    self.sendIgnore()
                    pad_target -= 1
            # exit the function without queuing further flushBuffer() calls
            # the next flushBuffer() call will occur when new data enters the buffer from pushData()
            return

        log.debug("[walkie-talkie - %s] %s bytes of data found in buffer."
                  " Flushing buffer.", self.end, dataLen)
        payloadLen = self._lengthDataProbdist.randomSample()

        if self._notify_bridge:
            # INF_LABEL = -1 means we don't pad packets (can be done in crypto layer)
            if payloadLen is const.INF_LABEL:
                payloadLen = const.MPU_CTRL if dataLen > const.MPU_CTRL else dataLen
            msgTotalLen = payloadLen + const.HDR_CTRL_LEN

            flags = const.FLAG_CONTROL | const.FLAG_LAST # | const.FLAG_DATA
            if dataLen > payloadLen:
                self.sendDownstream(self._msgFactory.new(self._buffer.read(payloadLen), 0,
                                                         flags, const.OP_WT_TALKIE_START, ""))
            else:
                paddingLen = payloadLen - dataLen
                self.sendDownstream(self._msgFactory.new(self._buffer.read(payloadLen), paddingLen,
                                                         flags, const.OP_WT_TALKIE_START, ""))
            self._notify_bridge = False
        else:
            # INF_LABEL = -1 means we don't pad packets (can be done in crypto layer)
            if payloadLen is const.INF_LABEL:
                payloadLen = const.MPU if dataLen > const.MPU else dataLen
            msgTotalLen = payloadLen + const.MIN_HDR_LEN

            self.session.consecPaddingMsgs = 0

            # If data in buffer fills the specified length, we just
            # encapsulate and send the message.
            if dataLen > payloadLen:
                self.sendDataMessage(self._buffer.read(payloadLen))

            # If data in buffer does not fill the message's payload,
            # pad so that it reaches the specified length.
            else:
                paddingLen = payloadLen - dataLen
                self.sendDataMessage(self._buffer.read(), paddingLen)
                log.debug("[walkie-talkie - %s] Padding message to %d (adding %d).", self.end, msgTotalLen, paddingLen)

        log.debug("[walkie-talkie - %s] Sent data message of length %d.", self.end, msgTotalLen)
        self.session.lastSndDataDownstreamTs = self.session.lastSndDownstreamTs = time.time()

        # schedule next call to flush the buffer
        dataDelay = self._delayDataProbdist.randomSample()
        self._deferData = deferLater(dataDelay, self.flushBuffer)
        log.debug("[walkie-talkie - %s] data waiting in buffer, flushing again "
                  "after delay of %s ms.", self.end, dataDelay)

    def onSessionEnds(self, sessId):
        """The communication session with the target server has ended.
        Fake bursts must be sent if there remains bursts in the decoy sequence.
        """
        if len(self._buffer) > 0:  # don't end the session until the buffer is empty
            reactor.callLater(0.5, self.onSessionEnds, sessId)
            return

        self.session.is_padding = True
        self._visiting = False

        log.info("[walkie-talkie - %s] - Session has ended! (sessid = %s)", self.end, sessId)
        if self.weAreClient and self.circuit:
            self.session.is_peer_padding = True
            self.sendControlMessage(const.OP_APP_HINT, [self.getSessId(), False])
        self.session.totalPadding = self.calculateTotalPadding(self)

        # the PT should be in Walkie mode if the previous burst was incoming
        # in such a case, the new outgoing (fake) burst should be sent if there are
        # bursts left in the decoy sequence
        self.whenFakeBurstEnds()

    def onEndPadding(self):
        # on conclusion of tail-padding, signal to the crawler that the
        #   trace is over by severing it's connection to the WT listener
        if self.weAreClient:
            self._listener.closeCrawler()
        #super(WalkieTalkieTransport, self).onEndPadding()

    def sendIgnore(self, paddingLength=None):
        """Overwrite sendIgnore (sendPadding) function so
        as to set a hard limit on the number of padding messages are sent per burst"""
        if self._active:    # only send padding when padding is active
            pad_target = self.getCurrentBurstPaddingTarget()
            if self._pad_count < pad_target or not self._visiting:

                # send ignore message (without congestion avoidance)
                if not paddingLength:
                    paddingLength = self._lengthDataProbdist.randomSample()
                    if paddingLength == const.INF_LABEL:
                        paddingLength = const.MPU
                log.debug("[walkie-talkie - %s] Sending ignore message.", self.end)
                self.sendDownstream(self._msgFactory.newIgnore(paddingLength))

                self._pad_count += 1
                log.debug("[walkie-talkie - %s] sent burst padding. running count = %d", self.end, self._pad_count)


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

        """Protocol handles connection establishment, loss, and data received events"""
        def __init__(self, factory, transport):
            self._factory = factory
            self._transport = transport

        def connectionMade(self):
            log.warning('[wt-listener]: making connection with crawler')

        def connectionLost(self, reason):
            log.warning('[wt-listener]: connection to crawler closed')

        def dataReceived(self, data):
            if data:
                command = struct.unpack("<i", data[:4])[0]
                log.debug('[wt-listener]: received opcode {}'.format(command))
                if command == const.WT_OP_PAGE:
                    log.debug('[wt-listener]: received new webpage session notification from crawler')
                    self._transport.receiveSessionPageId(data[4:].decode())
                elif command == const.WT_OP_TALKIE_START:
                    log.debug('[wt-listener]: received talkie start notification from browser')
                    self._transport.startTalkieBurst()
                elif command == const.WT_OP_SESSION_ENDS:
                    log.debug('[wt-listener]: received session end notification from crawler')
                    self._factory._listener.setCrawler(self)
                    self._transport.onSessionEnds(1)

    class _ServerFactory(Factory):
        """Builds protocols for handling incoming connections to WT listener"""
        _listener = None

        def __init__(self, listener):
            self._listener = listener

        def buildProtocol(self, addr):
            return self._listener._ServerProtocol(self, self._listener._transport)

    def __init__(self, port, transport):
        self._transport = transport
        self._port = port
        self._ep = TCP4ServerEndpoint(reactor, self._port, interface="127.0.0.1")
        self._crawler = None

    def listen(self):
        try:
            d = self._ep.listen(self._ServerFactory(self))
        except Exception, e:
            log.exception("[wt-listener - %s] Error when listening on port %d:", self._transport.end, self._port, e)

    def setCrawler(self, connection):
        self._crawler = connection

    def closeCrawler(self):
        if self._crawler:
            self._crawler.transport.loseConnection()
