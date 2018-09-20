"""
This module implements the CS-CSBuFLO countermeasure proposed by Cai et al.
"""
import math
import time
from random import uniform

# WFPadTools imports
import obfsproxy.common.log as logging
from obfsproxy.transports.wfpadtools import histo
from obfsproxy.transports.wfpadtools import const
from obfsproxy.transports.wfpadtools.wfpad import WFPadTransport
from obfsproxy.transports.wfpadtools.util import genutil as gu
from obfsproxy.transports.wfpadtools.util import mathutil as mu

# Logging
log = logging.get_obfslogger()


class CSBuFLOTransport(WFPadTransport):
    """Implementation of the CSBuFLO countermeasure.

    It extends the WFPad transport by adding congestion awareness and
    and offering two possible conditions to stop the padding that
    reduce the overhead of the one in BuFLO.
    """
    def __init__(self):
        super(CSBuFLOTransport, self).__init__()

        # Defaults for BuFLO specifications.
        self._initial_rho = const.INIT_RHO
        self._period = const.INIT_RHO
        self._length = const.MPU
        self._padding_mode = const.TOTAL_PADDING
        self._early_termination = False

        self._rho_stats = [[]]
        self._rho_star = self._initial_rho

        # Set constant length for messages
        self._lengthDataProbdist = histo.uniform(self._length)

    @classmethod
    def register_external_mode_cli(cls, subparser):
        """Register CLI arguments for CSBuFLO parameters."""
        subparser.add_argument("--period",
                               required=False,
                               type=float,
                               help="Initial transmission rate at which transport sends "
                                    "messages (Default: 0ms).",
                               dest="period")
        subparser.add_argument("--psize",
                               required=False,
                               type=int,
                               help="Length of messages to be transmitted"
                                    " (Default: MPU).",
                               dest="psize")
        subparser.add_argument("--early",
                               required=False,
                               type=bool,
                               help="Whether the client will notify the server"
                                    " that has ended padding, so that the server "
                                    "saves on padding by skipping the long tail.",
                               default=False)
        subparser.add_argument("--padding",
                               required=False,
                               type=str,
                               help="Padding mode for this endpoint. There"
                                    " are two possible values: \n"
                                    "- payload (CPSP): pads to the closest multiple "
                                    "of 2^N for N st 2^N closest power of two"
                                    " greater than the payload size.\n"
                                    "- total (CTSP): pads to closest power of two.\n"
                                    "(Default: CTSP).",
                               dest="padding")

        super(CSBuFLOTransport, cls).register_external_mode_cli(subparser)

    @classmethod
    def validate_external_mode_cli(cls, args):
        """Assign the given command line arguments to local variables."""
        super(CSBuFLOTransport, cls).validate_external_mode_cli(args)

        if args.period:
            cls._initial_rho = args.period
            cls._period = args.period
        if args.psize:
            cls._length = args.psize
        if args.padding:
            cls._padding_mode = args.padding
        if args.early:
            cls._early_termination = args.early

    def onSessionStarts(self, sessId):
        # Initialize rho stats
        self.constantRatePaddingDistrib(self._period)
        if self._padding_mode == const.TOTAL_PADDING:
            self.relayTotalPad(sessId, self._period, False)
        elif self._padding_mode == const.PAYLOAD_PADDING:
            self.relayPayloadPad(sessId, self._period, False)
        else:
            raise RuntimeError("Value passed for padding mode is not valid: %s" % self._padding_mode)

        if self._early_termination and self.weAreServer:
            stopCond = self.stopCondition
            def earlyTermination(self):
                return not self.session.is_peer_padding or stopCond()
            self.stopCondition = earlyTermination
        WFPadTransport.onSessionStarts(self, sessId)

    def onSessionEnds(self, sessId):
        super(CSBuFLOTransport, self).onSessionEnds(sessId)
        # Reset rho stats
        self._rho_stats = [[]]
        self._rho_star = self._initial_rho

    def onEndPadding(self):
        WFPadTransport.onEndPadding(self)
        if self._early_termination and self.weAreClient:
            self.sendControlMessage(const.OP_END_PADDING)
            log.info("[csbuflo - client] - Padding stopped! Will notify server.")

    def whenReceivedUpstream(self, data):
        self._rho_stats.append([])
        self.whenReceived()

    def whenReceivedDownstream(self, data):
        self.whenReceived()

    def whenReceived(self):
        if self._rho_star == 0:
            self._rho_star = self._initial_rho
        else:
            self._rho_star = self.estimate_rho(self._rho_star)
        log.debug("[cs-buflo] rho star = %s", self._rho_star)

    def crossed_threshold(self):
        """Return boolean whether we need to update the transmission rate

        CSBuFLO updates transmission rate when the amount of bytes sent
        downstream, namely, junk bytes + real data bytes is 
        """
        total_sent_bytes = self.session.totalBytes['snd']
        if total_sent_bytes - const.MTU <= 0:
            return False
        crossed = int(math.log(total_sent_bytes - const.MTU, 2)) < int(math.log(total_sent_bytes, 2))
        #log.debug("[cs-buflo] Total sent bytes = %s, MTU = %s, Crossed = %s", total_sent_bytes, const.MTU, crossed)
        return crossed

    def sendDataMessage(self, payload="", paddingLen=0):
        """Send data message."""
        super(CSBuFLOTransport, self).sendDataMessage(payload, paddingLen)
        self._rho_stats[-1].append(time.time())
        if self.crossed_threshold():
            self.update_transmission_rate()
        elif self._rho_star >= const.MAX_RHO:
            self._rho_star = self._initial_rho

    def estimate_rho(self, rho_star):
        """Estimate new value of rho based on past network performance."""
        #log.debug("[cs-buflo] rho stats: %s" % self._rho_stats)
        time_intervals = gu.flatten_list([gu.apply_consecutive_elements(burst_list, lambda x, y: (y - x) * const.SCALE)
                                          for burst_list in self._rho_stats])
        #log.debug("[cs-buflo] Time intervals = %s", time_intervals)
        if len(time_intervals) == 0:
            return rho_star
        else:
            return math.pow(2, math.floor(math.log(mu.median(time_intervals), 2)))

    def update_transmission_rate(self):
        """Transmission rate."""
        prev_period = self._period
        self._period = uniform(0, 2 * self._rho_star)
        self.constantRatePaddingDistrib(self._period)
        log.debug("[cs-buflo] Transmission rate has been updated from %s to %s.", prev_period, self._period)


class CSBuFLOClient(CSBuFLOTransport):
    """Extend the CSBuFLOTransport class."""

    def __init__(self):
        """Initialize a CSBuFLOClient object."""
        CSBuFLOTransport.__init__(self)


class CSBuFLOServer(CSBuFLOTransport):
    """Extend the CSBuFLOTransport class."""

    def __init__(self):
        """Initialize a CSBuFLOServer object."""
        CSBuFLOTransport.__init__(self)
