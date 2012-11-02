#!/usr/bin/python
# -*- coding: utf-8 -*-

import obfsproxy.network.socks as socks
from twisted.internet import reactor, error

import obfsproxy.network.launch_transport as launch_transport
import obfsproxy.transports.transports as transports
import obfsproxy.common.log as log

from pyptlib.client import init, reportSuccess, reportFailure, reportEnd
from pyptlib.config import EnvError

import pprint

def do_managed_client():
    should_start_event_loop = False

    try:
        managedInfo = init(transports.transports.keys())
    except EnvError:
        log.warning("Client managed-proxy protocol failed.")
        return

    log.debug("pyptlib gave us the following data:\n'%s'", pprint.pformat(managedInfo))

    for transport in managedInfo['transports']:
        try:
            addrport = launch_transport.launch_transport_listener(transport, None, 'socks', None)
        except transports.TransportNotFound:
            log.warning("Could not find transport '%s'" % transport)
            reportFailure(transport, "Could not find transport.")
            continue
        except error.CannotListenError:
            log.warning("Could not set up listener for '%s'." % transport)
            reportFailure(transport, "Could not set up listener.")
            continue

        should_start_event_loop = True
        log.debug("Successfully launched '%s' at '%s'" % (transport, str(addrport)))
        reportSuccess(transport, 4, addrport, None, None) # XXX SOCKS v4 hardcoded

    reportEnd()

    if should_start_event_loop:
        log.info("Starting up the event loop.")
        reactor.run()
    else:
        log.info("No transports launched. Nothing to do.")