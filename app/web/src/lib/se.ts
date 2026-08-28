/**
 * SoftEther domain knowledge the UI needs: names for wire enums, and a
 * catalogue of the security-policy fields with human labels, grouping, units
 * and defaults -- this is what turns the API's forty raw `policy:` fields into
 * a form a person can read.
 */
import type { Wire } from "./api";

export const AUTH_TYPES: Record<number, string> = {
  0: "Anonymous",
  1: "Password",
  2: "User certificate",
  3: "Signed certificate",
  4: "RADIUS",
  5: "NT domain",
};

export const AUTH_TYPE_OPTIONS = [
  { value: 1, label: "Password", hint: "A password stored on the VPN server." },
  { value: 0, label: "Anonymous", hint: "Anyone naming this user connects. Rarely wise." },
  { value: 2, label: "User certificate", hint: "A specific X.509 certificate registered on this user." },
  { value: 3, label: "Signed certificate", hint: "Any certificate signed by a trusted CA of the hub." },
  { value: 4, label: "RADIUS", hint: "Delegated to the hub's RADIUS server." },
  { value: 5, label: "NT domain", hint: "Delegated to the Windows domain controller." },
];

export const HUB_TYPES: Record<number, string> = {
  0: "Standalone",
  1: "Static (cluster)",
  2: "Dynamic (cluster)",
};

export const SESSION_TYPE = (s: Wire): string => {
  if (s["LinkMode_bool"]) return "cascade";
  if (s["SecureNATMode_bool"]) return "SecureNAT";
  if (s["BridgeMode_bool"]) return "bridge";
  if (s["Layer3Mode_bool"]) return "L3 switch";
  return "client";
};

export interface PolicyField {
  key: string; // wire name, e.g. "policy:MaxUpload_u32"
  label: string;
  help: string;
  kind: "bool" | "number";
  unit?: string;
  /** For numbers: what 0 means (usually "no limit"). */
  zero?: string;
  group: string;
}

export const POLICY_GROUPS = [
  "Access",
  "Bandwidth & sessions",
  "Address limits",
  "Filtering",
  "IPv6",
  "Advanced",
] as const;

export const POLICY_FIELDS: PolicyField[] = [
  // -- access ------------------------------------------------------------------
  { key: "policy:Access_bool", label: "Allow access", help: "Master switch: off refuses every connection by this user.", kind: "bool", group: "Access" },
  { key: "policy:MultiLogins_u32", label: "Concurrent logins", help: "How many sessions this user may hold at once.", kind: "number", zero: "unlimited", group: "Access" },
  { key: "policy:MaxConnection_u32", label: "TCP connections per session", help: "Parallel TCP carriers a single VPN session may open.", kind: "number", zero: "server default", group: "Access" },
  { key: "policy:TimeOut_u32", label: "Idle timeout", help: "Disconnect after this many seconds without traffic.", kind: "number", unit: "s", zero: "server default", group: "Access" },
  { key: "policy:AutoDisconnect_u32", label: "Auto-disconnect", help: "Hard-cut every session after this many seconds, active or not.", kind: "number", unit: "s", zero: "never", group: "Access" },
  { key: "policy:FixPassword_bool", label: "Forbid password change", help: "The user cannot change their own password from a client.", kind: "bool", group: "Access" },
  { key: "policy:NoSavePassword_bool", label: "Forbid saving the password", help: "Clients must ask for the password on every connection.", kind: "bool", group: "Access" },

  // -- bandwidth ----------------------------------------------------------------
  { key: "policy:MaxUpload_u32", label: "Upload limit", help: "Ceiling on traffic from the user into the hub.", kind: "number", unit: "bytes/s", zero: "unlimited", group: "Bandwidth & sessions" },
  { key: "policy:MaxDownload_u32", label: "Download limit", help: "Ceiling on traffic from the hub to the user.", kind: "number", unit: "bytes/s", zero: "unlimited", group: "Bandwidth & sessions" },
  { key: "policy:NoQoS_bool", label: "Disable VoIP/QoS priority", help: "Treat all of this user's packets equally.", kind: "bool", group: "Bandwidth & sessions" },
  { key: "policy:NoBroadcastLimiter_bool", label: "Unlimited broadcasts", help: "Exempt this user from the hub's broadcast-storm limiter.", kind: "bool", group: "Bandwidth & sessions" },
  { key: "policy:MonitorPort_bool", label: "Monitoring mode", help: "The session may mirror the hub's traffic (a capture port).", kind: "bool", group: "Bandwidth & sessions" },

  // -- address limits --------------------------------------------------------------
  { key: "policy:MaxMac_u32", label: "MAC addresses", help: "How many MAC addresses this session may register.", kind: "number", zero: "unlimited", group: "Address limits" },
  { key: "policy:MaxIP_u32", label: "IPv4 addresses", help: "How many IPv4 addresses this session may register.", kind: "number", zero: "unlimited", group: "Address limits" },
  { key: "policy:MaxIPv6_u32", label: "IPv6 addresses", help: "How many IPv6 addresses this session may register.", kind: "number", zero: "unlimited", group: "IPv6" },
  { key: "policy:CheckMac_bool", label: "Enforce MAC limit", help: "Drop frames from MAC addresses beyond the limit.", kind: "bool", group: "Address limits" },
  { key: "policy:CheckIP_bool", label: "Enforce IPv4 limit", help: "Drop packets from IPv4 addresses beyond the limit.", kind: "bool", group: "Address limits" },
  { key: "policy:CheckIPv6_bool", label: "Enforce IPv6 limit", help: "Drop packets from IPv6 addresses beyond the limit.", kind: "bool", group: "IPv6" },

  // -- filtering ---------------------------------------------------------------------
  { key: "policy:DHCPFilter_bool", label: "Filter DHCP (IPv4)", help: "Drop DHCP packets crossing this session.", kind: "bool", group: "Filtering" },
  { key: "policy:DHCPNoServer_bool", label: "Deny DHCP server role", help: "The user may not answer DHCP requests.", kind: "bool", group: "Filtering" },
  { key: "policy:DHCPForce_bool", label: "Require DHCP addresses", help: "Only IPv4 addresses handed out by DHCP are accepted.", kind: "bool", group: "Filtering" },
  { key: "policy:NoBridge_bool", label: "Deny bridging", help: "Frames from other MAC addresses behind the client are dropped.", kind: "bool", group: "Filtering" },
  { key: "policy:NoRouting_bool", label: "Deny routing (IPv4)", help: "The user may not act as an IPv4 router.", kind: "bool", group: "Filtering" },
  { key: "policy:NoServer_bool", label: "Deny server role (IPv4)", help: "Inbound TCP connections to the user are blocked.", kind: "bool", group: "Filtering" },
  { key: "policy:PrivacyFilter_bool", label: "Privacy filter", help: "No traffic between users who both carry this policy.", kind: "bool", group: "Filtering" },
  { key: "policy:ArpDhcpOnly_bool", label: "Broadcasts: ARP/DHCP/ICMPv6 only", help: "Every other broadcast is dropped.", kind: "bool", group: "Filtering" },
  { key: "policy:FilterIPv4_bool", label: "Drop all IPv4", help: "IPv4 packets do not pass this session.", kind: "bool", group: "Filtering" },
  { key: "policy:FilterIPv6_bool", label: "Drop all IPv6", help: "IPv6 packets do not pass this session.", kind: "bool", group: "IPv6" },
  { key: "policy:FilterNonIP_bool", label: "Drop non-IP frames", help: "Anything that is not IPv4/IPv6/ARP is dropped.", kind: "bool", group: "Filtering" },

  // -- IPv6 ----------------------------------------------------------------------------
  { key: "policy:RSandRAFilter_bool", label: "Filter RS/RA", help: "Drop IPv6 router solicitations and advertisements.", kind: "bool", group: "IPv6" },
  { key: "policy:RAFilter_bool", label: "Filter RA", help: "Drop IPv6 router advertisements only.", kind: "bool", group: "IPv6" },
  { key: "policy:DHCPv6Filter_bool", label: "Filter DHCPv6", help: "Drop DHCPv6 packets crossing this session.", kind: "bool", group: "IPv6" },
  { key: "policy:DHCPv6NoServer_bool", label: "Deny DHCPv6 server role", help: "The user may not answer DHCPv6 requests.", kind: "bool", group: "IPv6" },
  { key: "policy:NoRoutingV6_bool", label: "Deny routing (IPv6)", help: "The user may not act as an IPv6 router.", kind: "bool", group: "IPv6" },
  { key: "policy:NoServerV6_bool", label: "Deny server role (IPv6)", help: "Inbound IPv6 connections to the user are blocked.", kind: "bool", group: "IPv6" },
  { key: "policy:NoIPv6DefaultRouterInRA_bool", label: "Strip default router from RA", help: "Router advertisements pass with the default-route bit cleared.", kind: "bool", group: "IPv6" },
  { key: "policy:NoIPv6DefaultRouterInRAWhenIPv6_bool", label: "Strip RA default router (IPv6 tunnels)", help: "Same, but only for physical IPv6 VPN connections.", kind: "bool", group: "IPv6" },

  // -- advanced ---------------------------------------------------------------------------
  { key: "policy:VLanId_u32", label: "VLAN ID", help: "Tag this user's traffic onto one VLAN.", kind: "number", zero: "none", group: "Advanced" },
  { key: "policy:Ver3_bool", label: "Version-3 policy", help: "Enables the newer policy fields above on old servers.", kind: "bool", group: "Advanced" },
];

/** Access-list rule fields, in the order a person reads a firewall rule. */
export const ACCESS_PROTOCOLS: Record<number, string> = {
  0: "any",
  1: "ICMPv4",
  6: "TCP",
  17: "UDP",
  58: "ICMPv6",
};

/** A session name like "SID-USER-123" is server plumbing; show the tail. */
export function shortSession(name: string): string {
  return name.replace(/^SID-/, "");
}

/** Cumulative user transfer, both directions, from EnumUser/GetUser output. */
export function userBytes(u: Wire): { send: number; recv: number } {
  const pick = (prefix: string) =>
    (Number(u[`${prefix}.BroadcastBytes_u64`]) || 0) + (Number(u[`${prefix}.UnicastBytes_u64`]) || 0);
  // EnumUser prefixes the counters with "Ex."; GetUser does not.
  const ex = "Ex.Recv.BroadcastBytes_u64" in u;
  return {
    send: pick(ex ? "Ex.Send" : "Send"),
    recv: pick(ex ? "Ex.Recv" : "Recv"),
  };
}

/** True when the wire datetime means "unset" (SoftEther uses epoch-ish zeros). */
export function isNever(iso: string | null | undefined): boolean {
  if (!iso) return true;
  const d = new Date(iso);
  return !isFinite(d.getTime()) || d.getFullYear() < 1990;
}

/** How usable an account is, worst last -- the order the status column sorts
 *  in, and the same precedence the status pill itself renders by. */
function userStateRank(u: Wire, online?: boolean): number {
  if (u.DenyAccess_bool) return 3;
  const expires = (u.Expires_dt ?? u.ExpireTime_dt) as string;
  if (!isNever(expires) && new Date(expires).getTime() < Date.now()) return 2;
  return (online ?? Boolean(u.Online_bool)) ? 0 : 1;
}

/**
 * What a user row is worth in a sorted column, keyed by the column.
 *
 * Both user tables sort through this, so "sorted by transfer" means the same
 * thing on a hub's list as on the server-wide one. Dates that mean "never"
 * sort to the end they belong at: never-logged-in is the oldest possible
 * login, never-expires is the furthest possible expiry.
 */
export function userSortValue(u: Wire, key: string, online?: boolean): string | number {
  switch (key) {
    case "user":
      return String(u.Name_str ?? "").toLowerCase();
    case "hub":
      return String(u.HubName_str ?? "").toLowerCase();
    case "status":
      return userStateRank(u, online);
    case "group":
      return String(u.GroupName_str ?? "").toLowerCase();
    case "auth":
      return AUTH_TYPES[Number(u.AuthType_u32)] ?? "";
    case "login": {
      const t = u.LastLoginTime_dt as string;
      return isNever(t) ? 0 : new Date(t).getTime();
    }
    case "logins":
      return Number(u.NumLogin_u32) || 0;
    case "transfer": {
      const b = userBytes(u);
      return b.send + b.recv;
    }
    case "expires": {
      const e = (u.Expires_dt ?? u.ExpireTime_dt) as string;
      // Not Infinity: the numeric comparator subtracts, and Infinity minus
      // Infinity is NaN, which makes a sort silently incoherent.
      return isNever(e) ? Number.MAX_SAFE_INTEGER : new Date(e).getTime();
    }
    default:
      return "";
  }
}
