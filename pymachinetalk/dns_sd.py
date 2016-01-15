import sys
import avahi
import pprint
import avahi.ServiceTypeDatabase

import dbus
import gobject
gobject.threads_init()
from dbus.mainloop.glib import DBusGMainLoop

import threading


class ServiceTypeDatabase:
    def __init__(self):
        self.pretty_name = avahi.ServiceTypeDatabase.ServiceTypeDatabase()

    def get_human_type(self, servicetype):
        if str(servicetype) in self.pretty_name:
            return self.pretty_name[servicetype]
        else:
            return servicetype


class ServiceData():
    def __init__(self):
        self.uuid = None
        self.instance = None
        self.dsn = None
        self.name = None
        self.type = None
        self.txts = []


class ServiceDiscovery():
    def __init__(self, service_type, uuid='', interface='', debug=False):
        self.discovered_condition = threading.Condition(threading.Lock())
        self.disappeared_condition = threading.Condition(threading.Lock())

        # callbacks
        self.on_discovered = []
        self.on_disappeared = []
        self.on_error = []

        self.server = None
        # Start Service Discovery
        self.debug = debug
        self.domain = ''
        self.service_type = service_type
        self.service_names = {}  # used once discovered
        self.uuid = uuid
        self.interface = interface
        try:
            loop = DBusGMainLoop()
            self.system_bus = dbus.SystemBus(mainloop=loop)
            self.system_bus.add_signal_receiver(self.avahi_dbus_connect_cb,
                                                "NameOwnerChanged",
                                                "org.freedesktop.DBus",
                                                arg0="org.freedesktop.Avahi")
        except dbus.DBusException, e:
            pprint.pprint(e)
            sys.exit(1)

        self.service_browsers = {}

    def __del__(self):
        self.stop()

    def wait_discovered(self, timeout=None):
        with self.discovered_condition:
            if len(self.service_names) > 0:
                return True
            self.discovered_condition.wait(timeout=timeout)
            return (len(self.service_names) > 0)

    def wait_disappeared(self, timeout=None):
        with self.disappeared_condition:
            if len(self.service_names) == 0:
                return True
            self.disappeared_condition.wait(timeout=timeout)
            return (len(self.service_names) == 0)

    def start(self):
        self.start_service_discovery()

    def stop(self):
        self.stop_service_discovery()

    def avahi_dbus_connect_cb(self, a, connect, disconnect):
        if connect != "":
            print("We are disconnected from avahi-daemon")
            self.stop_service_discovery()
        else:
            print("We are connected to avahi-daemon")
            self.start_service_discovery()

    def siocgifname(self, interface):
        if interface <= 0:
            return "any"
        else:
            return self.server.GetNetworkInterfaceNameByIndex(interface)

    def service_resolved(self, interface, protocol, name, servicetype, domain, host, aprotocol, address, port, txt, flags):
        del aprotocol
        del flags
        stdb = ServiceTypeDatabase()
        h_type = stdb.get_human_type(servicetype)
        if self.debug:
            print("Service data for service '%s' of type '%s' (%s) in domain '%s' on %s.%i:" % (name, h_type, servicetype, domain, self.siocgifname(interface), protocol))
            print("\tHost %s (%s), port %i, TXT data: %s" % (host, address, port, avahi.txt_array_to_string_array(txt)))

        data = ServiceData()
        data.txts = avahi.txt_array_to_string_array(txt)
        data.name = name
        match = False
        for txt in data.txts:
            key, value = txt.split('=')
            if key == 'dsn':
                data.dsn = value
            elif key == 'service':
                data.type = value
            elif key == 'instance':
                data.instance = value
            elif key == 'uuid':
                data.uuid = value
                match = self.uuid == value
        match = match or (self.uuid == '')

        if match:
            with self.discovered_condition:
                self.service_names[name] = data
                self.discovered_condition.notify()
            if self.debug:
                print('discovered: %s %s %s' % (name, data.dsn, data.uuid))
            for func in self.on_discovered:
                func(data)

    def print_error(self, err):
        if self.debug:
            print("SD Error: %s" % str(err))
        for func in self.on_error:
            func(str(err))

    def new_service(self, interface, protocol, name, servicetype, domain, flags):
        del flags
        if self.debug:
            print("Found service '%s' of type '%s' in domain '%s' on %s.%i." % (name, servicetype, domain, self.siocgifname(interface), protocol))

# this check is for local services
#        try:
#            if flags & avahi.LOOKUP_RESULT_LOCAL:
#                return
#        except dbus.DBusException:
#            pass

        self.server.ResolveService(interface, protocol, name, servicetype, domain, avahi.PROTO_INET, dbus.UInt32(0), reply_handler=self.service_resolved, error_handler=self.print_error)

    def remove_service(self, interface, protocol, name, servicetype, domain, flags):
        del flags
        if self.debug:
            print("Service '%s' of type '%s' in domain '%s' on %s.%i disappeared." % (name, servicetype, domain, self.siocgifname(interface), protocol))
        if name in self.service_names:
            with self.disappeared_condition:
                data = self.service_names.pop(name)
                self.disappeared_condition.notify()
            if self.debug:
                print("disappered: %s" % name)
            for func in self.on_disappeared:
                func(data)

    def add_service_type(self, interface, protocol, servicetype, domain):
        # Are we already browsing this domain for this type?
        if (interface, protocol, servicetype, domain) in self.service_browsers:
            return

        if self.debug:
            print("Browsing for services of type '%s' in domain '%s' on %s.%i ..." % (servicetype, domain, self.siocgifname(interface), protocol))

        b = dbus.Interface(self.system_bus.get_object(avahi.DBUS_NAME,
                                                      self.server.ServiceBrowserNew(interface, protocol, servicetype, domain, dbus.UInt32(0))),
                           avahi.DBUS_INTERFACE_SERVICE_BROWSER)
        b.connect_to_signal('ItemNew', self.new_service)
        b.connect_to_signal('ItemRemove', self.remove_service)
        b.connect_to_signal('AllForNow', self.all_for_now_handler)
        b.connect_to_signal('CacheExhausted', self.cache_exhausted_handler)
        b.connect_to_signal('Failure', self.failure_handler)

        self.service_browsers[(interface, protocol, servicetype, domain)] = b

    def del_service_type(self, interface, protocol, servicetype, domain):

        service = (interface, protocol, servicetype, domain)
        if self.service_browsers not in service:
            return
        sb = self.service_browsers[service]
        try:
            sb.Free()
        except dbus.DBusException:
            pass
        del self.service_browsers[service]

    def all_for_now_handler(self):
        if self.debug:
            print('all for now')

    def cache_exhausted_handler(self):
        if self.debug:
            print('cache exhausted')

    def failure_handler(self, error):
        print('failure: %s' % error)

    def start_service_discovery(self):
        if len(self.domain) != 0:
            print("domain not null %s" % (self.domain))
            print("Already Discovering")
            return
        try:
            self.server = dbus.Interface(self.system_bus.get_object(avahi.DBUS_NAME, avahi.DBUS_PATH_SERVER),
                                         avahi.DBUS_INTERFACE_SERVER)
            self.domain = self.server.GetDomainName()
        except:
            print("Check that the Avahi daemon is running!")
            return

        if self.debug:
            print("Starting discovery")

        if self.interface == "":
            interface = avahi.IF_UNSPEC
        else:
            interface = self.server.GetNetworkInterfaceIndexByName(self.interface)
        protocol = avahi.PROTO_INET

        self.add_service_type(interface, protocol, self.service_type, self.domain)

    def stop_service_discovery(self):
        if len(self.domain) == 0:
            if self.debug:
                print("Discovery already stopped")
            return

        for sb in self.service_browsers.values():
            try:
                sb.Free()  # clean up service browsers
            except dbus.DBusException:
                pass
        self.service_browsers = {}

        if self.debug:
            print("Discovery stopped")
