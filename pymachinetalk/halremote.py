import uuid
import platform

import zmq
import threading

# protobuf
from machinetalk.protobuf.message_pb2 import Container
from machinetalk.protobuf.types_pb2 import *


class Pin():
    def __init__(self):
        self.name = ''
        self.pintype = HAL_BIT
        self.direction = HAL_IN
        self._synced = False
        self._value = None
        self.handle = 0  # stores handle received on bind
        self.parent = None
        self.synced_condition = threading.Condition(threading.Lock())
        self.value_condition = threading.Condition(threading.Lock())

        # callbacks
        self.on_synced_changed = []
        self.on_value_changed = []

    def wait_synced(self, timeout=None):
        with self.synced_condition:
            if self.synced:
                return True
            self.synced_condition.wait(timeout=timeout)
            return self.synced

    def wait_value(self, timeout=None):
        with self.value_condition:
            if self.value:
                return True
            self.value_condition.wait(timeout=timeout)
            return self.value

    @property
    def value(self):
        with self.value_condition:
            return self._value

    @value.setter
    def value(self, value):
        with self.value_condition:
            if self._value != value:
                self._value = value
                self.value_condition.notify()
                for func in self.on_value_changed:
                    func(value)

    @property
    def synced(self):
        with self.synced_condition:
            return self._synced

    @synced.setter
    def synced(self, value):
        with self.synced_condition:
            if value != self._synced:
                self._synced = value
                self.synced_condition.notify()
                for func in self.on_synced_changed:
                    func(value)

    def set(self, value):
        if self.value != value:
            self.value = value
            self.synced = False
            if self.parent:
                self.parent.pin_change(self)

    def get(self):
        return self.value


class RemoteComponent():
    def __init__(self, name, debug=False):
        self.threads = []
        self.shutdown = threading.Event()
        self.tx_lock = threading.Lock()
        self.timer_lock = threading.Lock()
        self.connected_condition = threading.Condition(threading.Lock())
        self.debug = debug

        # callbacks
        self.on_connected_changed = []

        self.name = name
        self.pinsbyname = {}
        self.pinsbyhandle = {}
        self.is_ready = False
        self.no_create = False

        self.halrcmd_uri = ''
        self.halrcomp_uri = ''
        self.connected = False
        self.heartbeat_period = 3000
        self.ping_outstanding = False
        self.state = 'Disconnected'
        self.halrcmd_state = 'Down'
        self.halrcomp_state = 'Down'
        self.halrcomp_period = 0
        self.halrcmd_timer = None
        self.halrcomp_timer = None

        # more efficient to reuse a protobuf message
        self.tx = Container()
        self.rx = Container()

        # ZeroMQ
        client_id = '%s-%s' % (platform.node(), uuid.uuid4())  # must be unique
        context = zmq.Context()
        context.linger = 0
        self.context = context
        self.halrcmd_socket = self.context.socket(zmq.DEALER)
        self.halrcmd_socket.setsockopt(zmq.LINGER, 0)
        self.halrcmd_socket.setsockopt(zmq.IDENTITY, client_id)
        self.halrcomp_socket = self.context.socket(zmq.SUB)
        self.sockets_connected = False

    def wait_connected(self, timeout=None):
        with self.connected_condition:
            if self.connected:
                return True
            self.connected_condition.wait(timeout=timeout)
            return self.connected

    def socket_worker(self):
        poll = zmq.Poller()
        poll.register(self.halrcmd_socket, zmq.POLLIN)
        poll.register(self.halrcomp_socket, zmq.POLLIN)

        while not self.shutdown.is_set():
            s = dict(poll.poll(200))
            if self.halrcmd_socket in s:
                self.process_halrcmd()
            if self.halrcomp_socket in s:
                self.process_halrcomp()

    def process_halrcmd(self):
        msg = self.halrcmd_socket.recv()
        self.rx.ParseFromString(msg)
        if self.debug:
            print('[%s] received message on halrcmd:' % self.name)
            print(self.rx)

        if self.rx.type == MT_PING_ACKNOWLEDGE:
            self.ping_outstanding = False
            if self.halrcmd_state == 'Trying':
                self.update_state('Connecting')
                self.bind()

        elif self.rx.type == MT_HALRCOMP_BIND_CONFIRM:
            self.halrcmd_state = 'Up'
            self.unsubscribe()  # clear previous subscription
            self.subscribe()  # trigger full update

        elif self.rx.type == MT_HALRCOMP_BIND_REJECT \
        or self.rx.type == MT_HALRCOMP_SET_REJECT:
            self.halrcmd_state = 'Down'
            self.update_state('Error')
            if self.rx.type == MT_HALRCOMP_BIND_REJECT:
                self.update_error('Bind', self.rx.note)
            else:
                self.update_error('Pinchange', self.rx.note)

        else:
            print('[%s] Warning: halrcmd receiced unsupported message' % self.name)

    def process_halrcomp(self):
        (topic, msg) = self.halrcomp_socket.recv_multipart()
        self.rx.ParseFromString(msg)

        if topic != self.name:  # ignore uninteresting messages
            return

        if self.debug:
            print('[%s] received message on halrcomp: topic %s' % (self.name, topic))
            print(self.rx)

        if self.rx.type == MT_HALRCOMP_INCREMENTAL_UPDATE:
            for rpin in self.rx.pin:
                lpin = self.pinsbyhandle[rpin.handle]
                self.pin_update(rpin, lpin)
            self.refresh_halrcomp_heartbeat()

        elif self.rx.type == MT_HALRCOMP_FULL_UPDATE:
            comp = self.rx.comp[0]
            for rpin in comp.pin:
                name = rpin.name.split('.')[1]
                lpin = self.pinsbyname[name]
                lpin.handle = rpin.handle
                self.pinsbyhandle[rpin.handle] = lpin
                self.pin_update(rpin, lpin)

            if self.halrcomp_state != 'Up':  # will be executed only once
                self.halrcomp_state = 'Up'
                self.update_state('Connected')

            if self.rx.HasField('pparams'):
                interval = self.rx.pparams.keepalive_timer
                self.start_halrcomp_heartbeat(interval * 2)

        elif self.rx.type == MT_PING:
            if self.halrcomp_state == 'Up':
                self.refresh_halrcomp_heartbeat()
            else:
                self.update_state('Connecting')
                self.unsubscribe()  # clean up previous subscription
                self.subscribe()  # trigger a fresh subscribe -> full update

        elif self.rx.type == MT_HALRCOMMAND_ERROR:
            self.halrcomp_state = 'Down'
            self.update_state('Error')
            self.update_error('halrcomp', self.rx.note)

    def start(self):
        self.halrcmd_state = 'Trying'
        self.update_state('Connecting')

        if self.connect_sockets():
            self.shutdown.clear()  # in case we already used the component
            self.threads.append(threading.Thread(target=self.socket_worker))
            for thread in self.threads:
                thread.start()
            self.start_halrcmd_heartbeat()
            with self.tx_lock:
                self.send_cmd(MT_PING)

    def stop(self):
        self.is_ready = False
        self.shutdown.set()
        for thread in self.threads:
            thread.join()
        self.threads = []
        self.cleanup()
        self.update_state('Disconnected')

    def cleanup(self):
        if self.connected:
            self.unsubscribe()
        self.stop_halrcmd_heartbeat()
        self.disconnect_sockets()

    def connect_sockets(self):
        self.sockets_connected = True
        self.halrcmd_socket.connect(self.halrcmd_uri)
        self.halrcomp_socket.connect(self.halrcomp_uri)

        return True

    def disconnect_sockets(self):
        if self.sockets_connected:
            self.halrcmd_socket.disconnect(self.halrcmd_uri)
            self.halrcomp_socket.disconnect(self.halrcomp_uri)
            self.sockets_connected = False

    def send_cmd(self, msg_type):
        self.tx.type = msg_type
        if self.debug:
            print('[%s] sending message: %s' % (self.name, msg_type))
            print(str(self.tx))
        self.halrcmd_socket.send(self.tx.SerializeToString(), zmq.NOBLOCK)
        self.tx.Clear()

    def halrcmd_timer_tick(self):
        if not self.connected:
            return

        if self.ping_outstanding:
            self.halrcmd_state = 'Trying'
            self.update_state('Timeout')

        with self.tx_lock:
            self.send_cmd(MT_PING)
        self.ping_outstanding = True

        self.halrcmd_timer = threading.Timer(self.heartbeat_period / 1000,
                                             self.halrcmd_timer_tick)
        self.halrcmd_timer.start()  # rearm timer

    def start_halrcmd_heartbeat(self):
        self.ping_outstanding = False

        if self.heartbeat_period > 0:
            self.halrcmd_timer = threading.Timer(self.heartbeat_period / 1000,
                                                 self.halrcmd_timer_tick)
            self.halrcmd_timer.start()

    def stop_halrcmd_heartbeat(self):
        if self.halrcmd_timer:
            self.halrcmd_timer.cancel()
            self.halrcmd_timer = None

    def halrcomp_timer_tick(self):
        if self.debug:
            print('[%s] timeout on halrcomp' % self.name)
        self.halrcomp_state = 'Down'
        self.update_state('Timeout')

    def start_halrcomp_heartbeat(self, interval):
        self.timer_lock.acquire()
        if self.halrcomp_timer:
            self.halrcomp_timer.cancel()

        self.halrcomp_period = interval
        if interval > 0:
            self.halrcomp_timer = threading.Timer(interval / 1000,
                                                  self.halrcomp_timer_tick)
            self.halrcomp_timer.start()
        self.timer_lock.release()

    def stop_halrcomp_heartbeat(self):
        self.timer_lock.acquire()
        if self.halrcomp_timer:
            self.halrcomp_timer.cancel()
            self.halrcomp_timer = None
        self.timer_lock.release()

    def refresh_halrcomp_heartbeat(self):
        self.timer_lock.acquire()
        if self.halrcomp_timer:
            self.halrcomp_timer.cancel()
            self.halrcomp_timer = threading.Timer(self.halrcomp_period / 1000,
                                                  self.halrcomp_timer_tick)
            self.halrcomp_timer.start()
        self.timer_lock.release()

    def update_state(self, state):
        if state != self.state:
            self.state = state
            if state == 'Connected':
                with self.connected_condition:
                    self.connected = True
                    self.connected_condition.notify()
                print('[%s] connected' % self.name)
                for func in self.on_connected_changed:
                    func(self.connected)
            elif self.connected:
                with self.connected_condition:
                    self.connected = False
                    self.connected_condition.notify()
                self.stop_halrcomp_heartbeat()
                self.unsync_pins()
                print('[%s] disconnected' % self.name)
                for func in self.on_connected_changed:
                    func(self.connected)
            elif state == 'Error':
                with self.connected_condition:
                    self.connected = False
                    self.connected_condition.notify()  # notify even if not connected

    def update_error(self, error, description):
        print('[%s] error: %s %s' % (self.name, error, description))

    # create a new HAL pin
    def newpin(self, name, pintype, direction):
        pin = Pin()
        pin.name = name
        pin.pintype = pintype
        pin.direction = direction
        pin.parent = self
        self.pinsbyname[name] = pin

        if pintype == HAL_FLOAT:
            pin.value = 0.0
        elif pintype == HAL_BIT:
            pin.value = False
        elif pintype == HAL_S32:
            pin.value = 0
        elif pintype == HAL_U32:
            pin.value = 0

        return pin

    def unsync_pins(self):
        for pin in self.pinsbyname:
            pin.synced = False

    def getpin(self, name):
        return self.pinsbyname[name]

    def ready(self):
        if not self.is_ready:
            self.is_ready = True
            self.start()

    def pin_update(self, rpin, lpin):
        if rpin.HasField('halfloat'):
            lpin.value = float(rpin.halfloat)
            lpin.synced = True
        elif rpin.HasField('halbit'):
            lpin.value = bool(rpin.halbit)
            lpin.synced = True
        elif rpin.HasField('hals32'):
            lpin.value = int(rpin.hals32)
            lpin.synced = True
        elif rpin.HasField('halu32'):
            lpin.value = int(rpin.halu32)
            lpin.synced = True

    def pin_change(self, pin):
        if self.debug:
            print('[%s] pin change %s' % (self.name, pin.name))

        if self.state != 'Connected':  # accept only when connected
            return
        if pin.direction == HAL_IN:  # only update out and IO pins
            return

        # This message MUST carry a Pin message for each pin which has
        # changed value since the last message of this type.
        # Each Pin message MUST carry the handle field.
        # Each Pin message MAY carry the name field.
        # Each Pin message MUST carry the type field
        # Each Pin message MUST - depending on pin type - carry a halbit,
        # halfloat, hals32, or halu32 field.
        with self.tx_lock:
            p = self.tx.pin.add()
            p.handle = pin.handle
            p.type = pin.pintype
            if p.type == HAL_FLOAT:
                p.halfloat = float(pin.value)
            elif p.type == HAL_BIT:
                p.halbit = bool(pin.value)
            elif p.type == HAL_S32:
                p.hals32 = int(pin.value)
            elif p.type == HAL_U32:
                p.halu32 = int(pin.value)
            self.send_cmd(MT_HALRCOMP_SET)

    def bind(self):
        with self.tx_lock:
            c = self.tx.comp.add()
            c.name = self.name
            c.no_create = self.no_create  # for now we create the component
            for name, pin in self.pinsbyname.iteritems():
                p = c.pin.add()
                p.name = '%s.%s' % (self.name, name)
                p.type = pin.pintype
                p.dir = pin.direction
                if p.type == HAL_FLOAT:
                    p.halfloat = float(pin.value)
                elif p.type == HAL_BIT:
                    p.halbit = bool(pin.value)
                elif p.type == HAL_S32:
                    p.hals32 = int(pin.value)
                elif p.type == HAL_U32:
                    p.halu32 = int(pin.value)
            if self.debug:
                print('[%s] bind' % self.name)
            self.send_cmd(MT_HALRCOMP_BIND)

    def subscribe(self):
        self.halrcomp_state = 'Trying'
        self.halrcomp_socket.setsockopt(zmq.SUBSCRIBE, self.name)

    def unsubscribe(self):
        self.halrcomp_state = 'Down'
        self.halrcomp_socket.setsockopt(zmq.UNSUBSCRIBE, self.name)

    def __getitem__(self, k):
        return self.pinsbyname[k].get()

    def __setitem__(self, k, v):
        self.pinsbyname[k].set(v)


def component(name):
    return RemoteComponent(name)
