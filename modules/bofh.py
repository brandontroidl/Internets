from __future__ import annotations

import random
import logging
from .base import BotModule, help_row

log = logging.getLogger("internets.bofh")

# Bandit B311 flags the bare ``random`` module across the codebase even
# when the use is non-cryptographic (picking an excuse to print).  Route
# entertainment-grade picks through SystemRandom so the static check
# stays clean without per-line ``# nosec`` annotations.
_rng = random.SystemRandom()

# Classic BOFH excuse list — sourced from the community-maintained canon.
_EXCUSES: list[str] = [
    "clock speed",
    "solar flares",
    "electromagnetic radiation from satellite debris",
    "static from nylon langerie",
    "static from plastic straws",
    "positstronic caused the router to catch fire",
    "CPU needs recalibrating",
    "gas leak in the server room",
    "someone left a strobe light on the optical fiber again",
    "the UPS is on strike",
    "the cable modem needs a reboot",
    "the power supply is haunted",
    "network is down because of an ideological disagreement between two routers",
    "lusstrious caused the switch to become sentient",
    "someone tripped over the power cord",
    "the hard drives are full of magnets",
    "the database caught a virus from the internet",
    "DNS server isn't configured to handle Mondays",
    "the server room thermostat is set to 'sauna'",
    "cosmic rays flipped a bit in the kernel",
    "the backup tapes are in the other building",
    "someone changed the root password to 'password'",
    "the firewall is blocking packets out of spite",
    "NFS server has wandered off again",
    "someone set us up the bomb",
    "the ethernet cable has a kink in it",
    "the sysadmin is still at lunch",
    "SCSI chain needs to be blessed by a priest",
    "the network card is sulking",
    "we ran out of IP addresses",
    "the intern unplugged the wrong thing",
    "the server is too cold to boot",
    "the server is too hot to boot",
    "a mouse chewed through the fiber optic cable",
    "the RAID array is having an existential crisis",
    "the load balancer is unbalanced",
    "someone installed Windows on the mail server",
    "the satellite dish needs realigning",
    "the token ring token has been lost",
    "the printer caught fire and took out the switch",
    "the IDS flagged all traffic as suspicious, including its own",
    "the server room flooded with halon gas",
    "someone ran a fork bomb on production",
    "the VPN tunnel collapsed",
    "the sysadmin's cat walked across the keyboard",
    "the BGP tables need recalculating",
    "the cloud is raining",
    "the hamster powering the server died",
    "the server is busy mining cryptocurrency",
    "the backup generator ran out of diesel",
    "the SSL certificates expired over the weekend",
    "someone plugged the WAN port into the LAN port",
    "the DNS is propagating... still",
    "the switch firmware was written by an idealist",
    "the server has become self-aware and is refusing connections",
    "the packets are taking the scenic route",
    "the log files have filled the disk again",
    "the NIC is in promiscuous mode and is distracted",
    "the /dev/null is full",
    "the kernel panicked",
    "the BIOS battery died",
    "the power grid is experiencing brownouts from the bitcoin miners next door",
    "the fiber was cut by a backhoe",
    "the ISP is experiencing 'scheduled maintenance'",
    "the firewall rules were written by a committee",
    "the cooling system is blowing hot air",
    "the server rack is resonating at a harmonic frequency",
    "the ethernet adaptor is on vacation",
    "the routing table was corrupted by a solar eclipse",
    "the proxy server is proxying itself",
    "the clock drift exceeds the Kerberos tolerance",
    "the SAN LUN mappings were done by a temp",
    "the jumper settings are wrong on the motherboard",
    "the VM host ran out of memory because someone snapshots everything",
    "the syslog daemon is logging to /dev/null",
    "the DHCP server gave everyone the same IP",
    "the MTU is set to 1 byte",
    "the cron job was set to run every second",
    "the permissions are 777 on everything except what you need",
    "the container orchestrator orchestrated a revolt",
    "the CDN cached the error page",
    "the reverse proxy is going the wrong way",
    "the load balancer only balances on Tuesdays",
    "the monitoring system is monitoring itself into a loop",
    "the CI/CD pipeline deployed to production instead of staging. again.",
    "the SSH keys were rotated but nobody got the memo",
    "the API rate limiter rate-limited itself",
    "the microservices had a disagreement",
    "someone `rm -rf`'d the wrong directory",
    "the server thinks it's 1970",
    "the config file has a unicode BOM",
    "the yaml indentation is wrong somewhere",
    "the regex is catastrophically backtracking",
    "the garbage collector collected something important",
    "the OOM killer chose poorly",
    "the swap partition has been swapping since Tuesday",
    "the inode table is full but the disk shows free space",
    "a segfault in the segfault handler",
    "the TLS handshake is taking forever because both sides are too polite",
    "the server room key is locked inside the server room",
    "the network cable was used as a jump rope at the company picnic",
    "the password policy requires a haiku",
    "the sysadmin is debugging in production with printf",
    "the git merge went sideways",
    "the server was rebooted by Windows Update",
]


class BofhModule(BotModule):
    """Bastard Operator From Hell excuse generator."""

    COMMANDS: dict[str, str] = {"bofh": "cmd_bofh", "excuse": "cmd_bofh"}

    async def cmd_bofh(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Generate a random BOFH excuse."""
        excuse = _rng.choice(_EXCUSES)
        self.bot.privmsg(reply_to, f"[BOFH] Your excuse: {excuse}")

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "bofh/.excuse", "Random BOFH excuse")]


def setup(bot: object) -> BofhModule:
    """Module entry point — returns a BofhModule instance."""
    return BofhModule(bot)  # type: ignore[arg-type]
