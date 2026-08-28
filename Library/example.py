# -*- coding: utf-8 -*-
"""A worked tour of the softether library against a real VPN Server.

    python example.py --host 127.0.0.1 --port 5555 --password adminpass

Everything it creates is named ``EXAMPLE*`` and removed again at the end, but
it does write to the server: run it against a test box, not production.
Pass ``--read-only`` to skip every mutating step.
"""

from __future__ import annotations

import argparse
import datetime
import sys

from Library.softether import (
    AccessProtocol,
    AlreadyExistsError,
    LogSwitchType,
    NotFoundError,
    NotSupportedError,
    PacketLogConfig,
    PacketLogIndex,
    RpcError,
    SoftEtherClient,
    SoftEtherError,
    UserAuthType,
    ac_rule,
    access_rule,
    admin_option,
)

HUB = "EXAMPLEHUB"


def show_server(vpn):
    print("== server ==")
    info = vpn.get_server_info()
    print("  product     :", info.ServerProductName, info.ServerVersionString)
    print("  build       :", info.ServerBuildInfoString)
    print("  mode        :", info.ServerType_u32, "(0 standalone, 1 controller, 2 member)")
    print("  os          :", info.OsSystemName, info.OsVersion)

    status = vpn.get_server_status()
    print("  hubs        :", status.NumHubTotal_u32)
    print("  sessions    :", status.NumSessionsTotal_u32)
    print("  started     :", status.StartTime_dt)          # a real datetime
    print("  traffic in  :", status.recv_unicast_bytes, "unicast bytes")

    print("  listeners   :", [
        (item.Ports_u32, "enabled" if item.Enables_bool else "disabled")
        for item in vpn.enum_listener().ListenerList
    ])
    print("  capabilities:", len(vpn.get_caps().CapsList), "entries")


def show_hubs(vpn):
    print("== hubs ==")
    for hub in vpn.enum_hub().HubList:
        print("  %-20s users=%-4s sessions=%-4s online=%s"
              % (hub.HubName_str, hub.NumUsers_u32, hub.NumSessions_u32, hub.Online_bool))


def build_hub(vpn):
    print("== creating %s ==" % HUB)
    try:
        vpn.create_hub(HUB, admin_password_plain_text="examplepass", online=True,
                       max_session=100, no_enum=False)
        print("  created")
    except AlreadyExistsError:
        print("  already there, reusing it")

    # Logging: keep a daily security log and TCP connection headers only.
    packet_log = [PacketLogConfig.NONE] * 16
    packet_log[PacketLogIndex.TCP_CONNECTION] = PacketLogConfig.HEADER
    vpn.set_hub_log(HUB,
                    save_security_log=True,
                    security_log_switch_type=LogSwitchType.DAY,
                    save_packet_log=True,
                    packet_log_switch_type=LogSwitchType.DAY,
                    packet_log_config=packet_log)
    print("  logging configured")

    # A password user that expires in 30 days, with a couple of policy limits.
    expires = datetime.datetime.utcnow() + datetime.timedelta(days=30)
    try:
        vpn.create_user(HUB, "alice",
                        realname="Alice Example",
                        note="created by example.py",
                        auth_type=UserAuthType.PASSWORD,
                        auth_password="s3cr3t",
                        expire_time=expires,
                        use_policy=True,
                        policy_access=True,
                        policy_max_connection=8,
                        policy_max_upload=10_000_000,
                        policy_no_bridge=True)
        print("  user alice created, expires", expires.strftime("%Y-%m-%d"))
    except AlreadyExistsError:
        print("  user alice already exists")

    # Replace the whole access list in one call.
    vpn.set_access_list(HUB, [
        access_rule(priority=100, active=True, discard=False,
                    protocol=AccessProtocol.TCP, dest_port_start=443, dest_port_end=443,
                    note="allow https"),
        access_rule(priority=200, active=True, discard=False,
                    protocol=AccessProtocol.ICMPV4, note="allow ping"),
        access_rule(priority=999, active=True, discard=True, note="drop everything else"),
    ])
    print("  access list:", len(vpn.enum_access(HUB).AccessList), "rules")

    # Source IP limits and an administration option.
    vpn.set_ac_list(HUB, [ac_rule(priority=1, deny=False, masked=True,
                                  ip_address="10.0.0.0", subnet_mask="255.255.255.0")])
    try:
        vpn.set_hub_admin_options(HUB, [admin_option(name="deny_empty_password", value=1)])
        print("  admin options set")
    except RpcError as exc:
        print("  admin options refused:", exc.name or exc.message)

    # SecureNAT: a virtual DHCP + NAT for the hub.
    try:
        vpn.set_securenat_option(HUB, use_nat=True, use_dhcp=True,
                                 ip="192.168.30.1", mask="255.255.255.0",
                                 dhcp_lease_ip_start="192.168.30.10",
                                 dhcp_lease_ip_end="192.168.30.200",
                                 dhcp_subnet_mask="255.255.255.0",
                                 dhcp_expire_time_span=7200,
                                 dhcp_gateway_address="192.168.30.1",
                                 dhcp_dns_server_address="8.8.8.8")
        vpn.enable_securenat(HUB)
        print("  SecureNAT enabled")
    except NotSupportedError as exc:
        print("  SecureNAT unavailable here:", exc.message)


def inspect_hub(vpn):
    print("== inspecting %s ==" % HUB)
    status = vpn.get_hub_status(HUB)
    print("  online=%s sessions=%s users=%s groups=%s macs=%s"
          % (status.Online_bool, status.NumSessions_u32, status.NumUsers_u32,
             status.NumGroups_u32, status.NumMacTables_u32))

    for user in vpn.enum_user(HUB).UserList:
        print("  user %-12s logins=%-4s expires=%s"
              % (user.Name_str, user.NumLogin_u32, user.Expires_dt or "never"))

    for session in vpn.enum_session(HUB).SessionList:
        print("  session %-20s user=%-10s ip=%-15s packets=%s"
              % (session.Name_str, session.Username_str, session.ClientIP_ip,
                 session.PacketNum_u64))

    log_files = vpn.enum_log_file().LogFiles
    print("  log files:", len(log_files))
    if log_files:
        first = log_files[0]
        chunk = vpn.read_log_file(first.FilePath_str)
        print("  %s -> %d bytes read" % (first.FilePath_str, len(chunk.Buffer_bin or b"")))


def hub_admin_mode(host, port):
    """Log in to the hub itself instead of the whole server."""
    print("== virtual hub admin mode ==")
    with SoftEtherClient(host, port, password="examplepass", hub=HUB,
                         suppress_insecure_warning=True) as hub_admin:
        print("  users seen as hub admin:",
              [u.Name_str for u in hub_admin.enum_user(HUB).UserList])
        try:
            hub_admin.get_server_status()
        except SoftEtherError as exc:
            print("  server-wide call refused, as expected:", type(exc).__name__)


def tear_down(vpn):
    print("== cleaning up ==")
    for step, action in (("user", lambda: vpn.delete_user(HUB, "alice")),
                         ("hub", lambda: vpn.delete_hub(HUB))):
        try:
            action()
            print("  %s deleted" % step)
        except NotFoundError:
            print("  %s was already gone" % step)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--password", default="")
    parser.add_argument("--fingerprint", default=None,
                        help="pin the server certificate (sha256 hex)")
    parser.add_argument("--read-only", action="store_true",
                        help="only read; create nothing")
    args = parser.parse_args()

    try:
        with SoftEtherClient(args.host, args.port, args.password,
                             fingerprint=args.fingerprint,
                             suppress_insecure_warning=args.fingerprint is None) as vpn:
            print("connected:", vpn.ping())
            show_server(vpn)
            show_hubs(vpn)
            if args.read_only:
                return 0
            build_hub(vpn)
            inspect_hub(vpn)
            hub_admin_mode(args.host, args.port)
            tear_down(vpn)
            vpn.flush()          # persist everything to vpn_server.config
            print("done")
    except SoftEtherError as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
