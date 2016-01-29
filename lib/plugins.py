#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from collections import namedtuple
import traceback
import sys
import os
import imp
import pkgutil
import time

from util import *
from i18n import _
from util import profiler, PrintError, DaemonThread
import wallet

class Plugins(DaemonThread):

    @profiler
    def __init__(self, config, is_local, gui_name):
        DaemonThread.__init__(self)
        if is_local:
            find = imp.find_module('plugins')
            plugins = imp.load_module('electrum_ltc_plugins', *find)
        else:
            plugins = __import__('electrum_ltc_plugins')
        self.pkgpath = os.path.dirname(plugins.__file__)
        self.config = config
        self.hw_wallets = {}
        self.plugins = {}
        self.gui_name = gui_name
        self.descriptions = {}
        self.device_manager = DeviceMgr()
        self.load_plugins()
        self.start()

    def load_plugins(self):
        for loader, name, ispkg in pkgutil.iter_modules([self.pkgpath]):
            m = loader.find_module(name).load_module(name)
            d = m.__dict__
            gui_good = self.gui_name in d.get('available_for', [])
            # We register wallet types even if the GUI isn't provided
            # otherwise the user gets a misleading message like
            # "Unknown wallet type: 2fa"
            details = d.get('registers_wallet_type')
            if details:
                self.register_plugin_wallet(name, gui_good, details)
            if not gui_good:
                continue
            self.descriptions[name] = d
            if not d.get('requires_wallet_type') and self.config.get('use_' + name):
                try:
                    self.load_plugin(name)
                except BaseException as e:
                    traceback.print_exc(file=sys.stdout)
                    self.print_error("cannot initialize plugin %s:" % name,
                                     str(e))

    def get(self, name):
        return self.plugins.get(name)

    def count(self):
        return len(self.plugins)

    def load_plugin(self, name):
        full_name = 'electrum_ltc_plugins.' + name + '.' + self.gui_name
        loader = pkgutil.find_loader(full_name)
        if not loader:
            raise RuntimeError("%s implementation for %s plugin not found"
                               % (self.gui_name, name))
        p = loader.load_module(full_name)
        plugin = p.Plugin(self, self.config, name)
        self.add_jobs(plugin.thread_jobs())
        self.plugins[name] = plugin
        self.print_error("loaded", name)
        return plugin

    def close_plugin(self, plugin):
        self.remove_jobs(plugin.thread_jobs())

    def enable(self, name):
        self.config.set_key('use_' + name, True, True)
        p = self.get(name)
        if p:
            return p
        return self.load_plugin(name)

    def disable(self, name):
        self.config.set_key('use_' + name, False, True)
        p = self.get(name)
        if not p:
            return
        self.plugins.pop(name)
        p.close()
        self.print_error("closed", name)

    def toggle(self, name):
        p = self.get(name)
        return self.disable(name) if p else self.enable(name)

    def is_available(self, name, w):
        d = self.descriptions.get(name)
        if not d:
            return False
        deps = d.get('requires', [])
        for dep, s in deps:
            try:
                __import__(dep)
            except ImportError:
                return False
        requires = d.get('requires_wallet_type', [])
        return not requires or w.wallet_type in requires

    def hardware_wallets(self, action):
        wallet_types, descs = [], []
        for name, (gui_good, details) in self.hw_wallets.items():
            if gui_good:
                try:
                    p = self.wallet_plugin_loader(name)
                    if action == 'restore' or p.is_enabled():
                        wallet_types.append(details[1])
                        descs.append(details[2])
                except:
                    self.print_error("cannot load plugin for:", name)
        return wallet_types, descs

    def register_plugin_wallet(self, name, gui_good, details):
        def dynamic_constructor(storage):
            return self.wallet_plugin_loader(name).wallet_class(storage)

        if details[0] == 'hardware':
            self.hw_wallets[name] = (gui_good, details)
        self.print_error("registering wallet %s: %s" %(name, details))
        wallet.wallet_types.append(details + (dynamic_constructor,))

    def wallet_plugin_loader(self, name):
        if not name in self.plugins:
            self.load_plugin(name)
        return self.plugins[name]

    def run(self):
        while self.is_running():
            time.sleep(0.1)
            self.run_jobs()
        self.print_error("stopped")


hook_names = set()
hooks = {}

def hook(func):
    hook_names.add(func.func_name)
    return func

def run_hook(name, *args):
    results = []
    f_list = hooks.get(name, [])
    for p, f in f_list:
        if p.is_enabled():
            try:
                r = f(*args)
            except Exception:
                print_error("Plugin error")
                traceback.print_exc(file=sys.stdout)
                r = False
            if r:
                results.append(r)

    if results:
        assert len(results) == 1, results
        return results[0]


class BasePlugin(PrintError):

    def __init__(self, parent, config, name):
        self.parent = parent  # The plugins object
        self.name = name
        self.config = config
        self.wallet = None
        # add self to hooks
        for k in dir(self):
            if k in hook_names:
                l = hooks.get(k, [])
                l.append((self, getattr(self, k)))
                hooks[k] = l

    def diagnostic_name(self):
        return self.name

    def __str__(self):
        return self.name

    def close(self):
        # remove self from hooks
        for k in dir(self):
            if k in hook_names:
                l = hooks.get(k, [])
                l.remove((self, getattr(self, k)))
                hooks[k] = l
        self.parent.close_plugin(self)
        self.on_close()

    def on_close(self):
        pass

    def requires_settings(self):
        return False

    def thread_jobs(self):
        return []

    def is_enabled(self):
        return self.is_available() and self.config.get('use_'+self.name) is True

    def is_available(self):
        return True

    def settings_dialog(self):
        pass

Device = namedtuple("Device", "path interface_number id_ product_key")
DeviceInfo = namedtuple("DeviceInfo", "device description initialized")

class DeviceMgr(PrintError):
    '''Manages hardware clients.  A client communicates over a hardware
    channel with the device.

    In addition to tracking device HID IDs, the device manager tracks
    hardware wallets and manages wallet pairing.  A HID ID may be
    paired with a wallet when it is confirmed that the hardware device
    matches the wallet, i.e. they have the same master public key.  A
    HID ID can be unpaired if e.g. it is wiped.

    Because of hotplugging, a wallet must request its client
    dynamically each time it is required, rather than caching it
    itself.

    The device manager is shared across plugins, so just one place
    does hardware scans when needed.  By tracking HID IDs, if a device
    is plugged into a different port the wallet is automatically
    re-paired.

    Wallets are informed on connect / disconnect events.  It must
    implement connected(), disconnected() callbacks.  Being connected
    implies a pairing.  Callbacks can happen in any thread context,
    and we do them without holding the lock.

    Confusingly, the HID ID (serial number) reported by the HID system
    doesn't match the device ID reported by the device itself.  We use
    the HID IDs.

    This plugin is thread-safe.  Currently only devices supported by
    hidapi are implemented.

    '''

    def __init__(self):
        super(DeviceMgr, self).__init__()
        # Keyed by wallet.  The value is the device id if the wallet
        # has been paired, and None otherwise.
        self.wallets = {}
        # A list of clients.  The key is the client, the value is
        # a (path, id_) pair.
        self.clients = {}
        # What we recognise.  Each entry is a (vendor_id, product_id)
        # pair.
        self.recognised_hardware = set()
        # For synchronization
        self.lock = threading.RLock()

    def register_devices(self, device_pairs):
        for pair in device_pairs:
            self.recognised_hardware.add(pair)

    def create_client(self, device, handler, plugin):
        # Get from cache first
        client = self.client_lookup(device.id_)
        if client:
            return client
        client = plugin.create_client(device, handler)
        if client:
            self.print_error("Registering", client)
            with self.lock:
                self.clients[client] = (device.path, device.id_)
        return client

    def wallet_id(self, wallet):
        with self.lock:
            return self.wallets.get(wallet)

    def wallet_by_id(self, id_):
        with self.lock:
            for wallet, wallet_id in self.wallets.items():
                if wallet_id == id_:
                    return wallet
            return None

    def unpair_wallet(self, wallet):
        with self.lock:
            if not wallet in self.wallets:
                return
            wallet_id = self.wallets.pop(wallet)
            client = self.client_lookup(wallet_id)
            self.clients.pop(client, None)
        wallet.unpaired()
        if client:
            client.close()

    def unpair_id(self, id_):
        with self.lock:
            wallet = self.wallet_by_id(id_)
        if wallet:
            self.unpair_wallet(wallet)

    def pair_wallet(self, wallet, id_):
        with self.lock:
            self.wallets[wallet] = id_
        wallet.paired()

    def paired_wallets(self):
        return list(self.wallets.keys())

    def client_lookup(self, id_):
        with self.lock:
            for client, (path, client_id) in self.clients.items():
                if client_id == id_:
                    return client
        return None

    def client_by_id(self, id_, handler):
        '''Returns a client for the device ID if one is registered.  If
        a device is wiped or in bootloader mode pairing is impossible;
        in such cases we communicate by device ID and not wallet.'''
        self.scan_devices(handler)
        return self.client_lookup(id_)

    def client_for_wallet(self, plugin, wallet, force_pair):
        assert wallet.handler

        devices = self.scan_devices(wallet.handler)
        wallet_id = self.wallet_id(wallet)

        client = self.client_lookup(wallet_id)
        if client:
            # An unpaired client might have another wallet's handler
            # from a prior scan.  Replace to fix dialog parenting.
            client.handler = wallet.handler
            return client

        for device in devices:
            if device.id_ == wallet_id:
                return self.create_client(device, wallet.handler, plugin)

        if force_pair:
            first_address, derivation = wallet.first_address()
            assert first_address

            # The wallet has not been previously paired, so let the user
            # choose an unpaired device and compare its first address.
            info = self.select_device(wallet, plugin, devices)
            if info:
                client = self.client_lookup(info.device.id_)
                if client and client.is_pairable():
                    # See comment above for same code
                    client.handler = wallet.handler
                    # This will trigger a PIN/passphrase entry request
                    client_first_address = client.first_address(derivation)
                    if client_first_address == first_address:
                        self.pair_wallet(wallet, info.device.id_)
                        return client

        return None

    def unpaired_device_infos(self, handler, plugin, devices=None):
        '''Returns a list of DeviceInfo objects: one for each connected,
        unpaired device accepted by the plugin.'''
        if devices is None:
            devices = self.scan_devices(handler)
        devices = [dev for dev in devices if not self.wallet_by_id(dev.id_)]

        states = [_("wiped"), _("initialized")]
        infos = []
        for device in devices:
            if not device.product_key in plugin.DEVICE_IDS:
                continue
            client = self.create_client(device, handler, plugin)
            if not client:
                continue
            state = states[client.is_initialized()]
            label = client.label() or _("An unnamed %s") % plugin.device
            descr = "%s (%s)" % (label, state)
            infos.append(DeviceInfo(device, descr, client.is_initialized()))

        return infos

    def select_device(self, wallet, plugin, devices=None):
        '''Ask the user to select a device to use if there is more than one,
        and return the DeviceInfo for the device.'''
        infos = self.unpaired_device_infos(wallet.handler, plugin, devices)
        if not infos:
            return None
        if len(infos) == 1:
            return infos[0]
        msg = _("Please select which %s device to use:") % plugin.device
        descriptions = [info.description for info in infos]
        return infos[wallet.handler.query_choice(msg, descriptions)]

    def scan_devices(self, handler):
        # All currently supported hardware libraries use hid, so we
        # assume it here.  This can be easily abstracted if necessary.
        # Note this import must be local so those without hardware
        # wallet libraries are not affected.
        import hid

        self.print_error("scanning devices...")

        # First see what's connected that we know about
        devices = []
        for d in hid.enumerate(0, 0):
            product_key = (d['vendor_id'], d['product_id'])
            if product_key in self.recognised_hardware:
                devices.append(Device(d['path'], d['interface_number'],
                                      d['serial_number'], product_key))

        # Now find out what was disconnected
        pairs = [(dev.path, dev.id_) for dev in devices]
        disconnected_ids = []
        with self.lock:
            connected = {}
            for client, pair in self.clients.items():
                if pair in pairs:
                    connected[client] = pair
                else:
                    disconnected_ids.append(pair[1])
            self.clients = connected

        # Unpair disconnected devices
        for id_ in disconnected_ids:
            self.unpair_id(id_)

        return devices
