# XXX modulify transports and move this to a single import
import obfsproxy.transports.dummy as dummy
import obfsproxy.transports.b64 as b64
import obfsproxy.transports.obfs2 as obfs2
import obfsproxy.transports.obfs3 as obfs3
import obfsproxy.transports.scramblesuit.scramblesuit as scramblesuit
import obfsproxy.transports.wfpadtools.wfpad as wfpad
from obfsproxy.transports.wfpadtools.specific import buflo, csbuflo, tamaraw, \
    adaptive, bwdiff, walkietalkie

transports = {'dummy': {'base': dummy.DummyTransport, 'client': dummy.DummyClient, 'server': dummy.DummyServer},
              'b64': {'base': b64.B64Transport, 'client': b64.B64Client, 'server': b64.B64Server},
              'obfs2': {'base': obfs2.Obfs2Transport, 'client': obfs2.Obfs2Client, 'server': obfs2.Obfs2Server},
              'scramblesuit': {'base': scramblesuit.ScrambleSuitTransport,
                               'client': scramblesuit.ScrambleSuitClient,
                               'server': scramblesuit.ScrambleSuitServer},
              'obfs3': {'base': obfs3.Obfs3Transport, 'client': obfs3.Obfs3Client, 'server': obfs3.Obfs3Server},
              'buflo': {'base': buflo.BuFLOTransport, 'client': buflo.BuFLOClient, 'server': buflo.BuFLOServer},
              'wfpad': {'base': wfpad.WFPadTransport, 'client': wfpad.WFPadClient, 'server': wfpad.WFPadServer},
              'csbuflo': {'base': csbuflo.CSBuFLOTransport, 'client': csbuflo.CSBuFLOClient,
                          'server': csbuflo.CSBuFLOServer},
              'tamaraw': {'base': tamaraw.TamarawTransport, 'client': tamaraw.TamarawClient,
                          'server': tamaraw.TamarawServer},
              'bwdiff': {'base': bwdiff.BWDiffTransport, 'client': bwdiff.BWDiffClient, 'server': bwdiff.BWDiffServer},
              'adaptive': {'base': adaptive.AdaptiveTransport, 'client': adaptive.AdaptiveClient,
                           'server': adaptive.AdaptiveServer},
              'walkietalkie': {'base': walkietalkie.WalkieTalkieTransport, 'client': walkietalkie.WalkieTalkieClient,
                               'server': walkietalkie.WalkieTalkieServer}}


def get_transport_class(name, role):
    # Rewrite equivalent roles.
    if role == 'socks':
        role = 'client'
    elif role == 'ext_server':
        role = 'server'

    # Find the correct class
    if (name in transports) and (role in transports[name]):
        return transports[name][role]
    else:
        raise TransportNotFound


class TransportNotFound(Exception): pass
