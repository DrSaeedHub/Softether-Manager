# softether — Python client for the SoftEther VPN Server JSON-RPC API

A single-file, dependency-free Python library covering **all 135 RPC methods** of the
SoftEther VPN Server JSON-RPC API Suite, generated from the official suite document
(`softether api doc.htm`, the reference every VPN Server serves at `https://<host>:<port>/api/`).

* **Monolithic** — everything lives in `softether.py`: transport, TLS, error taxonomy,
  type coercion, enumerations, structure builders and every endpoint. Copy the file into
  your project; there is nothing to install.
* **Complete** — every documented method, with every documented argument, typed and
  documented in its own docstring.
* **Standard library only** — Python 3.8+, no `requests`, no `pip install`.

```
softether.py            the library (this is the deliverable)
test_softether.py       41 tests, run offline against a local HTTPS stand-in server
example.py             a worked end-to-end tour of the API
spec.json               the machine-readable API description parsed from the document
build/                  the generator that turns the document into softether.py
softether api doc.htm   the source document
```

## Quick start

```python
from softether import SoftEtherClient, UserAuthType

with SoftEtherClient("127.0.0.1", 5555, password="adminpass") as vpn:
    print(vpn.get_server_info().ServerVersionString)

    vpn.create_hub("VPN1", admin_password_plain_text="hubpass", online=True)
    vpn.create_user("VPN1", "alice",
                    auth_type=UserAuthType.PASSWORD_AUTHENTICATION,
                    auth_password="s3cr3t",
                    note="created by the ops script")

    for session in vpn.enum_session("VPN1").SessionList:
        print(session.Name, session.ClientIP, session.PacketSize)
```

Virtual Hub administrator mode — a hub password instead of the server password:

```python
vpn = SoftEtherClient("127.0.0.1", 5555, password="hubpass", hub="VPN1")
```

From the shell:

```bash
python softether.py -H 127.0.0.1 -p adminpass GetServerStatus
python softether.py -H 127.0.0.1 -p adminpass EnumUser '{"HubName_str":"VPN1"}'
python softether.py --list          # every method and its Python name
```

## Naming

Every RPC is available under a `snake_case` name and under its exact RPC name:

```python
vpn.get_hub_status("VPN1")     # idiomatic
vpn.GetHubStatus("VPN1")       # matches the document
vpn.call("GetHubStatus", {"HubName_str": "VPN1"})   # raw escape hatch
```

Arguments are the wire fields with the type suffix dropped and converted to `snake_case`:
`HubName_str` → `hub_name`, `policy:MaxUpload_u32` → `policy_max_upload`,
`AdminPasswordPlainText_str` → `admin_password_plain_text`. Identifying arguments are
positional; everything else is keyword-only and defaults to *not sent*.

## Types

The API encodes each field's type in the field name; the client converts both ways.

| Suffix | You pass | You get back |
|---|---|---|
| `_u32` / `_u64` | `int`, range-checked | `int` |
| `_str` | `str`, ASCII-checked | `str` |
| `_utf` | `str` | `str` |
| `_bin` | `bytes` (or an existing base64 `str`) | `bytes` |
| `_ip` | `"10.0.0.1"` or an `ipaddress` object | `str` |
| `_bool` | `bool` | `bool` |
| `_dt` | `datetime`, ISO string or POSIX timestamp | `datetime` (naive UTC), or `None` when unset |

```python
vpn.set_server_cert(cert=open("cert.cer", "rb").read(),
                    key=open("key.pem", "rb").read())

config = vpn.get_config()
open("vpn_server.config", "wb").write(config.FileData_bin)   # already bytes
```

Results are `RpcResult`, a `dict` you can also read as attributes — by wire name, by name
without the suffix, or in `snake_case`. `result.raw` keeps the untouched JSON.

```python
status = vpn.get_hub_status("VPN1")
status["NumSessions_u32"], status.NumSessions_u32, status.NumSessions, status.num_sessions
```

## Enumerations

Documented enumerations are real `IntEnum` classes and are validated before anything is sent:

`ServerType`, `OsType`, `HubType`, `ConnectionType`, `LogSwitchType`, `PacketLogConfig`,
`PacketLogIndex`, `ProxyType`, `ClientAuthType`, `UserAuthType`, `SessionStatus`,
`AccessProtocol`, `NatProtocol`, `NatTcpStatus`, `KeepConnectProtocol`, `SysLogSaveType`.

```python
from softether import UserAuthType, LogSwitchType, PacketLogConfig, PacketLogIndex

vpn.create_user("VPN1", "bob", auth_type=UserAuthType.RADIUS_AUTHENTICATION)

packet_log = [PacketLogConfig.NOT_SAVE] * 16
packet_log[PacketLogIndex.TCP_CONNECTION] = PacketLogConfig.ONLY_HEADER
vpn.set_hub_log("VPN1", save_security_log=True,
                security_log_switch_type=LogSwitchType.DAILY_BASIS,
                save_packet_log=True, packet_log_config=packet_log)
```

Passing an undocumented value raises `ValidationError` before the request leaves the
process. Talking to a newer server than the document? Construct the client with
`strict=False`.

## List-of-structure arguments

Four RPCs take a list of records. Build them with the helpers — keys may be wire names,
names without the suffix, or `snake_case`:

```python
from softether import access_rule, ac_rule, admin_option, AccessProtocol

vpn.set_access_list("VPN1", [
    access_rule(priority=100, discard=False, protocol=AccessProtocol.TCP,
                dest_port_start=443, dest_port_end=443, note="allow https"),
    access_rule(priority=200, discard=True, note="drop the rest"),
])

vpn.set_ac_list("VPN1", [ac_rule(priority=1, deny=False, masked=True,
                                 ip_address="10.0.0.0", subnet_mask="255.255.255.0")])

vpn.set_hub_admin_options("VPN1", [admin_option(name="deny_empty_password", value=1)])
```

## Errors

Everything raised inherits from `SoftEtherError`; no call ever signals failure by
returning `None`.

```
SoftEtherError
├── ValidationError          bad argument, nothing was sent (also a ValueError)
├── TransportError
│   ├── ConnectionFailedError
│   ├── TimeoutError         (also exported as RpcTimeoutError)
│   ├── TLSError
│   │   └── CertificateFingerprintError
│   └── HTTPError            non-200 status
├── AuthenticationError      password rejected (HTTP 401/403 or ERR_AUTH_FAILED)
├── ApiDisabledError         HTTP 404 — /api/ not enabled, or the server predates it
├── ProtocolError            the answer was not valid JSON-RPC 2.0
└── RpcError                 the server refused the call — .code, .name, .message, .method
    ├── NotFoundError
    ├── AlreadyExistsError
    ├── NotSupportedError
    ├── PermissionError      (also exported as AccessDeniedError)
    ├── BusyError
    └── InvalidParameterError
```

```python
from softether import NotFoundError, RpcError

try:
    vpn.delete_user("VPN1", "ghost")
except NotFoundError:
    pass                       # already gone
except RpcError as exc:
    print(exc.name, exc.code, exc.message)
```

The suite document does not publish the numeric error table, so `RpcError` subclasses are
chosen from the `ERR_*` symbol the server puts in `error.message`. An unrecognised error
surfaces as a plain `RpcError` carrying `code` and `message` verbatim — never swallowed,
never guessed at.

`PermissionError` and `TimeoutError` intentionally reuse the standard-library names, which
is convenient when catching them explicitly. `from softether import *` exports the
`AccessDeniedError` / `RpcTimeoutError` aliases instead, so the builtins are never shadowed.

## TLS

SoftEther ships a self-signed certificate, so `verify` defaults to `False` and the client
warns once (`InsecureTLSWarning`). Two ways to authenticate the server properly:

```python
# pin the certificate — ideal for a self-signed local server
SoftEtherClient("10.0.0.5", 5555, password, fingerprint="9f86d0…")

# or verify against a CA
SoftEtherClient("vpn.example.com", 443, password, verify=True, ca_file="ca.pem")
```

Get the fingerprint with
`openssl s_client -connect host:5555 </dev/null | openssl x509 -noout -fingerprint -sha256`.

## Connection handling and retries

One keep-alive HTTPS connection per client, guarded by a lock, so a client is safe to share
between threads (calls serialise). Retry policy:

* A reused connection that dies before a single response byte arrived is always retried —
  the request provably never reached the server.
* Beyond that, only side-effect-free methods (`Get*`, `Enum*`, `Test`, `Read*`, `Make*`)
  are retried. A retried `CreateUser` could report "already exists" for a call that in fact
  succeeded, so state-changing calls are not retried unless you pass `retry_unsafe=True`.

```python
SoftEtherClient(host, port, password, timeout=15, retries=3, retry_backoff=0.5)
```

## Two notes on the source document

* **`SetCrl`** — the document prints an empty parameter object, contradicting its own
  description and parameter table. `set_crl()` takes the fields the document lists in the
  result, which is what the server expects.
* **`GetVgsConfig` / `SetVgsConfig`** — listed in the table of contents, but the reference
  body carries no section for them, so no parameters could be generated. They are exposed
  as `get_vgs_config()` and `set_vgs_config(**wire_fields)`, which pass through to `call()`.
  Both are VPN Gate builds only; a stock server answers with an error.

Also note the upstream spelling `GetDDnsInternetSettng` / `SetDDnsInternetSettng`, kept
as-is and aliased to `get_ddns_internet_setting` / `set_ddns_internet_setting`.

## Tests

```bash
python -m unittest test_softether -v
```

41 tests, fully offline. A local HTTPS server with a throw-away certificate stands in for
the VPN Server, so authentication, keep-alive, retries, timeouts, fingerprint pinning,
CA verification, malformed answers, the error taxonomy and the CLI are all exercised for
real — and every one of the 133 generated methods is called and its payload checked.
The certificate is generated with `cryptography`, the only test-time dependency; the
library itself needs nothing.

## Regenerating

```bash
python build/parse_doc.py "softether api doc.htm"   # document -> spec.json
python build/build.py                               # spec.json -> softether.py
```

`build/` holds the parser, the code generator and the hand-written parts of the module
(`header_a/b/c.py`, `footer.py`). Edit those rather than `softether.py`, which is generated.
