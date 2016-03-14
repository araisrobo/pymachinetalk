#/usr/bin/env python

import time
import sys
import gobject
import threading

from pymachinetalk.dns_sd import ServiceDiscovery
import pymachinetalk.halremote as halremote


class BasicClass():
    def __init__(self):
        # launcher_sd = ServiceDiscovery(service_type="_launcher._sub._machinekit._tcp", debug=True)
        launcher_sd = ServiceDiscovery(service_type="_launcher._sub._machinekit._tcp")
        launcher_sd.on_discovered.append(self.service_discovered)
        launcher_sd.on_disappeared.append(self.service_disappeared)
        launcher_sd.start()
        self.launcher_sd = launcher_sd

        self.halrcompReady = False
        self.halrcmdReady = False
        halrcomp = halremote.RemoteComponent('anddemo')
        self.but0 = halrcomp.newpin('button0', halremote.HAL_BIT, halremote.HAL_OUT)
        self.but1 = halrcomp.newpin('button1', halremote.HAL_BIT, halremote.HAL_OUT)
        self.led = halrcomp.newpin('led', halremote.HAL_BIT, halremote.HAL_IN)
        # halrcomp.no_create = True
        self.halrcomp = halrcomp

    def start_sd(self, uuid):
        halrcmd_sd = ServiceDiscovery(service_type="_halrcmd._sub._machinekit._tcp", uuid=uuid)
        halrcmd_sd.on_discovered.append(self.halrcmd_discovered) # start service discover
        halrcmd_sd.start()
        #halrcmd_sd.disappered_callback = disappeared
        #self.halrcmd_sd = halrcmd_sd

        halrcomp_sd = ServiceDiscovery(service_type="_halrcomp._sub._machinekit._tcp", uuid=uuid)
        halrcomp_sd.on_discovered.append(self.halrcomp_discovered)
        halrcomp_sd.start()
        #self.harcomp_sd = halrcomp_sd

    def service_disappeared(self, data):
        print("disappeared %s %s" % (data.name))

    def service_discovered(self, data):
        print("discovered %s %s %s" % (data.name, data.dsn, data.uuid))
        # set uuid for interlaken.local
        uuid = "a09a5a04-f7ac-40e9-b898-2d41f391f68e"
        if (data.uuid == uuid):
            self.start_sd(data.uuid)

    def halrcmd_discovered(self, data):
        print("discovered %s %s" % (data.name, data.dsn))
        self.halrcomp.halrcmd_uri = data.dsn
        self.halrcmdReady = True
        if self.halrcompReady:
            self.start_halrcomp()

    def halrcomp_discovered(self, data):
        print("discovered %s %s" % (data.name, data.dsn))
        self.halrcomp.halrcomp_uri = data.dsn
        self.halrcompReady = True
        if self.halrcmdReady:
            self.start_halrcomp()

    def start_halrcomp(self):
        print('connecting rcomp %s' % self.halrcomp.name)
        self.halrcomp.ready()
        print('waiting for component connected')
        self.halrcomp.wait_connected()
        print('remote component connected')
        self.but0.set(True)
        for i in range(0,10):
            self.but1.set(not self.led.get())
            time.sleep(1)
            print self.led.get()


    def stop(self):
        self.halrcomp.stop()


def main():
    gobject.threads_init()  # important: initialize threads if gobject main loop is used
    basic = BasicClass()
    loop = gobject.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        loop.quit()

    print("stopping threads")
    basic.stop()

    # wait for all threads to terminate
    while threading.active_count() > 1:
        time.sleep(0.1)

    print("threads stopped")
    sys.exit(0)

if __name__ == "__main__":
    main()
