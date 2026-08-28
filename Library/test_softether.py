# -*- coding: utf-8 -*-
"""Test suite for the softether library.

Runs entirely offline: a local HTTPS server impersonates the VPN Server's
``/api/`` endpoint, so transport, TLS, authentication, retries and the error
taxonomy are exercised for real.

    python -m unittest test_softether -v
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import inspect
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import Library.softether as se


# --------------------------------------------------------------------------
# A throw-away self-signed certificate
# --------------------------------------------------------------------------
def make_cert():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    directory = tempfile.mkdtemp(prefix="softether-test-")
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_path, key_path, fingerprint


CERT, KEY, FINGERPRINT = make_cert()

PASSWORD = "adminpass"

#: The example result the suite document prints for each RPC, keyed by method.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec.json"),
          encoding="utf-8") as _fh:
    SPEC = {entry["name"]: entry for entry in json.load(_fh)}
DOC_RESULTS = {name: entry["output"] for name, entry in SPEC.items()}


class FakeVpnServer(threading.Thread):
    """Minimal stand-in for the SoftEther JSON-RPC endpoint."""

    def __init__(self):
        super().__init__(daemon=True)
        self.requests = []          # [(headers, payload), ...]
        # ok | doc | error | badjson | badid | http500 | notfound | close | hang
        self.behaviour = "ok"
        self.result = {}
        self.connections = 0
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT, KEY)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                server.requests.append((dict(self.headers), payload))

                mode = server.behaviour
                password = self.headers.get("X-VPNADMIN-PASSWORD")
                auth = self.headers.get("Authorization")
                if auth and auth.startswith("Basic "):
                    password = base64.b64decode(auth[6:]).decode().split(":", 1)[1]
                # "doc" mode replays the document's own example results and is
                # used to exercise client code end to end, including logins to a
                # Virtual Hub with its own password, so it accepts any password.
                if mode != "doc" and password != PASSWORD:
                    return self.send_body(401, b'{"error":"unauthorized"}')

                if mode == "hang":
                    time.sleep(5)
                if mode in ("close", "close-once"):
                    # "close-once" flips back before the connection is killed,
                    # so exactly one request dies -- deterministic, where a
                    # timer would race the client's retry on a fast loopback.
                    if mode == "close-once":
                        server.behaviour = "ok"
                    self.close_connection = True
                    self.wfile.close()
                    self.connection.close()
                    return
                if mode == "notfound":
                    return self.send_body(404, b"not found")
                if mode == "http500":
                    return self.send_body(500, b"boom")
                if mode == "badjson":
                    return self.send_body(200, b"<html>proxy error</html>")
                body = {"jsonrpc": "2.0", "id": payload["id"]}
                if mode == "badid":
                    body["id"] = "999999"
                    body["result"] = {}
                elif mode == "doc":
                    body["result"] = DOC_RESULTS.get(payload["method"], {})
                elif mode == "error":
                    body["error"] = server.result or {"code": 32, "message": "ERR_HUB_NOT_FOUND"}
                else:
                    body["result"] = server.result if server.result else payload["params"]
                return self.send_body(200, json.dumps(body).encode("utf-8"))

            def send_body(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.socket.getsockname()[1]

    def run(self):
        self.httpd.serve_forever(poll_interval=0.05)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def last_params(self):
        return self.requests[-1][1]["params"]


SERVER = None


def setUpModule():
    global SERVER
    SERVER = FakeVpnServer()
    SERVER.start()


def tearDownModule():
    SERVER.stop()


def client(**kwargs):
    kwargs.setdefault("suppress_insecure_warning", True)
    kwargs.setdefault("password", PASSWORD)
    return se.SoftEtherClient("localhost", SERVER.port, **kwargs)


# ==========================================================================
class TestCoercion(unittest.TestCase):
    def test_uint_range(self):
        self.assertEqual(se._pack("Test", {"IntValue_u32": 7}), {"IntValue_u32": 7})
        with self.assertRaises(se.ValidationError):
            se._pack("Test", {"IntValue_u32": -1})
        with self.assertRaises(se.ValidationError):
            se._pack("Test", {"IntValue_u32": 2 ** 32})
        with self.assertRaises(se.ValidationError):
            se._pack("Test", {"IntValue_u32": "seven"})

    def test_ascii_vs_utf(self):
        self.assertEqual(se._pack("GetHub", {"HubName_str": "VPN"}), {"HubName_str": "VPN"})
        with self.assertRaises(se.ValidationError):
            se._pack("GetHub", {"HubName_str": "ولاية"})
        self.assertEqual(se._pack("CreateUser", {"Note_utf": "ملاحظة"})["Note_utf"], "ملاحظة")

    def test_binary(self):
        packed = se._pack("AddCa", {"Cert_bin": b"hello"})
        self.assertEqual(packed["Cert_bin"], base64.b64encode(b"hello").decode())
        self.assertEqual(se._pack("AddCa", {"Cert_bin": "aGVsbG8="})["Cert_bin"], "aGVsbG8=")
        with self.assertRaises(se.ValidationError):
            se._pack("AddCa", {"Cert_bin": "not base64!!"})

    def test_ip(self):
        self.assertEqual(se._pack("AddL3If", {"IpAddress_ip": "10.0.0.1"})["IpAddress_ip"], "10.0.0.1")
        with self.assertRaises(se.ValidationError):
            se._pack("AddL3If", {"IpAddress_ip": "10.0.0.999"})

    def test_datetime(self):
        moment = datetime.datetime(2026, 8, 23, 7, 42, 34, 123000)
        self.assertEqual(se._pack("CreateUser", {"ExpireTime_dt": moment})["ExpireTime_dt"],
                         "2026-08-23T07:42:34.123")
        aware = moment.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=3)))
        self.assertEqual(se._pack("CreateUser", {"ExpireTime_dt": aware})["ExpireTime_dt"],
                         "2026-08-23T04:42:34.123")

    def test_bool(self):
        self.assertIs(se._pack("SetHubOnline", {"Online_bool": True})["Online_bool"], True)
        with self.assertRaises(se.ValidationError):
            se._pack("SetHubOnline", {"Online_bool": 5})

    def test_enum_validation(self):
        packed = se._pack("CreateUser", {"AuthType_u32": se.UserAuthType.RADIUS_AUTHENTICATION})
        self.assertEqual(packed["AuthType_u32"], 4)
        with self.assertRaises(se.ValidationError) as ctx:
            se._pack("CreateUser", {"AuthType_u32": 99})
        self.assertIn("UserAuthType", str(ctx.exception))
        # relaxed mode lets anything through
        self.assertEqual(se._pack("CreateUser", {"AuthType_u32": 99}, strict=False)["AuthType_u32"], 99)

    def test_scalar_list(self):
        packed = se._pack("SetHubLog", {"PacketLogConfig_u32": [1, 1, 2, 0, 0, 0, 0, 0]})
        self.assertEqual(len(packed["PacketLogConfig_u32"]), 8)
        with self.assertRaises(se.ValidationError):
            se._pack("SetHubLog", {"PacketLogConfig_u32": [9]})
        with self.assertRaises(se.ValidationError):
            se._pack("SetHubLog", {"PacketLogConfig_u32": list(range(3)) * 10})

    def test_struct_list_aliases(self):
        rule = {"Priority": 10, "discard": False, "SrcIpAddress_ip": "10.0.0.0",
                "src_subnet_mask": "255.0.0.0", "Protocol_u32": se.AccessProtocol.TCP}
        packed = se._pack("AddAccess", {"AccessListSingle": [rule]})
        entry = packed["AccessListSingle"][0]
        self.assertEqual(entry["Priority_u32"], 10)
        self.assertIs(entry["Discard_bool"], False)
        self.assertEqual(entry["SrcSubnetMask_ip"], "255.0.0.0")
        self.assertEqual(entry["Protocol_u32"], 6)

    def test_struct_unknown_key(self):
        with self.assertRaises(se.ValidationError):
            se._pack("AddAccess", {"AccessListSingle": [{"Nope": 1}]})
        self.assertEqual(se._pack("AddAccess", {"AccessListSingle": [{"Nope": 1}]},
                                  strict=False)["AccessListSingle"], [{}])

    def test_builders(self):
        rule = se.access_rule(priority=5, discard=True, protocol=se.AccessProtocol.UDP)
        self.assertEqual(rule, {"Priority_u32": 5, "Discard_bool": True,
                                "Protocol_u32": se.AccessProtocol.UDP})
        self.assertEqual(se.admin_option(name="no_securenat", value=1),
                         {"Name_str": "no_securenat", "Value_u32": 1})
        self.assertEqual(se.ac_rule(deny=True, ip_address="1.2.3.4"),
                         {"Deny_bool": True, "IpAddress_ip": "1.2.3.4"})


class TestRpcResult(unittest.TestCase):
    def test_decoding(self):
        result = se.RpcResult({
            "HubName_str": "VPN",
            "Cert_bin": base64.b64encode(b"DER").decode(),
            "CreatedTime_dt": "2026-08-23T07:42:34.123",
            "ExpireTime_dt": "0001-01-01T00:00:00.000",
            "HubList": [{"Name_str": "A", "NumUsers_u32": 3}],
        })
        self.assertEqual(result.Cert_bin, b"DER")
        self.assertEqual(result.CreatedTime, datetime.datetime(2026, 8, 23, 7, 42, 34, 123000))
        self.assertIsNone(result.ExpireTime_dt)
        self.assertEqual(result.hub_name, "VPN")
        self.assertEqual(result.HubList[0].Name, "A")
        self.assertEqual(result.raw["Cert_bin"], base64.b64encode(b"DER").decode())
        with self.assertRaises(AttributeError):
            result.Nonexistent
        self.assertEqual(result.field("Nonexistent", "fallback"), "fallback")


class TestErrorMapping(unittest.TestCase):
    def test_symbols(self):
        cases = {
            "ERR_HUB_NOT_FOUND": se.NotFoundError,
            "ERR_OBJECT_NOT_FOUND": se.NotFoundError,
            "ERR_HUB_ALREADY_EXISTS": se.AlreadyExistsError,
            "ERR_AUTH_FAILED": se.AuthenticationError,
            "ERR_NOT_ENOUGH_RIGHT": se.PermissionError,
            "ERR_NOT_SUPPORTED": se.NotSupportedError,
            "ERR_TOO_MANY_USER": se.BusyError,
            "ERR_INVALID_PARAMETER": se.InvalidParameterError,
            "ERR_SOMETHING_BRAND_NEW": se.RpcError,
        }
        for message, expected in cases.items():
            error = se._rpc_error(7, message, "GetHub")
            self.assertIsInstance(error, expected, message)
            self.assertEqual(error.code, 7)
            self.assertEqual(error.name, message)


class TestTransport(unittest.TestCase):
    def setUp(self):
        SERVER.behaviour = "ok"
        SERVER.result = {}
        SERVER.requests.clear()

    def test_round_trip_and_headers(self):
        with client(hub="VPN1") as c:
            result = c.get_hub("VPN1")
        headers, payload = SERVER.requests[-1]
        self.assertEqual(headers["X-VPNADMIN-HUBNAME"], "VPN1")
        self.assertEqual(headers["X-VPNADMIN-PASSWORD"], PASSWORD)
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertEqual(payload["method"], "GetHub")
        self.assertEqual(payload["params"], {"HubName_str": "VPN1"})
        self.assertEqual(result.HubName_str, "VPN1")

    def test_basic_auth(self):
        with client(auth="basic") as c:
            c.get_server_info()
        self.assertIn("Authorization", SERVER.requests[-1][0])

    def test_keep_alive_reuses_one_connection(self):
        with client() as c:
            for _ in range(3):
                c.get_server_status()
            self.assertIsNotNone(c._conn)
        self.assertEqual(len(SERVER.requests), 3)

    def test_bad_password(self):
        with client(password="wrong") as c:
            with self.assertRaises(se.AuthenticationError):
                c.get_server_info()

    def test_http_404_means_api_disabled(self):
        SERVER.behaviour = "notfound"
        with client() as c:
            with self.assertRaises(se.ApiDisabledError):
                c.get_server_info()

    def test_http_500(self):
        SERVER.behaviour = "http500"
        with client(retries=0) as c:
            with self.assertRaises(se.HTTPError) as ctx:
                c.get_server_info()
        self.assertEqual(ctx.exception.status, 500)

    def test_non_json_body(self):
        SERVER.behaviour = "badjson"
        with client() as c:
            with self.assertRaises(se.ProtocolError):
                c.get_server_info()

    def test_id_mismatch(self):
        SERVER.behaviour = "badid"
        with client() as c:
            with self.assertRaises(se.ProtocolError):
                c.get_server_info()

    def test_rpc_error(self):
        SERVER.behaviour = "error"
        SERVER.result = {"code": 32, "message": "ERR_HUB_NOT_FOUND"}
        with client() as c:
            with self.assertRaises(se.NotFoundError) as ctx:
                c.get_hub("missing")
        self.assertEqual(ctx.exception.method, "GetHub")
        self.assertEqual(ctx.exception.code, 32)

    def test_timeout(self):
        SERVER.behaviour = "hang"
        with client(timeout=0.3, retries=0) as c:
            with self.assertRaises(se.TimeoutError):
                c.get_server_info()
        SERVER.behaviour = "ok"

    def test_dead_port(self):
        """A port nothing listens on: refused on most systems, dropped on some."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        c = se.SoftEtherClient("127.0.0.1", dead_port, PASSWORD,
                               retries=0, timeout=2, suppress_insecure_warning=True)
        with self.assertRaises(se.TransportError):
            c.get_server_info()

    def test_stale_connection_is_retried_even_for_unsafe_methods(self):
        with client(retries=2, retry_backoff=0) as c:
            c.get_server_info()                      # opens and uses the connection
            SERVER.behaviour = "close-once"          # next request dies mid-flight
            result = c.create_hub("VPN1", admin_password_plain_text="x")
        self.assertEqual(result.HubName_str, "VPN1")

    def test_unsafe_method_is_not_retried_on_a_fresh_connection(self):
        SERVER.behaviour = "close"
        c = client(retries=3, retry_backoff=0)
        with self.assertRaises(se.TransportError):
            c.create_hub("VPN1", admin_password_plain_text="x")
        attempts = len(SERVER.requests)
        self.assertEqual(attempts, 1)
        c.close()
        SERVER.behaviour = "ok"

    def test_fingerprint_pinning(self):
        with client(fingerprint=FINGERPRINT) as c:
            c.get_server_info()
        with client(fingerprint="00" * 32) as c:
            with self.assertRaises(se.CertificateFingerprintError):
                c.get_server_info()

    def test_verify_against_the_generated_ca(self):
        with client(verify=True, ca_file=CERT) as c:
            c.get_server_info()
        c = se.SoftEtherClient("localhost", SERVER.port, PASSWORD, verify=True,
                               retries=0, suppress_insecure_warning=True)
        with self.assertRaises(se.TLSError):
            c.get_server_info()

    def test_insecure_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            se.SoftEtherClient("localhost", SERVER.port, PASSWORD)
        self.assertTrue(any(issubclass(w.category, se.InsecureTLSWarning) for w in caught))

    def test_ping(self):
        SERVER.result = {}
        with client() as c:
            self.assertTrue(c.ping())

    def test_from_url_and_rpc_name_alias(self):
        c = se.SoftEtherClient.from_url("https://%s@localhost:%d/" % (PASSWORD, SERVER.port),
                                        suppress_insecure_warning=True)
        try:
            self.assertEqual(c.password, PASSWORD)
            self.assertEqual(c.GetServerInfo().__class__, se.RpcResult)
        finally:
            c.close()

    def test_call_escape_hatch(self):
        with client() as c:
            c.call("GetVgsConfig", {})
        self.assertEqual(SERVER.requests[-1][1]["method"], "GetVgsConfig")
        with client() as c:
            with self.assertRaises(se.ValidationError):
                c.call("", {})
            with self.assertRaises(se.ValidationError):
                c.call("GetHub", ["not", "a", "mapping"])


class TestGeneratedSurface(unittest.TestCase):
    """Every documented RPC is present, callable and sends what it was given."""

    SAMPLES = {
        "u32": 1, "u64": 1, "str": "x", "utf": "x", "bin": b"x",
        "ip": "10.0.0.1", "bool": True, "dt": datetime.datetime(2026, 1, 1),
    }

    def test_every_method_exists(self):
        self.assertEqual(len(se.RPC_METHODS), 135)
        for name in se.RPC_METHODS:
            python_name = se._RPC_TO_PYTHON[name]
            self.assertTrue(hasattr(se.SoftEtherClient, python_name), name)
            self.assertTrue(callable(getattr(se.SoftEtherClient, python_name)), name)

    def test_no_duplicate_python_names(self):
        names = [se._RPC_TO_PYTHON[n] for n in se.RPC_METHODS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_documented_field_is_a_parameter(self):
        spec = json.load(open(os.path.join(os.path.dirname(__file__), "spec.json"),
                              encoding="utf-8"))
        for entry in spec:
            method = getattr(se.SoftEtherClient, se._RPC_TO_PYTHON[entry["name"]])
            signature = inspect.signature(method)
            expected = len(entry["input"]) or (len(entry["output"]) if entry["name"] == "SetCrl" else 0)
            self.assertEqual(len(signature.parameters) - 1, expected, entry["name"])

    def test_call_every_method(self):
        """Drive all 133 generated methods against the fake server."""
        SERVER.behaviour = "ok"
        SERVER.result = {}
        spec = json.load(open(os.path.join(os.path.dirname(__file__), "spec.json"),
                              encoding="utf-8"))
        by_name = {entry["name"]: entry for entry in spec}
        with client() as c:
            for name in se.RPC_METHODS:
                entry = by_name.get(name)
                if entry is None:
                    continue
                method = getattr(c, se._RPC_TO_PYTHON[name])
                args = []
                for param in list(inspect.signature(method).parameters.values()):
                    if param.default is not inspect.Parameter.empty:
                        continue
                    args.append(self._sample(name, param.name))
                result = method(*args)
                self.assertIsInstance(result, se.RpcResult, name)
                self.assertEqual(SERVER.requests[-1][1]["method"], name)
                self.assertEqual(len(SERVER.last_params()), len(args), name)

    def _sample(self, method, arg):
        wire = self._wire_name(method, arg)
        if wire in se._STRUCT_FIELDS:
            return [{}]
        if wire in se._SCALAR_LIST_FIELDS:
            return [0]
        enum_cls = se._ENUM_FIELDS.get(method, {}).get(wire)
        if enum_cls is not None:
            return int(list(enum_cls)[0])
        kind = se._field_kind(wire)
        return self.SAMPLES[kind]

    @staticmethod
    def _wire_name(method, arg):
        spec = json.load(open(os.path.join(os.path.dirname(__file__), "spec.json"),
                              encoding="utf-8")) if not hasattr(TestGeneratedSurface, "_spec") \
            else TestGeneratedSurface._spec
        TestGeneratedSurface._spec = spec
        entry = next(e for e in spec if e["name"] == method)
        fields = list(entry["input"]) or list(entry["output"])
        for field in fields:
            base = field
            for suffix in ("_u32", "_u64", "_str", "_utf", "_bin", "_ip", "_bool", "_dt"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            if base.replace(":", "_").lower().replace("_", "") == arg.replace("_", ""):
                return field
        raise AssertionError("no wire field for %s.%s" % (method, arg))


class TestExampleScript(unittest.TestCase):
    """example.py runs end to end against results taken from the document."""

    def test_full_tour(self):
        import Library.example as example

        SERVER.behaviour = "doc"
        SERVER.result = {}
        SERVER.requests.clear()
        try:
            code = example.main.__wrapped__() if hasattr(example.main, "__wrapped__") else                 self._run(example)
        finally:
            SERVER.behaviour = "ok"
        self.assertEqual(code, 0)
        called = {request[1]["method"] for request in SERVER.requests}
        for expected in ("GetServerInfo", "EnumHub", "CreateHub", "CreateUser",
                         "SetAccessList", "EnumSession", "DeleteHub", "Flush"):
            self.assertIn(expected, called)

    @staticmethod
    def _run(example):
        argv = sys.argv
        sys.argv = ["example.py", "--host", "localhost", "--port", str(SERVER.port),
                    "--password", PASSWORD]
        try:
            return example.main()
        finally:
            sys.argv = argv


class TestCli(unittest.TestCase):
    def test_list(self):
        self.assertEqual(se._cli(["--list"]), 0)

    def test_call(self):
        SERVER.behaviour = "ok"
        code = se._cli(["-H", "localhost", "-P", str(SERVER.port), "-p", PASSWORD,
                        "--fingerprint", FINGERPRINT, "GetHub", '{"HubName_str":"VPN"}'])
        self.assertEqual(code, 0)

    def test_error_exit_code(self):
        SERVER.behaviour = "error"
        SERVER.result = {"code": 1, "message": "ERR_HUB_NOT_FOUND"}
        code = se._cli(["-H", "localhost", "-P", str(SERVER.port), "-p", PASSWORD,
                        "GetHub", '{"HubName_str":"VPN"}'])
        SERVER.behaviour = "ok"
        self.assertEqual(code, 1)

    def test_bad_params(self):
        self.assertEqual(se._cli(["GetHub", "{not json"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
