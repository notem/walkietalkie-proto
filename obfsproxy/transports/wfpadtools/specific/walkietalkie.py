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
from obfsproxy.transports.wfpadtools.session import Session

from twisted.internet import reactor
from twisted.internet.protocol import Protocol, Factory
from twisted.internet.endpoints import TCP4ServerEndpoint


import struct

log = logging.get_obfslogger()

RELAY_DELAY_TIME = 300     # 99th percentile of IBAT for Wang's HD data is 80ms
CLIENT_DELAY_TIME = 300     # 99th percentile of IBAT for Wang's HD data is 4ms

class WalkieTalkieTransport(WFPadTransport):
    """Implementation of the Walkie-Talkie countermeasure.

    It extends the BasePadder by choosing a constant probability distribution
    for time, and a constant probability distribution for packet lengths. The
    minimum time for which the link will be padded is also specified.
    """
    def __init__(self):
        super(WalkieTalkieTransport, self).__init__()

        self._decoy_directory = const.WT_DECOY_DIR if "DECOY_DIR" not in os.environ else os.environ["DECOY_DIR"]
        self._decoy_sequence = []#[10 for _ in range(20)]

        # Set constant length for messages
        self._length = const.MPU
        self._lengthDataProbdist = histo.uniform(self._length)

        self._burstTimeoutHisto = histo.uniform(0.5)

        self._time_of_last_pkt = 0

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
            log.warning("[walkietalkie - %s] starting WT listener on port %d", self.end, self._port)
            self._listener.listen()

    def _initializeWTState(self):
        self._burst_count = 0
        self._pad_count = 0
        self._packets_seen = 0
        self._queue_session_end_notification = False
        self._queue_session_start_notification = False
        self.flushbuf_id = 0

        # next packet should notify server of WT burst start
        #self._notify_bridge = False

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
        log.debug("[walkietalkie] received url id for new session, %s", id)
        id = id.split("://")[1] if '://' in id else id  # remove protocol text
        self._setPadSequence(id)
        # if we are the client, relay the session page information to the bridge's PT
        if self.weAreClient:
            log.debug("[walkietalkie - %s] relaying session url to bridge", self.end)
            self.sendControlMessage(const.OP_WT_PAGE_ID, [id])
        #self._decoy_sequence = [(10, 10) for _ in range(30)]

    def _setPadSequence(self, id):
        """Load the burst sequence and decoy burst sequence for a particular webpage
        then compute the number of padding packets required for each incoming/outgoing burst"""
        self._decoy_sequence = self._loadSequence(id, self._decoy_directory)
        log.info('[walkietalkie - %s] site decoy sequence %s', self.end, str(self._decoy_sequence))
        self._burst_count = 0

    def _loadSequence(self, id, directory):
        """Load a burst sequence from a pickle file"""
        seq = []
        try:
            fname = os.path.join(directory, id+".pkl")
            if os.path.exists(fname):
                with open(fname) as fi:
                    seq = pickle.load(fi)
            else:
                log.debug('[walkietalkie - %s] sequence not found for %s from %s', self.end, id, directory)
        except:
            log.debug('[walkietalkie - %s] unable to load sequence for %s from %s', self.end, id, directory)
        return seq

    def getBurstTarget(self):
        """Retrieve the number of padding messages that should be sent
        so as to achieve WT mold-padding for the current burst.
        If there are no decoy burst pairs left in the decoy sequence,
        return (0, 0) to indicate that no padding should be done"""
        seq = self._decoy_sequence
        pad_pair = seq[self._burst_count] if self._burst_count < len(seq) else (0, 0)
        pad_target = pad_pair[0] if self.weAreClient else pad_pair[1]
        return pad_target

    ##def whenReceivedUpstream(self, data):
    ##    """enable padding when real outgoing packets are seen"""
    ##    if not self._active:
    ##        self._active = True

    ##def whenReceivedDownstream(self, data):
    ##    """disable padding when incoming packets are seen"""
    ##    self._packets_seen += 1
    ##    if self._active:
    ##        self._active = False

    #def startTalkieBurst(self):
    #    """Ready for the next walkie-talkie burst.
    #    1) The PT should iterate the burst count so as to load the correct decoy pair.
    #    2) num of pad messages sent should be reset to zero"""
    #    log.info('[walkie-talkie - %s] number of packets seen %s', self.end, self._packets_seen)
    #    if self._packets_seen > 0:
    #        self._packets_seen = 0
    #        self._burst_count += 1
    #        self._pad_count = 0
    #        log.debug('[walkie-talkie - %s] next Walkie-Talkie '
    #                 'burst no.{d}, padding={p}'.format(d=self._burst_count, p=self.getCurrentBurstPaddingTarget()), self.end)
    #        if self.weAreClient:
    #            self._active = True
    #            self._notify_bridge = True
    #        else:
    #            self._active = False
    def checkTimeout(self):
        return CLIENT_DELAY_TIME if self.weAreClient() else RELAY_DELAY_TIME < int(time.time()*1000) - self._time_of_last_pkt

    def updateTimeout(self):
        self._time_of_last_pkt = int(time.time() * 1000)

    def whenReceivedUpstream(self, data):
        """Template method for child WF defense transport."""
        self.updateTimeout()

    def whenReceivedDownstream(self, data):
        """Template method for child WF defense transport."""
        self.updateTimeout()

    def whenBurstEnds(self):
        """BurstEnd packets are sent by the cooperating node during tail-padding
        When such a packet is received, the receiving node should switch to Talkie mode and begin
        sending the next padding burst"""
        self._pad_count = 0
        self._active = True
        log.info('[walkietalkie - %s] ready to send next burst.', self.end)

        pad_target = self.getBurstTarget()
        if pad_target <= 0 and self.session.is_padding:
            log.debug("[walkietalkie - %s] no more bursts left in decoy!", self.end)
            self.onEndPadding()

        if self.weAreClient:
            delay = CLIENT_DELAY_TIME * 10
        else:
            delay = RELAY_DELAY_TIME * 10
        #if not self._deferData or (self._deferData and self._deferData.called):
        if self._deferData and self._deferData.called:
            self._deferData.cancel()
        self._deferData = deferLater(delay, self.flushBuffer, id=self.flushbuf_id+1)
        self.flushbuf_id += 1

    #def _sendFakeBurst(self, pad_target):
    #    """Send a burst of dummy packets.
    #    The final packet in the burst is a control message to flag the end of burst"""
    #    log.debug("[walkie-talkie - %s] send fake burst padding: (%d).", self.end, pad_target)
    #    while pad_target > 1:
    #        self.sendIgnore()
    #        pad_target -= 1
    #    # the FAKE BURST END control message fills the final packet in the burst
    #    self.sendControlMessage(const.OP_WT_BURST_END, [])
    #
    #    if self.weAreClient:
    #        bcount = self._burst_count-1 if self._burst_count-1 >= 0 else 0
    #        server_pad = self._pad_seq[bcount][1] if bcount < len(self._dec_seq) else 0
    #        if not server_pad > 0:
    #            self.whenBurstEnds()

    ##def flushBuffer(self):
    ##    """Overwrite WFPadTransport flushBuffer.
    ##    In case the buffer is not empty, the buffer is flushed and we send
    ##    these data over the wire. However, if the buffer is empty we immediately
    ##    send the required number of padding messages for the burst.
    ##    """
    ##    dataLen = len(self._buffer)

    ##    # If data buffer is empty and the PT is currently in talkie mode, send padding immediately
    ##    if dataLen <= 0:
    ##        # don't send padding if not in Talkie mode
    ##        if self._active:
    ##            pad_target = self.getCurrentBurstPaddingTarget() - self._pad_count
    ##            log.debug("[walkie-talkie - %s] buffer is empty, send mold padding (%d).", self.end, pad_target)
    ##            while pad_target > 0:
    ##                self.sendIgnore()
    ##                pad_target -= 1
    ##        # exit the function without queuing further flushBuffer() calls
    ##        # the next flushBuffer() call will occur when new data enters the buffer from pushData()
    ##        return

    ##    log.debug("[walkie-talkie - %s] %s bytes of data found in buffer."
    ##              " Flushing buffer.", self.end, dataLen)
    ##    payloadLen = self._lengthDataProbdist.randomSample()

    ##    if self._notify_bridge:
    ##        # INF_LABEL = -1 means we don't pad packets (can be done in crypto layer)
    ##        if payloadLen is const.INF_LABEL:
    ##            payloadLen = const.MPU_CTRL if dataLen > const.MPU_CTRL else dataLen
    ##        msgTotalLen = payloadLen + const.HDR_CTRL_LEN

    ##        flags = const.FLAG_CONTROL | const.FLAG_LAST # | const.FLAG_DATA
    ##        if dataLen > payloadLen:
    ##            self.sendDownstream(self._msgFactory.new(self._buffer.read(payloadLen), 0,
    ##                                                     flags, const.OP_WT_TALKIE_START, ""))
    ##        else:
    ##            paddingLen = payloadLen - dataLen
    ##            self.sendDownstream(self._msgFactory.new(self._buffer.read(payloadLen), paddingLen,
    ##                                                     flags, const.OP_WT_TALKIE_START, ""))
    ##        self._notify_bridge = False
    ##    else:
    ##        # INF_LABEL = -1 means we don't pad packets (can be done in crypto layer)
    ##        if payloadLen is const.INF_LABEL:
    ##            payloadLen = const.MPU if dataLen > const.MPU else dataLen
    ##        msgTotalLen = payloadLen + const.MIN_HDR_LEN

    ##        self.session.consecPaddingMsgs = 0

    ##        # If data in buffer fills the specified length, we just
    ##        # encapsulate and send the message.
    ##        if dataLen > payloadLen:
    ##            self.sendDataMessage(self._buffer.read(payloadLen))

    ##        # If data in buffer does not fill the message's payload,
    ##        # pad so that it reaches the specified length.
    ##        else:
    ##            paddingLen = payloadLen - dataLen
    ##            self.sendDataMessage(self._buffer.read(), paddingLen)
    ##            log.debug("[walkie-talkie - %s] Padding message to %d (adding %d).", self.end, msgTotalLen, paddingLen)

    ##    log.debug("[walkie-talkie - %s] Sent data message of length %d.", self.end, msgTotalLen)
    ##    self.session.lastSndDataDownstreamTs = self.session.lastSndDownstreamTs = time.time()

    ##    # schedule next call to flush the buffer
    ##    dataDelay = self._delayDataProbdist.randomSample()
    ##    self._deferData = deferLater(dataDelay, self.flushBuffer)
    ##    log.debug("[walkie-talkie - %s] data waiting in buffer, flushing again "
    ##              "after delay of %s ms.", self.end, dataDelay)

    def flushBuffer(self, id):
        """Overwrite WFPadTransport flushBuffer so as to behave in half-duplex mode

        Flush buffer only when new upstream packets have not been received for some time and the PT is in talkie mode
        """
        # only consider flushing buffer if in talkie-mode
        # !! one side of PT must always be talkie-mode else connection will hang
        if self._active and id==self.flushbuf_id:
            #if not self.checkTimeout():
            #    delay = CLIENT_DELAY_TIME if self.weAreClient() else RELAY_DELAY_TIME
            #    if len(self._buffer) <= 0:
            #        delay *= 10
            #    log.debug("flush buffer called too soon, delaying again")
            #    self._deferData = deferLater(delay, self.flushBuffer)
            #    return

            dataLen = len(self._buffer)

            log.debug("[walkietalkie - %s] %s bytes of data found in buffer."
                      " Flushing buffer.", self.end, dataLen)
            payloadLen = self._lengthDataProbdist.randomSample()

            # INF_LABEL = -1 means we don't pad packets (can be done in crypto layer)
            if payloadLen is const.INF_LABEL:
                payloadLen = const.MPU if dataLen > const.MPU else dataLen
            msgTotalLen = payloadLen + const.MIN_HDR_LEN

            payload_counts = (dataLen // payloadLen) + 1 if dataLen > 0 else 0
            burst_target = self.getBurstTarget() - 1
            if self._queue_session_start_notification:
                burst_target -= 1
            if self._queue_session_end_notification:
                burst_target -= 1

            # send packets with real payloads
            for i in range(min(payload_counts, burst_target)):
                # If data in buffer fills the specified length, we just
                # encapsulate and send the message.
                if len(self._buffer) > payloadLen:
                    self.sendDataMessage(self._buffer.read(payloadLen))

                # If data in buffer does not fill the message's payload,
                # pad so that it reaches the specified length.
                else:
                    paddingLen = payloadLen - len(self._buffer)
                    self.sendDataMessage(self._buffer.read(), paddingLen)
                    log.debug("[walkietalkie - %s] Padding message to %d (adding %d).", self.end, msgTotalLen, paddingLen)

                log.debug("[walkietalkie - %s] Sent data message of length %d.", self.end, msgTotalLen)

            # send session end notification if applicable
            if self._queue_session_end_notification:
                self.sendControlMessage(const.OP_APP_HINT, [self.getSessId(), False, "session end notification!"])
                payload_counts += 1  # SESSION END control message
                self._queue_session_end_notification = False
            if self._queue_session_start_notification:
                self.sendControlMessage(const.OP_APP_HINT, [self.getSessId(), True, "session start notification!"])
                payload_counts += 1  # SESSION END control message
                self._queue_session_start_notification = False

            # send packets with dummy payloads
            payload_counts += 1  # BURST END control message
            self.session.consecPaddingMsgs = 0
            for j in range(burst_target - payload_counts):
                self.sendIgnore()
            self.sendControlMessage(const.OP_WT_BURST_END, "burst end notification!")

            # set to walkie-mode and iterate burst counter
            self._active = False
            self._burst_count += 1

            self.session.lastSndDataDownstreamTs = self.session.lastSndDownstreamTs = time.time()

            #if len(self._buffer) > 0:
            #    dataDelay = self._delayDataProbdist.randomSample()
            #    self._deferData = deferLater(dataDelay, self.flushBuffer)
            #    log.debug("[wt - %s] data waiting in buffer, flushing again "
            #              "after delay of %s ms.", self.end, dataDelay)
            #else:  # If buffer is empty, generate padding messages.
            #    self.deferBurstPadding('snd')
            #    log.debug("[wt - %s] buffer is empty, pad `snd` burst.", self.end)

    def pushData(self, data):
        """Overwrite WFPadTransport pushData to schedule flushBuffer based on custom timings.
        """
        log.debug("[walkietalkie - %s] Pushing %d bytes of outgoing data.", self.end, len(data))
        if len(data) <= 0:
            log.debug("[walkietalkie - %s] pushData() was called without a reason!", self.end)
            return

        # Cancel existing deferred calls to padding methods to prevent
        # callbacks that remove tokens from histograms
        #deferBurstCancelled, deferGapCancelled = self.cancelDeferrers('snd')

        # Draw delay for data message
        #delay = self._burstTimeoutHisto.randomSample()
        if self.weAreClient:
            delay = CLIENT_DELAY_TIME
        else:
            delay = RELAY_DELAY_TIME

        # Update delay according to elapsed time since last message
        # was sent. In case elapsed time is greater than current
        # delay, we sent the data message as soon as possible.
        #if deferBurstCancelled or deferGapCancelled:
        #    elapsed = self.elapsedSinceLastMsg()
        #    newDelay = delay - elapsed
        #    delay = 0 if newDelay < 0 else newDelay
        #    log.debug("[wfpad - %s] New delay is %s", self.end, delay)
        #
        #    if deferBurstCancelled and hasattr(self._burstHistoProbdist['snd'], "histo"):
        #        self._burstHistoProbdist['snd'].removeToken(elapsed, False)
        #    if deferGapCancelled and hasattr(self._gapHistoProbdist['snd'], "histo"):
        #        self._gapHistoProbdist['snd'].removeToken(elapsed, False)

        # Push data message to data buffer
        self._buffer.write(data)
        log.debug("[walkietalkie - %s] Buffered %d bytes of outgoing data w/ delay %sms", self.end, len(self._buffer), delay)

        # if there is a scheduled flush buffer, cancel and re-schedule
        if self._deferData and self._deferData.called:
            self._deferData.cancel()
        self._deferData = deferLater(delay, self.flushBuffer, id=self.flushbuf_id+1)
        self.flushbuf_id += 1
        log.debug("[walkietalkie - %s] Delay buffer flush %s ms delay", self.end, delay)

    def onSessionEnds(self, sessId):
        """The communication session with the target server has ended.
        Fake bursts must be sent if there remains bursts in the decoy sequence.
        """
        #if len(self._buffer) > 0:  # don't end the session until the buffer is empty
        #    reactor.callLater(0.5, self.onSessionEnds, sessId)
        #    return

        self.session.is_padding = True
        self._visiting = False

        log.info("[walkietalkie - %s] - Session has ended! (sessid = %s)", self.end, sessId)
        if self.weAreClient and self.circuit:
            self.session.is_peer_padding = True
            self._queue_session_end_notification = True
            #self.sendControlMessage(const.OP_APP_HINT, [self.getSessId(), False])
        self.session.totalPadding = self.calculateTotalPadding(self)

        # the PT should be in Walkie mode if the previous burst was incoming
        # in such a case, the new outgoing (fake) burst should be sent if there are
        # bursts left in the decoy sequence
        #self.whenBurstEnds()

    def onEndPadding(self):
        # on conclusion of tail-padding, signal to the crawler that the
        #   trace is over by severing it's connection to the WT listener
        if self.weAreClient:
            self._listener.closeCrawler()
        self.session.is_padding = False
        #super(WalkieTalkieTransport, self).onEndPadding()

    ##def sendIgnore(self, paddingLength=None):
    ##    """Overwrite sendIgnore (sendPadding) function so
    ##    as to set a hard limit on the number of padding messages are sent per burst"""
    ##    if self._active:    # only send padding when padding is active
    ##        pad_target = self.getCurrentBurstPaddingTarget()
    ##        if self._pad_count < pad_target or not self._visiting:

    ##            # send ignore message (without congestion avoidance)
    ##            if not paddingLength:
    ##                paddingLength = self._lengthDataProbdist.randomSample()
    ##                if paddingLength == const.INF_LABEL:
    ##                    paddingLength = const.MPU
    ##            log.debug("[walkie-talkie - %s] Sending ignore message.", self.end)
    ##            self.sendDownstream(self._msgFactory.newIgnore(paddingLength))

    ##            self._pad_count += 1
    ##            log.debug("[walkie-talkie - %s] sent burst padding. running count = %d", self.end, self._pad_count)

    def onSessionStarts(self, sessId):
        """Sens hint for session start.

        To be extended at child classes that implement final website
        fingerprinting countermeasures.
        """
        self.session = Session()
        if self.weAreClient:
            self._queue_session_start_notification = True
            #self.sendControlMessage(const.OP_APP_HINT, [self.getSessId(), True])
        else:
            self._sessId = sessId
        self._visiting = True

        # We defer flush of buffer
        # Since flush is likely to be empty because we just started the session,
        # we will start padding.
        #delay = self._delayDataProbdist.randomSample()
        #if not self._deferData or (self._deferData and self._deferData.called):
        #    self._deferData = deferLater(delay, self.flushBuffer)
        #    log.debug("[walkietalkie - %s] Delay buffer flush %s ms delay", self.end, delay)

        log.info("[walkietalkie - %s] - Session has started!(sessid = %s)", self.end, sessId)

    def processMessages(self, data):
        """Extract WFPad protocol messages.

        Data is written to the local application and padding messages are
        filtered out.
        """
        log.debug("[walkietalkie - %s] Parse protocol messages from stream.", self.end)

        # Make sure there actually is data to be parsed
        if (data is None) or (len(data) == 0):
            return None

        # Try to extract protocol messages
        msgs = []
        try:
            msgs = self._msgExtractor.extract(data)
        except Exception, e:
            log.exception("[walkietalkie - %s] Exception extracting "
                          "messages from stream: %s", self.end, str(e))

        self.session.lastRcvDownstreamTs = time.time()
        direction = const.IN if self.weAreClient else const.OUT
        for msg in msgs:
            log.debug("[walkietalkie - %s] A new message has been parsed!", self.end)
            msg.rcvTime = time.time()

            if msg.flags & const.FLAG_CONTROL:
                # Process control messages
                payload = msg.payload
                if len(payload) > 0:
                    self.circuit.upstream.write(payload)
                log.debug("[walkietalkie - %s] Control flag detected, processing opcode %d.", self.end, msg.opcode)
                self.receiveControlMessage(msg.opcode, msg.args)
                self.session.history.append(
                    (time.time(), const.FLAG_CONTROL, direction, msg.totalLen, len(msg.payload)))

            #self.deferBurstPadding('rcv')
            #self.session.numMessages['rcv'] += 1
            #self.session.totalBytes['rcv'] += msg.totalLen
            #log.debug("total bytes and total len of message: %s" % msg.totalLen)

            # Filter padding messages out.
            if msg.flags & const.FLAG_PADDING:
                log.debug("[walkietalkie - %s] Padding message ignored.", self.end)

                self.session.history.append(
                    (time.time(), const.FLAG_PADDING, direction, msg.totalLen, len(msg.payload)))

            # Forward data to the application.
            elif msg.flags & const.FLAG_DATA:
                log.debug("[walkietalkie - %s] Data flag detected, relaying upstream", self.end)
                self.session.dataBytes['rcv'] += len(msg.payload)
                self.session.dataMessages['rcv'] += 1

                self.circuit.upstream.write(msg.payload)

                self.session.lastRcvDataDownstreamTs = time.time()
                self.session.history.append(
                    (time.time(), const.FLAG_DATA, direction, msg.totalLen, len(msg.payload)))

            # Otherwise, flag not recognized
            else:
                log.error("[walkietalkie - %s] Invalid message flags: %d.", self.end, msg.flags)
        return msgs

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
                #elif command == const.WT_OP_TALKIE_START:
                #    log.debug('[wt-listener]: received talkie start notification from browser')
                #    self._transport.startTalkieBurst()
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
