#
# Copyright 2006-2009  Red Hat, Inc.
# Jeremy Katz <katzj@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free  Software Foundation; either version 2 of the License, or
# (at your option)  any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.

import logging
import random

import libvirt

from virtinst import util
from virtinst.VirtualDevice import VirtualDevice
from virtinst.xmlbuilder import XMLBuilder, XMLProperty


def _random_mac(conn):
    """Generate a random MAC address.

    00-16-3E allocated to xensource
    52-54-00 used by qemu/kvm

    The OUI list is available at http://standards.ieee.org/regauth/oui/oui.txt.

    The remaining 3 fields are random, with the first bit of the first
    random field set 0.

    @return: MAC address string
    """
    ouis = {'xen': [0x00, 0x16, 0x3E], 'qemu': [0x52, 0x54, 0x00]}

    try:
        oui = ouis[conn.getType().lower()]
    except KeyError:
        oui = ouis['xen']

    mac = oui + [
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff)]
    return ':'.join(["%02x" % x for x in mac])


class VirtualPort(XMLBuilder):
    type = XMLProperty(xpath="./virtualport/@type")

    managerid = XMLProperty(xpath="./virtualport/parameters/@managerid",
                            is_int=True)

    typeid = XMLProperty(xpath="./virtualport/parameters/@typeid", is_int=True)
    typeidversion = XMLProperty(
            xpath="./virtualport/parameters/@typeidversion", is_int=True)
    instanceid = XMLProperty(xpath="./virtualport/parameters/@instanceid")


class VirtualNetworkInterface(VirtualDevice):

    _virtual_device_type = VirtualDevice.VIRTUAL_DEV_NET

    TYPE_BRIDGE     = "bridge"
    TYPE_VIRTUAL    = "network"
    TYPE_USER       = "user"
    TYPE_ETHERNET   = "ethernet"
    TYPE_DIRECT   = "direct"
    network_types = [TYPE_BRIDGE, TYPE_VIRTUAL, TYPE_USER, TYPE_ETHERNET,
                     TYPE_DIRECT]

    @staticmethod
    def get_network_type_desc(net_type):
        """
        Return human readable description for passed network type
        """
        desc = net_type.capitalize()

        if net_type == VirtualNetworkInterface.TYPE_BRIDGE:
            desc = _("Shared physical device")
        elif net_type == VirtualNetworkInterface.TYPE_VIRTUAL:
            desc = _("Virtual networking")
        elif net_type == VirtualNetworkInterface.TYPE_USER:
            desc = _("Usermode networking")

        return desc

    @staticmethod
    def generate_mac(conn):
        """
        Generate a random MAC that doesn't conflict with any VMs on
        the connection.
        """
        if hasattr(conn, "_virtinst__fake_conn_predictable"):
            # Testing hack
            return "00:11:22:33:44:55"

        for ignore in range(256):
            mac = _random_mac(conn)
            ret = VirtualNetworkInterface.is_conflict_net(conn, mac)
            if ret[1] is None:
                return mac

        logging.debug("Failed to generate non-conflicting MAC")
        return None

    @staticmethod
    def is_conflict_net(conn, searchmac):
        """
        @returns: a two element tuple:
            first element is True if fatal collision occured
            second element is a string description of the collision.

            Non fatal collisions (mac addr collides with inactive guest) will
            return (False, "description of collision")
        """
        if searchmac is None:
            return (False, None)

        vms = conn.fetch_all_guests()
        for vm in vms:
            for nic in vm.get_devices("interface"):
                nicmac = nic.macaddr or ""
                if nicmac.lower() == searchmac.lower():
                    return (True, _("The MAC address '%s' is in use "
                                    "by another virtual machine.") % searchmac)
        return (False, None)


    def __init__(self, conn, macaddr=None, type=TYPE_BRIDGE, bridge=None,
                 network=None, model=None,
                 parsexml=None, parsexmlnode=None):
        # pylint: disable=W0622
        # Redefining built-in 'type', but it matches the XML so keep it

        VirtualDevice.__init__(self, conn, parsexml, parsexmlnode)

        self._network = None
        self._bridge = None
        self._macaddr = None
        self._type = None
        self._model = None
        self._target_dev = None
        self._source_dev = None
        self._source_mode = "vepa"

        self.virtualport = VirtualPort(conn, parsexml, parsexmlnode)
        self._XML_SUB_ELEMENTS.append("virtualport")

        # Generate _random_mac
        self._random_mac = None
        self._default_bridge = None

        if self._is_parse():
            return

        self.type = type
        self.macaddr = macaddr
        self.bridge = bridge
        self.source_dev = bridge
        self.network = network
        self.model = model

        if self.type == self.TYPE_VIRTUAL:
            if network is None:
                raise ValueError(_("A network name was not provided"))

    def _generate_default_bridge(self):
        ret = self._default_bridge
        if ret is None:
            ret = False
            default = util.default_bridge(self.conn)
            if default:
                ret = default[1]

        self._default_bridge = ret
        return ret or None

    def get_source(self):
        """
        Convenince function, try to return the relevant <source> value
        per the network type.
        """
        if self.type == self.TYPE_VIRTUAL:
            return self.network
        if self.type == self.TYPE_BRIDGE:
            return self.bridge
        if self.type == self.TYPE_ETHERNET or self.type == self.TYPE_DIRECT:
            return self.source_dev
        if self.type == self.TYPE_USER:
            return None
        return self.network or self.bridge or self.source_dev

    def set_source(self, newsource):
        """
        Conveninece function, try to set the relevant <source> value
        per the network type
        """
        if self.type == self.TYPE_VIRTUAL:
            self.network = newsource
        elif self.type == self.TYPE_BRIDGE:
            self.bridge = newsource
        elif self.type == self.TYPE_ETHERNET or self.type == self.TYPE_DIRECT:
            self.source_dev = newsource
        return
    source = property(get_source, set_source)

    def get_type(self):
        return self._type
    def set_type(self, val):
        if val not in self.network_types:
            raise ValueError(_("Unknown network type %s") % val)
        self._type = val
    type = XMLProperty(get_type, set_type,
                         xpath="./@type")

    def get_macaddr(self):
        # Don't generate a random MAC if parsing XML, since it can be slow
        if self._macaddr or self._is_parse():
            return self._macaddr
        if not self._random_mac:
            self._random_mac = self.generate_mac(self.conn)
        return self._random_mac
    def set_macaddr(self, val):
        util.validate_macaddr(val)
        self._macaddr = val
    macaddr = XMLProperty(get_macaddr, set_macaddr,
                            xpath="./mac/@address")

    def get_network(self):
        return self._network
    def set_network(self, newnet):
        def _is_net_active(netobj):
            # Apparently the 'info' command was never hooked up for
            # libvirt virNetwork python apis.
            if not self.conn:
                return True
            return self.conn.listNetworks().count(netobj.name())

        if newnet is not None and self.conn:
            try:
                net = self.conn.networkLookupByName(newnet)
            except libvirt.libvirtError, e:
                raise ValueError(_("Virtual network '%s' does not exist: %s")
                                   % (newnet, str(e)))
            if not _is_net_active(net):
                raise ValueError(_("Virtual network '%s' has not been "
                                   "started.") % newnet)

        self._network = newnet
    network = XMLProperty(get_network, set_network,
                            xpath="./source/@network")

    def get_bridge(self):
        if (not self._is_parse() and
            not self._bridge and
            self.type == self.TYPE_BRIDGE):
            return self._generate_default_bridge()
        return self._bridge
    def set_bridge(self, val):
        self._bridge = val
    bridge = XMLProperty(get_bridge, set_bridge,
                           xpath="./source/@bridge")

    def get_model(self):
        return self._model
    def set_model(self, val):
        self._model = val
    model = XMLProperty(get_model, set_model,
                          xpath="./model/@type")

    def get_target_dev(self):
        return self._target_dev
    def set_target_dev(self, val):
        self._target_dev = val
    target_dev = XMLProperty(get_target_dev, set_target_dev,
                               xpath="./target/@dev")

    def get_source_dev(self):
        return self._source_dev
    def set_source_dev(self, val):
        self._source_dev = val
    source_dev = XMLProperty(get_source_dev, set_source_dev,
                               xpath="./source/@dev")

    def get_source_mode(self):
        return self._source_mode
    def set_source_mode(self, newmode):
        self._source_mode = newmode
    source_mode = XMLProperty(get_source_mode, set_source_mode,
                                xpath="./source/@mode")

    def setup(self, meter=None):
        if self.macaddr:
            ret, msg = self.is_conflict_net(self.conn, self.macaddr)
            if msg is not None:
                if ret is False:
                    logging.warning(msg)
                else:
                    raise RuntimeError(msg)

    def _get_xml_config(self):
        src_xml = ""
        model_xml = ""
        target_xml = ""
        if self.type == self.TYPE_BRIDGE:
            src_xml     = "      <source bridge='%s'/>\n" % self.bridge
        elif self.type == self.TYPE_VIRTUAL:
            src_xml     = "      <source network='%s'/>\n" % self.network
        elif self.type == self.TYPE_ETHERNET and self.source_dev:
            src_xml     = "      <source dev='%s'/>\n" % self.source_dev
        elif self.type == self.TYPE_DIRECT and self.source_dev:
            src_xml     = "      <source dev='%s' mode='%s'/>\n" % (self.source_dev, self.source_mode)

        if self.model:
            model_xml   = "      <model type='%s'/>\n" % self.model

        if self.target_dev:
            target_xml  = "      <target dev='%s'/>\n" % self.target_dev

        xml  = "    <interface type='%s'>\n" % self.type
        xml += src_xml
        xml += "      <mac address='%s'/>\n" % self.macaddr
        xml += target_xml
        xml += model_xml
        xml += "    </interface>"
        return xml
