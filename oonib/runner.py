# -*- encoding: utf-8 -*-
#
# :authors: Arturo Filastò, Isis Lovecruft
# :licence: see LICENSE for details
"""
In here we define a runner for the oonib backend system.
"""

from __future__ import print_function

from twisted.internet import reactor
from twisted.application import service, internet, app
from twisted.python.runtime import platformType

import txtorcon

from oonib.report.api import reportingBackend

from oonib import oonibackend
from oonib import config
from oonib import log

def txSetupFailed(failure):
    log.err("Setup failed")
    log.exception(failure)

def setupCollector(tor_process_protocol):
    def setup_complete(port):
        print("Exposed collector Tor hidden service on httpo://%s"
              % port.onion_uri)

    torconfig = txtorcon.TorConfig(tor_process_protocol.tor_protocol)
    public_port = 80
    # XXX there is currently a bug in txtorcon that prevents data_dir from
    # being passed properly. Details on the bug can be found here:
    # https://github.com/meejah/txtorcon/pull/22
    hs_endpoint = txtorcon.TCPHiddenServiceEndpoint(reactor, torconfig,
            public_port, data_dir=config.main.tor_datadir)
    hidden_service = hs_endpoint.listen(reportingBackend)
    hidden_service.addCallback(setup_complete)
    hidden_service.addErrback(txSetupFailed)

def startTor():
    def updates(prog, tag, summary):
        print("%d%%: %s" % (prog, summary))

    torconfig = txtorcon.TorConfig()
    torconfig.SocksPort = 9055
    if config.main.tor2webmode:
        torconfig.Tor2webMode = 1
        torconfig.CircuitBuildTimeout = 60
    torconfig.save()
    d = txtorcon.launch_tor(torconfig, reactor,
            tor_binary=config.main.tor_binary,
            progress_updates=updates)
    d.addCallback(setupCollector)
    d.addErrback(txSetupFailed)

class OBaseRunner():
    pass

if platformType == "win32":
    from twisted.scripts._twistw import WindowsApplicationRunner

    OBaseRunner = WindowsApplicationRunner
    # XXX Currently we don't support windows for starting a Tor Hidden Service
    log.warn(
        "Apologies! We don't support starting a Tor Hidden Service on Windows.")

else:
    from twisted.scripts._twistd_unix import UnixApplicationRunner
    class OBaseRunner(UnixApplicationRunner):
        def postApplication(self):
            """After the application is created, start the application and run
            the reactor. After the reactor stops, clean up PID files and such.
            """
            self.startApplication(self.application)
            # This is our addition. The rest is taken from
            # twisted/scripts/_twistd_unix.py 12.2.0
            startTor()
            self.startReactor(None, self.oldstdout, self.oldstderr)
            self.removePID(self.config['pidfile'])

        def createOrGetApplication(self):
            return oonibackend.application

OBaseRunner.loggerFactory = log.LoggerFactory
