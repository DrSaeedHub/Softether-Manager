/**
 * The one door to the backend.
 *
 * Paths are *relative* ("api/v1/..."): the exported page lives at the install
 * prefix's root, so the browser resolves them under whatever prefix this panel
 * happens to be served from -- no build-time base, no runtime discovery.
 * `NEXT_PUBLIC_API_BASE` overrides for the dev server, where the UI runs on
 * :3000 and the API on :8000.
 */

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";
const TOKEN_KEY = "sem_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  seError: string;
  constructor(status: number, message: string, seError = "") {
    super(message);
    this.status = status;
    this.seError = seError;
  }
}

export type Wire = Record<string, any>;

async function request<T = Wire>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${BASE}api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401) {
    clearToken();
    if (!path.includes("/auth/")) {
      window.dispatchEvent(new CustomEvent("sem-unauthorized"));
    }
  }

  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    let seError = "";
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") detail = data.detail;
      else if (data?.detail?.message) {
        detail = data.detail.message;
        seError = data.detail.se_error ?? "";
      } else if (data?.detail) detail = JSON.stringify(data.detail);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail, seError);
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

export interface Probe {
  online: boolean;
  configured: boolean;
  error?: string;
  version?: string;
  hostname?: string;
  sessions?: number;
  users?: number;
  hubs?: number;
  send_bytes?: number;
  recv_bytes?: number;
  started?: string | null;
  current_time?: string | null;
}

export interface UsagePoint {
  t: string;
  send: number;
  recv: number;
  sessions?: number;
}

export interface Usage {
  hours: number;
  points: UsagePoint[];
  total_send: number;
  total_recv: number;
  samples: number;
}

/** What a traffic quota counts. `total` is the two directions added up. */
export type QuotaMetric = "total" | "download" | "upload";
export type QuotaUnit = "MB" | "GB" | "TB";

/** A byte ceiling on a hub or on one user's config, and how far into it the
 *  subject has got this cycle. */
export interface Quota {
  subject: "hub" | "user";
  hub: string;
  username: string;
  limit_bytes: number;
  /** The same limit as the operator typed it: a number and a unit. */
  limit: number;
  unit: QuotaUnit;
  metric: QuotaMetric;
  enabled: boolean;
  upload_bytes: number;
  download_bytes: number;
  /** Only what `metric` counts — this is what the limit is compared against. */
  used_bytes: number;
  remaining_bytes: number | null;
  percent: number;
  exceeded: boolean;
  exceeded_date: string | null;
  /** Whether the panel has actually cut the subject off. */
  blocked: boolean;
  blocked_date: string | null;
  cycle_start: string;
  updated_date: string;
}

export interface QuotaIn {
  limit: number;
  unit: QuotaUnit;
  metric: QuotaMetric;
  enabled: boolean;
}

const hubPath = (hub: string) => `/hubs/${encodeURIComponent(hub)}`;

export const api = {
  // -- auth ------------------------------------------------------------------
  authState: () => request<{ setup_required: boolean }>("GET", "/auth/state"),
  setup: (username: string, password: string) =>
    request<{ token: string; username: string }>("POST", "/auth/setup", { username, password }),
  login: (username: string, password: string) =>
    request<{ token: string; username: string }>("POST", "/auth/login", { username, password }),
  logout: () => request("POST", "/auth/logout"),
  me: () => request<{ username: string }>("GET", "/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    request("PUT", "/auth/password", { current_password, new_password }),

  // -- the panel itself --------------------------------------------------------
  systemInfo: () => request("GET", "/system/info"),
  resources: () => request("GET", "/system/resources"),
  vpnTemplate: () => request<Wire>("GET", "/system/vpn-template"),
  saveVpnTemplate: (body: Wire) => request<Wire>("PUT", "/system/vpn-template", body),
  settings: () => request("GET", "/system/settings"),
  saveSettings: (body: Wire) => request("PUT", "/system/settings", body),
  updateStatus: () => request("GET", "/system/update"),
  updateCheck: () => request("POST", "/system/update/check"),
  updateApply: (version = "") => request("POST", "/system/update/apply", { version }),
  updateState: () => request("GET", "/system/update/state"),
  restartPanel: () => request("POST", "/system/restart"),
  audit: (beforeId = 0) =>
    request<Wire[]>("GET", `/system/audit?limit=100${beforeId ? `&before_id=${beforeId}` : ""}`),

  // -- the connection to SoftEther ----------------------------------------------
  connection: () => request<{ host: string; port: number; configured: boolean }>("GET", "/server/connection"),
  saveConnection: (body: Wire) => request("PUT", "/server/connection", body),
  testConnection: (body: Wire) => request("POST", "/server/connection/test", body),
  probe: () => request<Probe>("POST", "/server/probe"),

  // -- server-level SoftEther -----------------------------------------------------
  overview: () => request("GET", "/server/overview"),
  serverInfo: () => request("GET", "/server/info"),
  serverStatus: () => request("GET", "/server/status"),
  caps: () => request("GET", "/server/caps"),
  listeners: () => request("GET", "/server/listeners"),
  createListener: (port: number, enable = true) =>
    request("POST", "/server/listeners", { port, enable }),
  toggleListener: (port: number, enable: boolean) =>
    request("PUT", `/server/listeners/${port}`, { port, enable }),
  deleteListener: (port: number) => request("DELETE", `/server/listeners/${port}`),
  specialListener: () => request("GET", "/server/special-listener"),
  setSpecialListener: (body: Wire) => request("PUT", "/server/special-listener", body),
  ipsec: () => request("GET", "/server/ipsec"),
  setIpsec: (body: Wire) => request("PUT", "/server/ipsec", body),
  openvpn: () => request("GET", "/server/openvpn"),
  setOpenvpn: (body: Wire) => request("PUT", "/server/openvpn", body),
  openvpnSample: () =>
    request<{ filename: string; zip_base64: string }>("POST", "/server/openvpn/sample-config"),
  azure: () => request("GET", "/server/azure"),
  setAzure: (enabled: boolean) => request("PUT", "/server/azure", { IsEnabled_bool: enabled }),
  ddns: () => request("GET", "/server/ddns"),
  setDdnsHostname: (hostname: string) => request("PUT", "/server/ddns/hostname", { hostname }),
  ddnsProxy: () => request("GET", "/server/ddns/proxy"),
  setDdnsProxy: (body: Wire) => request("PUT", "/server/ddns/proxy", body),
  cipher: () => request("GET", "/server/cipher"),
  setCipher: (cipher: string) => request("PUT", "/server/cipher", { cipher }),
  cert: () => request("GET", "/server/cert"),
  setCert: (cert_base64: string, key_base64: string) =>
    request("PUT", "/server/cert", { cert_base64, key_base64 }),
  regenerateCert: (common_name: string) =>
    request("POST", "/server/cert/regenerate", { common_name }),
  setAdminPassword: (password: string) => request("PUT", "/server/admin-password", { password }),
  connections: () => request("GET", "/server/connections"),
  connectionInfo: (name: string) =>
    request("GET", `/server/connections/${encodeURIComponent(name)}`),
  disconnectConnection: (name: string) =>
    request("DELETE", `/server/connections/${encodeURIComponent(name)}`),
  bridges: () => request("GET", "/server/bridges"),
  bridgeSupport: () => request("GET", "/server/bridges/support"),
  bridgeDevices: () => request("GET", "/server/bridges/devices"),
  addBridge: (device: string, hub: string) => request("POST", "/server/bridges", { device, hub }),
  deleteBridge: (device: string, hub: string) =>
    request("POST", "/server/bridges/delete", { device, hub }),
  l3: () => request("GET", "/server/l3"),
  addL3: (name: string) => request("POST", "/server/l3", { name }),
  delL3: (name: string) => request("DELETE", `/server/l3/${encodeURIComponent(name)}`),
  startL3: (name: string) => request("POST", `/server/l3/${encodeURIComponent(name)}/start`),
  stopL3: (name: string) => request("POST", `/server/l3/${encodeURIComponent(name)}/stop`),
  l3Ifs: (name: string) => request("GET", `/server/l3/${encodeURIComponent(name)}/interfaces`),
  addL3If: (name: string, body: Wire) =>
    request("POST", `/server/l3/${encodeURIComponent(name)}/interfaces`, body),
  delL3If: (name: string, body: Wire) =>
    request("POST", `/server/l3/${encodeURIComponent(name)}/interfaces/delete`, body),
  l3Routes: (name: string) => request("GET", `/server/l3/${encodeURIComponent(name)}/routes`),
  addL3Route: (name: string, body: Wire) =>
    request("POST", `/server/l3/${encodeURIComponent(name)}/routes`, body),
  delL3Route: (name: string, body: Wire) =>
    request("POST", `/server/l3/${encodeURIComponent(name)}/routes/delete`, body),
  farm: () => request("GET", "/server/farm"),
  setFarm: (body: Wire) => request("PUT", "/server/farm", body),
  farmStatus: () => request("GET", "/server/farm/status"),
  farmMembers: () => request("GET", "/server/farm/members"),
  keepalive: () => request("GET", "/server/keepalive"),
  setKeepalive: (body: Wire) => request("PUT", "/server/keepalive", body),
  syslog: () => request("GET", "/server/syslog"),
  setSyslog: (body: Wire) => request("PUT", "/server/syslog", body),
  etherip: () => request("GET", "/server/etherip"),
  addEtherip: (body: Wire) => request("POST", "/server/etherip", body),
  deleteEtherip: (key: string) => request("DELETE", `/server/etherip/${encodeURIComponent(key)}`),
  logFiles: () => request("GET", "/server/logs"),
  readLog: (path: string, offset = 0) =>
    request<{ path: string; offset: number; text: string; bytes: number }>(
      "GET",
      `/server/logs/read?path=${encodeURIComponent(path)}&offset=${offset}`,
    ),
  getConfig: () => request<{ filename: string; config_base64: string }>("GET", "/server/config"),
  setConfig: (config_base64: string) => request("PUT", "/server/config", { config_base64 }),
  flush: () => request("POST", "/server/flush"),
  reboot: () => request("POST", "/server/reboot"),
  crashServer: () => request("POST", "/server/crash"),

  // -- every user on the server ---------------------------------------------------
  allUsers: () =>
    request<{ hubs: string[]; users: Wire[]; errors: { hub: string; error: string }[] }>(
      "GET",
      "/users",
    ),

  // -- hubs -------------------------------------------------------------------------
  hubs: () => request("GET", "/hubs"),
  createHub: (body: Wire) => request("POST", "/hubs", body),
  hub: (hub: string) => request("GET", hubPath(hub)),
  setHub: (hub: string, body: Wire) => request("PUT", hubPath(hub), body),
  deleteHub: (hub: string) => request("DELETE", hubPath(hub)),
  hubOnline: (hub: string, online: boolean) => request("PUT", `${hubPath(hub)}/online`, { online }),
  hubStatus: (hub: string) => request("GET", `${hubPath(hub)}/status`),
  hubTraffic: (hub: string, hours: number) =>
    request<Usage>("GET", `${hubPath(hub)}/traffic?hours=${hours}`),
  hubLog: (hub: string) => request("GET", `${hubPath(hub)}/log-settings`),
  setHubLog: (hub: string, body: Wire) => request("PUT", `${hubPath(hub)}/log-settings`, body),
  radius: (hub: string) => request("GET", `${hubPath(hub)}/radius`),
  setRadius: (hub: string, body: Wire) => request("PUT", `${hubPath(hub)}/radius`, body),
  hubMsg: (hub: string) => request<{ message: string }>("GET", `${hubPath(hub)}/message`),
  setHubMsg: (hub: string, message: string) =>
    request("PUT", `${hubPath(hub)}/message`, { message }),
  adminOptions: (hub: string) => request("GET", `${hubPath(hub)}/admin-options`),
  setAdminOptions: (hub: string, body: Wire) =>
    request("PUT", `${hubPath(hub)}/admin-options`, body),
  extOptions: (hub: string) => request("GET", `${hubPath(hub)}/ext-options`),
  setExtOptions: (hub: string, body: Wire) => request("PUT", `${hubPath(hub)}/ext-options`, body),

  // -- users / groups -----------------------------------------------------------------
  users: (hub: string) => request("GET", `${hubPath(hub)}/users`),
  createUser: (hub: string, body: Wire) => request("POST", `${hubPath(hub)}/users`, body),
  user: (hub: string, name: string) =>
    request("GET", `${hubPath(hub)}/users/${encodeURIComponent(name)}`),
  setUser: (hub: string, name: string, body: Wire) =>
    request("PUT", `${hubPath(hub)}/users/${encodeURIComponent(name)}`, body),
  deleteUser: (hub: string, name: string) =>
    request("DELETE", `${hubPath(hub)}/users/${encodeURIComponent(name)}`),
  hubUsersOnline: (hub: string) =>
    request<{ usernames: string[] }>("GET", `${hubPath(hub)}/users-online`),
  userSessions: (hub: string, name: string) =>
    request("GET", `${hubPath(hub)}/users/${encodeURIComponent(name)}/sessions`),
  userCredentialState: (hub: string, name: string) =>
    request<{ available: boolean; source: string | null }>(
      "GET",
      `${hubPath(hub)}/users/${encodeURIComponent(name)}/credential-state`,
    ),
  userVpnFile: (
    hub: string,
    name: string,
    body: { host: string; port: number; embed_password: boolean; password?: string; account_name?: string; filename?: string },
  ) =>
    request<{ filename: string; content: string }>(
      "POST",
      `${hubPath(hub)}/users/${encodeURIComponent(name)}/vpn-file`,
      body,
    ),
  userUsage: (hub: string, name: string, hours: number) =>
    request<Usage>("GET", `${hubPath(hub)}/users/${encodeURIComponent(name)}/usage?hours=${hours}`),
  sessionUsage: (hub: string, id: number) =>
    request<Usage & { session: Wire }>("GET", `${hubPath(hub)}/session-history/${id}/usage`),
  userSessionHistory: (hub: string, name: string, beforeId = 0) =>
    request<Wire[]>(
      "GET",
      `${hubPath(hub)}/users/${encodeURIComponent(name)}/session-history?limit=100${beforeId ? `&before_id=${beforeId}` : ""}`,
    ),
  groups: (hub: string) => request("GET", `${hubPath(hub)}/groups`),
  createGroup: (hub: string, body: Wire) => request("POST", `${hubPath(hub)}/groups`, body),
  group: (hub: string, name: string) =>
    request("GET", `${hubPath(hub)}/groups/${encodeURIComponent(name)}`),
  setGroup: (hub: string, name: string, body: Wire) =>
    request("PUT", `${hubPath(hub)}/groups/${encodeURIComponent(name)}`, body),
  deleteGroup: (hub: string, name: string) =>
    request("DELETE", `${hubPath(hub)}/groups/${encodeURIComponent(name)}`),

  // -- sessions ---------------------------------------------------------------------------
  sessions: (hub: string) => request("GET", `${hubPath(hub)}/sessions`),
  sessionStatus: (hub: string, name: string) =>
    request("GET", `${hubPath(hub)}/sessions/${encodeURIComponent(name)}`),
  killSession: (hub: string, name: string) =>
    request("DELETE", `${hubPath(hub)}/sessions/${encodeURIComponent(name)}`),

  // -- access control -----------------------------------------------------------------------
  accessList: (hub: string) => request("GET", `${hubPath(hub)}/access`),
  addAccess: (hub: string, rule: Wire) => request("POST", `${hubPath(hub)}/access`, rule),
  setAccessList: (hub: string, rules: Wire[]) =>
    request("PUT", `${hubPath(hub)}/access`, { AccessList: rules }),
  deleteAccess: (hub: string, ruleId: number) =>
    request("DELETE", `${hubPath(hub)}/access/${ruleId}`),
  acList: (hub: string) => request("GET", `${hubPath(hub)}/ac-list`),
  setAcList: (hub: string, rules: Wire[]) =>
    request("PUT", `${hubPath(hub)}/ac-list`, { ACList: rules }),

  // -- CA / CRL ---------------------------------------------------------------------------------
  cas: (hub: string) => request("GET", `${hubPath(hub)}/ca`),
  addCa: (hub: string, cert_base64: string) => request("POST", `${hubPath(hub)}/ca`, { cert_base64 }),
  getCa: (hub: string, key: number) => request("GET", `${hubPath(hub)}/ca/${key}`),
  deleteCa: (hub: string, key: number) => request("DELETE", `${hubPath(hub)}/ca/${key}`),
  crls: (hub: string) => request("GET", `${hubPath(hub)}/crl`),
  addCrl: (hub: string, body: Wire) => request("POST", `${hubPath(hub)}/crl`, body),
  deleteCrl: (hub: string, key: number) => request("DELETE", `${hubPath(hub)}/crl/${key}`),

  // -- SecureNAT ----------------------------------------------------------------------------------
  securenat: (hub: string) => request("GET", `${hubPath(hub)}/securenat`),
  securenatEnable: (hub: string, on: boolean) =>
    request("POST", `${hubPath(hub)}/securenat/${on ? "enable" : "disable"}`),
  setSecurenatOptions: (hub: string, body: Wire) =>
    request("PUT", `${hubPath(hub)}/securenat/options`, body),
  natTable: (hub: string) => request("GET", `${hubPath(hub)}/securenat/nat-table`),
  dhcpTable: (hub: string) => request("GET", `${hubPath(hub)}/securenat/dhcp-table`),

  // -- cascade links --------------------------------------------------------------------------------
  links: (hub: string) => request("GET", `${hubPath(hub)}/links`),
  createLink: (hub: string, body: Wire) => request("POST", `${hubPath(hub)}/links`, body),
  link: (hub: string, name: string) =>
    request("GET", `${hubPath(hub)}/links/${encodeURIComponent(name)}`),
  setLink: (hub: string, name: string, body: Wire) =>
    request("PUT", `${hubPath(hub)}/links/${encodeURIComponent(name)}`, body),
  deleteLink: (hub: string, name: string) =>
    request("DELETE", `${hubPath(hub)}/links/${encodeURIComponent(name)}`),
  linkStatus: (hub: string, name: string) =>
    request("GET", `${hubPath(hub)}/links/${encodeURIComponent(name)}/status`),
  linkOnline: (hub: string, name: string, online: boolean) =>
    request("PUT", `${hubPath(hub)}/links/${encodeURIComponent(name)}/online`, { online }),
  renameLink: (hub: string, name: string, newName: string) =>
    request("POST", `${hubPath(hub)}/links/${encodeURIComponent(name)}/rename`, { new_name: newName }),

  // -- address tables ----------------------------------------------------------------------------------
  macTable: (hub: string) => request("GET", `${hubPath(hub)}/mac-table`),
  deleteMac: (hub: string, key: number) => request("DELETE", `${hubPath(hub)}/mac-table/${key}`),
  ipTable: (hub: string) => request("GET", `${hubPath(hub)}/ip-table`),
  deleteIp: (hub: string, key: number) => request("DELETE", `${hubPath(hub)}/ip-table/${key}`),

  // -- traffic quotas ----------------------------------------------------------------------
  quotas: () => request<Quota[]>("GET", "/quotas"),
  hubQuota: (hub: string) => request<Quota>("GET", `/quotas/hub/${encodeURIComponent(hub)}`),
  setHubQuota: (hub: string, body: QuotaIn) =>
    request<Quota>("PUT", `/quotas/hub/${encodeURIComponent(hub)}`, body),
  deleteHubQuota: (hub: string) => request("DELETE", `/quotas/hub/${encodeURIComponent(hub)}`),
  resetHubQuota: (hub: string) =>
    request<Quota>("POST", `/quotas/hub/${encodeURIComponent(hub)}/reset`),
  userQuota: (hub: string, name: string) =>
    request<Quota>("GET", `/quotas/user/${encodeURIComponent(hub)}/${encodeURIComponent(name)}`),
  setUserQuota: (hub: string, name: string, body: QuotaIn) =>
    request<Quota>("PUT", `/quotas/user/${encodeURIComponent(hub)}/${encodeURIComponent(name)}`, body),
  deleteUserQuota: (hub: string, name: string) =>
    request("DELETE", `/quotas/user/${encodeURIComponent(hub)}/${encodeURIComponent(name)}`),
  resetUserQuota: (hub: string, name: string) =>
    request<Quota>("POST", `/quotas/user/${encodeURIComponent(hub)}/${encodeURIComponent(name)}/reset`),

  // -- the RPC console -----------------------------------------------------------------------------------
  rpcMethods: () => request<Wire[]>("GET", "/rpc/methods"),
  rpcCall: (method: string, params: Wire) => request("POST", "/rpc/call", { method, params }),
};
