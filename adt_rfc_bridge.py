"""
ADT-over-RFC bridge
===================

Gives an HTTP-only ABAP ADT client (for example `vsp` / vibing-steampunk, or any
tool that speaks the ADT REST API over HTTP) full access to a SAP system that is
only reachable over RFC / through a SAProuter -- by tunnelling every ADT REST
request through the standard SAP function module ``SADT_REST_RFC_ENDPOINT`` (the
same dispatcher Eclipse ADT uses when it talks ADT over RFC).

    your ADT client  --HTTP-->  this bridge (localhost)
                     --pyrfc / RFC (+ saprouter)-->  SADT_REST_RFC_ENDPOINT  (SAP)

Why this exists
---------------
HTTP-only ADT clients cannot reach a SAP system whose only ingress is a SAProuter
that permits NI-native routes (DIAG / gateway) but denies raw routing to the ICM
HTTP(S) port. Eclipse works in that situation because it does not use plain HTTP:
it serialises each ADT request and sends it over RFC to the gateway. This bridge
reproduces exactly that, so an HTTP client gets the same reach.

The function module interface (introspect it on your own system to confirm):
  IMPORT  REQUEST  SADT_REST_REQUEST
            REQUEST_LINE  { METHOD, URI, VERSION }
            HEADER_FIELDS [] { NAME, VALUE }
            MESSAGE_BODY  (xstring)
  EXPORT  RESPONSE SADT_REST_RESPONSE
            STATUS_LINE   { VERSION, STATUS_CODE, REASON_PHRASE }
            HEADER_FIELDS [] { NAME, VALUE }
            MESSAGE_BODY  (xstring)

A single persistent, lock-serialised RFC connection is used so that ADT stateful
sessions (object locks, etc.) are preserved across calls.

Configuration (environment variables -- nothing is hard-coded)
--------------------------------------------------------------
  RFC_ASHOST     application server host (as SAP sees it, e.g. an internal IP)
  RFC_SYSNR      system / instance number, e.g. "00"
  RFC_CLIENT     client, e.g. "100"
  RFC_USER       RFC user
  RFC_PASSWD     RFC user password
  RFC_SAPROUTER  saprouter route string, e.g. "/H/router.example.com/S/3299"
                 (omit if the system is reachable directly over the network)
  BRIDGE_PORT    local TCP port the bridge listens on (default 8410)

See `.env.example`. The bridge only connects to SAP on the *first* request
(lazy connect), so an idle bridge performs no SAP logon.

Run
---
  python adt_rfc_bridge.py            # start the bridge
  python adt_rfc_bridge.py selftest   # one-shot ADT discovery call, prints the result

Requires the SAP NW RFC SDK on the library path and the `pyrfc` package built
against it. See README.md.
"""
import os
import sys
import threading
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pyrfc import Connection

LOGFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adt_bridge.log")
_loglock = threading.Lock()


def log(msg):
    """Append a timestamped line to the local log file (best-effort)."""
    line = datetime.datetime.now().strftime("%H:%M:%S.%f ") + msg
    with _loglock:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _required(name):
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(
            "ERROR: required environment variable %s is not set.\n"
            "Copy .env.example, fill it in, and load it before starting the bridge.\n"
            % name
        )
        sys.exit(2)
    return val


def build_params():
    """Assemble pyrfc connection parameters from the environment."""
    params = dict(
        ashost=_required("RFC_ASHOST"),
        sysnr=os.environ.get("RFC_SYSNR", "00"),
        client=_required("RFC_CLIENT"),
        user=_required("RFC_USER"),
        passwd=_required("RFC_PASSWD"),
    )
    # pyrfc understands the saprouter parameter natively and traverses the
    # router exactly like JCo/Eclipse do. Only add it when one is configured.
    router = os.environ.get("RFC_SAPROUTER")
    if router:
        params["saprouter"] = router
    return params


PARAMS = build_params()
PORT = int(os.environ.get("BRIDGE_PORT", "8410"))

_conn = None
_lock = threading.Lock()
# Hop-by-hop / framing headers we must not copy from the FM response back to the
# HTTP client -- the bridge sets its own Content-Length and closes the framing.
_SKIP_RESP_HDR = {"content-length", "transfer-encoding", "connection", "keep-alive"}


def get_conn():
    """Return the live RFC connection, (re)opening it lazily if needed."""
    global _conn
    try:
        if _conn is not None and _conn.alive:
            return _conn
    except Exception:
        pass
    _conn = Connection(**PARAMS)
    return _conn


def adt_call(method, uri, headers, body):
    """Map one ADT HTTP request to SADT_REST_RFC_ENDPOINT and back."""
    # The FM does not support HTTP HEAD (returns 400). Issue a GET instead; the
    # HTTP handler drops the body for HEAD so the client still gets a valid HEAD.
    fm_method = "GET" if method == "HEAD" else method
    req = {
        "REQUEST_LINE": {"METHOD": fm_method, "URI": uri, "VERSION": "HTTP/1.1"},
        "HEADER_FIELDS": [{"NAME": k, "VALUE": v} for (k, v) in headers],
        "MESSAGE_BODY": body or b"",
    }
    log(">> %s %s" % (method, uri))
    with _lock:
        conn = get_conn()
        try:
            res = conn.call("SADT_REST_RFC_ENDPOINT", REQUEST=req)
        except Exception:
            # One reconnect attempt on a dead connection, then give up.
            # Never hammer the logon -- repeated wrong logons lock the SAP user.
            global _conn
            _conn = None
            conn = get_conn()
            res = conn.call("SADT_REST_RFC_ENDPOINT", REQUEST=req)

    r = res["RESPONSE"]
    sl = r["STATUS_LINE"]
    log("<< status %s %s" % (sl.get("STATUS_CODE"), sl.get("REASON_PHRASE")))

    code = int(sl["STATUS_CODE"]) if str(sl["STATUS_CODE"]).strip() else 200
    reason = sl.get("REASON_PHRASE") or ""
    out_headers = [
        (h["NAME"], h["VALUE"])
        for h in r["HEADER_FIELDS"]
        if h["NAME"].lower() not in _SKIP_RESP_HDR
    ]

    # ADT-over-RFC issues no CSRF token because there is no HTTP session. Many
    # ADT clients require an X-CSRF-Token before they will perform writes, so we
    # synthesise one in response to a "fetch" request. The FM is already
    # authenticated by the RFC logon and does not validate CSRF, so the value is
    # only there to satisfy the client.
    wants_csrf = any(
        k.lower() == "x-csrf-token" and v.strip().lower() == "fetch"
        for k, v in headers
    )
    if wants_csrf and not any(k.lower() == "x-csrf-token" for k, _ in out_headers):
        out_headers.append(("X-CSRF-Token", "ADT-RFC-BRIDGE"))

    out_body = r["MESSAGE_BODY"] or b""
    if isinstance(out_body, str):
        out_body = out_body.encode("utf-8")
    return code, reason, out_headers, out_body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence the default stderr access log
        pass

    def _handle(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n) if n else b""
            headers = [
                (k, v)
                for (k, v) in self.headers.items()
                if k.lower() not in ("content-length", "connection", "host")
            ]
            code, reason, out_headers, out_body = adt_call(
                self.command, self.path, headers, body
            )
        except Exception as e:
            msg = ("ADT-RFC bridge error: %s" % e).encode("utf-8", "replace")
            self.send_response(502, "Bad Gateway")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            sys.stderr.write(msg.decode("utf-8", "replace") + "\n")
            return

        self.send_response(code, reason)
        for k, v in out_headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out_body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = _handle


def selftest():
    """One-shot ADT core discovery call -- proves the whole path works."""
    code, reason, hdrs, body = adt_call(
        "GET",
        "/sap/bc/adt/core/discovery",
        [("Accept", "application/atomsvc+xml")],
        b"",
    )
    print("SELFTEST status: %s %s" % (code, reason))
    print("content-type:", dict((k.lower(), v) for k, v in hdrs).get("content-type"))
    print("body bytes:", len(body))
    print(body[:400].decode("utf-8", "replace"))


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    via = PARAMS.get("saprouter", "direct")
    print(
        "ADT-RFC bridge listening on http://127.0.0.1:%d -> %s (sysnr %s, client %s) via %s"
        % (PORT, PARAMS["ashost"], PARAMS["sysnr"], PARAMS["client"], via)
    )
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        main()
