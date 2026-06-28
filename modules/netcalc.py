"""Network calculators - pure stdlib, no network, no key.

    .cidr <a.b.c.d/prefix>            network / broadcast / mask / hosts / range
    .subnet <ip/prefix> <new_prefix>  split a block into smaller subnets
    .port <number|name>               map a port number <-> service name
"""
from __future__ import annotations

import ipaddress
import socket
from .base import BotModule, help_row, strip_ctrl

_MAX_INPUT = 80

# Common port → service.  Complements socket.getservby* (which reads
# /etc/services and varies by host); this covers the usual suspects.
_PORTS: dict[int, str] = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp",
    53: "dns", 67: "dhcp-server", 68: "dhcp-client", 69: "tftp",
    80: "http", 88: "kerberos", 110: "pop3", 111: "rpcbind", 119: "nntp",
    123: "ntp", 135: "msrpc", 137: "netbios-ns", 139: "netbios-ssn",
    143: "imap", 161: "snmp", 162: "snmp-trap", 179: "bgp", 194: "irc",
    389: "ldap", 443: "https", 445: "smb", 465: "smtps", 514: "syslog",
    515: "printer", 587: "submission", 631: "ipp", 636: "ldaps",
    853: "dns-over-tls", 990: "ftps", 993: "imaps", 995: "pop3s",
    1080: "socks", 1194: "openvpn", 1433: "mssql", 1521: "oracle",
    1723: "pptp", 2049: "nfs", 3128: "squid", 3306: "mysql", 3389: "rdp",
    5060: "sip", 5222: "xmpp", 5432: "postgresql", 5672: "amqp",
    5900: "vnc", 6379: "redis", 6667: "irc", 6697: "ircs", 8080: "http-alt",
    8443: "https-alt", 9090: "prometheus", 9200: "elasticsearch",
    9418: "git", 11211: "memcached", 25565: "minecraft", 27017: "mongodb",
}
_NAMES: dict[str, int] = {v: k for k, v in _PORTS.items()}


def _cidr(arg: str) -> str:
    try:
        net = ipaddress.ip_network(arg.strip(), strict=False)
    except ValueError:
        return "invalid CIDR - try 10.0.0.0/24 or 2001:db8::/48"
    total = net.num_addresses
    parts = [str(net.with_prefixlen), f"net {net.network_address}"]
    if isinstance(net, ipaddress.IPv4Network):
        parts.append(f"bcast {net.broadcast_address}")
        parts.append(f"mask {net.netmask}")
        if net.prefixlen <= 30:
            parts.append(f"hosts {total - 2:,}")
            parts.append(f"usable {net.network_address + 1}–{net.broadcast_address - 1}")
        else:
            parts.append(f"hosts {total:,}")
    else:
        parts.append(f"/{net.prefixlen}")
        parts.append(f"addrs {total:,}" if total < 10 ** 12 else f"addrs 2^{128 - net.prefixlen}")
    return " :: ".join(parts)


def _subnet(block: str, new: str) -> str:
    try:
        net = ipaddress.ip_network(block.strip(), strict=False)
        newlen = int(str(new).strip().lstrip("/"))
    except (ValueError, TypeError):
        return "usage: .subnet <ip/prefix> <new_prefix>  e.g. .subnet 10.0.0.0/16 24"
    if not (net.prefixlen <= newlen <= net.max_prefixlen):
        return f"new prefix must be between {net.prefixlen} and {net.max_prefixlen}"
    count = 2 ** (newlen - net.prefixlen)
    first = ipaddress.ip_network(f"{net.network_address}/{newlen}", strict=False)
    last = ipaddress.ip_network(f"{net[-1]}/{newlen}", strict=False)
    return (f"{count:,} × /{newlen} :: {first.num_addresses:,} addr each :: "
            f"first {first.network_address} :: last {last.network_address}")


def _port(arg: str) -> str:
    s = arg.strip().lower()
    if not s:
        return "usage: .port <number|name>"
    if s.isdigit():
        n = int(s)
        if not (0 <= n <= 65535):
            return "port out of range (0–65535)"
        name = _PORTS.get(n)
        if not name:
            try:
                name = socket.getservbyport(n)
            except (OSError, ValueError):
                name = None
        return f"port {n} → {name}" if name else f"port {n} → (unassigned / not well-known)"
    n = _NAMES.get(s)
    if n is None:
        try:
            n = socket.getservbyname(s)
        except (OSError, ValueError):
            n = None
    return f"{strip_ctrl(s, 30)} → port {n}" if n is not None else \
        f"no well-known port for '{strip_ctrl(s, 30)}'"


class NetcalcModule(BotModule):
    """`.cidr` / `.subnet` / `.port` - offline network calculators."""

    COMMANDS: dict[str, str] = {
        "cidr": "cmd_cidr",
        "subnet": "cmd_subnet",
        "port": "cmd_port",
    }

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return False
        return True

    async def cmd_cidr(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cidr <a.b.c.d/prefix>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_cidr(arg[:_MAX_INPUT])))

    async def cmd_subnet(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        parts = (arg or "").split()
        if len(parts) != 2:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}subnet <ip/prefix> <new_prefix>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_subnet(parts[0][:_MAX_INPUT], parts[1][:8])))

    async def cmd_port(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}port <number|name>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_port(arg[:_MAX_INPUT])))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "cidr <ip/prefix>", "Network/broadcast/mask/hosts/range"),
            help_row(prefix, "subnet <ip/prefix> <newlen>", "Split a block into subnets"),
            help_row(prefix, "port <number|name>", "Port number <-> service name"),
        ]


def setup(bot: object) -> NetcalcModule:
    return NetcalcModule(bot)  # type: ignore[arg-type]
