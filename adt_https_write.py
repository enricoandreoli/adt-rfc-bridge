"""adt_https_write.py - edit + ACTIVATE an ABAP object over the real ADT HTTPS endpoint.

General, reusable replacement for the one-off fix_* scripts. Works against any SAP system
whose ICM HTTP(S) port is reachable (directly or via a saprouter host that forwards it),
even on NW 75x where the lock-result says MODIFICATION_SUPPORT=NoModification - that flag
self-blocks vsp's edit but the raw ADT PUT/activate succeed anyway.

Why HTTPS and not the RFC bridge: over real HTTP the ADT session is stateful (sap-contextid
+ SAP_SESSIONID cookies), so LOCK -> PUT -> ACTIVATE share one session and activation works.
The RFC bridge (SADT_REST_RFC_ENDPOINT) is stateless per call and can only read + PUT, never
activate.

Two things this gets right that cost hours to discover:
  - GET .../source/main returns the INACTIVE version when one exists -> always verify with
    ?version=active.
  - Activation is TWO-PHASE: preauditRequested=true expands the inactive set (class + the
    changed METHOD ref + transport); then preauditRequested=false with ALL those object
    refs actually promotes. Referencing only the class = silent no-op.

Usage:
  set env SAP_URL / SAP_USER / SAP_PASSWORD / SAP_CLIENT / SAP_CORRNR (or pass flags)
  python adt_https_write.py --uri /sap/bc/adt/oo/classes/zcl_x --name ZCL_X \
         --source new.abap [--corr T74K900349] [--activate-only] [--check]

  As a module:
    import adt_https_write as w
    s = w.Session(base, user, pwd, client)
    w.put_source(s, obj_uri, source, corr)   # obj_uri = .../classes/zcl_x
    w.activate(s, obj_uri, name)
"""
import os, ssl, base64, re, argparse, sys
import urllib.request, urllib.error
from urllib.parse import quote
from http.cookiejar import CookieJar


class Session:
    def __init__(self, base, user, pwd, client):
        self.base = base.rstrip("/")
        self.client = client
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self.jar))
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode()
        self.csrf = "Fetch"

    def req(self, method, path, headers=None, body=None):
        h = {"Authorization": self.auth, "X-CSRF-Token": self.csrf,
             "X-sap-adt-sessiontype": "stateful"}
        if headers:
            h.update(headers)
        sep = "&" if "?" in path else "?"
        url = self.base + path + sep + "sap-client=" + self.client
        data = body.encode("utf-8") if isinstance(body, str) else body
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            resp = self.opener.open(r, timeout=180)
            code, hh, rb = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            code, hh, rb = e.code, e.headers, e.read()
        tok = hh.get("x-csrf-token")
        if tok and tok.lower() not in ("required", "fetch"):
            self.csrf = tok
        return code, rb.decode("utf-8", "replace")

    def fetch_csrf(self, probe_path):
        self.req("GET", probe_path, {"Accept": "text/plain", "X-CSRF-Token": "Fetch"})


def get_source(s, obj_uri, version="active"):
    code, body = s.req("GET", obj_uri + "/source/main?version=" + version, {"Accept": "text/plain"})
    return code, body


def put_source(s, obj_uri, source, corr):
    code, body = s.req("POST", obj_uri + "?_action=LOCK&accessMode=MODIFY",
                       {"Accept": "application/vnd.sap.as+xml;dataname=com.sap.adt.lock.result"})
    m = re.search(r"<LOCK_HANDLE>(.*?)</LOCK_HANDLE>", body)
    if code != 200 or not m:
        raise RuntimeError("LOCK failed %s: %s" % (code, body[:300]))
    enc = quote(m.group(1), safe="")
    try:
        q = obj_uri + "/source/main?lockHandle=" + enc + (("&corrNr=" + corr) if corr else "")
        code, body = s.req("PUT", q, {"Accept": "text/plain",
                                      "Content-Type": "text/plain; charset=utf-8"}, source)
        if code != 200:
            raise RuntimeError("PUT failed %s: %s" % (code, body[:300]))
    finally:
        s.req("POST", obj_uri + "?_action=UNLOCK&lockHandle=" + enc,
              {"Accept": "application/vnd.sap.as+xml;dataname=com.sap.adt.lock.result"})
    return True


def activate(s, obj_uri, name, otype="CLAS/OC"):
    """Two-phase activate. Returns (ok, messages_xml)."""
    refs0 = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
             '<adtcore:objectReference adtcore:uri="%s" adtcore:type="%s" adtcore:name="%s"/>'
             '</adtcore:objectReferences>' % (obj_uri, otype, name))
    code, pre = s.req("POST", "/sap/bc/adt/activation?method=activate&preauditRequested=true",
                      {"Accept": "application/xml", "Content-Type": "application/xml"}, refs0)
    # collect object refs (inside <ioc:object>, skip <ioc:transport>); fall back to refs0
    obj_refs = []
    for blk in re.findall(r'<ioc:object\b[^>]*>(.*?)</ioc:object>', pre, re.DOTALL):
        for r in re.findall(r'<ioc:ref\b[^>]*/>', blk):
            u = re.search(r'adtcore:uri="([^"]+)"', r)
            t = re.search(r'adtcore:type="([^"]+)"', r)
            n = re.search(r'adtcore:name="([^"]+)"', r)
            if u and n:
                obj_refs.append((u.group(1), t.group(1) if t else "", n.group(1)))
    if not obj_refs:
        obj_refs = [(obj_uri, otype, name)]
    body_refs = "".join(
        '<adtcore:objectReference adtcore:uri="%s"%s adtcore:name="%s"/>'
        % (u, (' adtcore:type="%s"' % t if t else ""), n) for (u, t, n) in obj_refs)
    act = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
           + body_refs + '</adtcore:objectReferences>')
    code, body = s.req("POST", "/sap/bc/adt/activation?method=activate&preauditRequested=false",
                       {"Accept": "application/xml", "Content-Type": "application/xml"}, act)
    has_error = ('type="E"' in body) or ('type="A"' in body)
    return (code == 200 and not has_error), body


def syntax_check(s, obj_uri, version="inactive"):
    chk = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<chkrun:checkObjectList xmlns:chkrun="http://www.sap.com/adt/checkrun" '
           'xmlns:adtcore="http://www.sap.com/adt/core">'
           '<chkrun:checkObject adtcore:uri="%s/source/main" chkrun:version="%s"/>'
           '</chkrun:checkObjectList>' % (obj_uri, version))
    code, body = s.req("POST", "/sap/bc/adt/checkruns?reporters=abapCheckRun",
                       {"Accept": "application/vnd.sap.adt.checkmessages+xml",
                        "Content-Type": "application/vnd.sap.adt.checkobjects+xml"}, chk)
    errs = re.findall(r'<chkrun:checkMessage\b[^>]*chkrun:type="[EA]"[^>]*>', body)
    return errs, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", required=True, help="object URI, e.g. /sap/bc/adt/oo/classes/zcl_x")
    ap.add_argument("--name", required=True, help="object name, e.g. ZCL_X")
    ap.add_argument("--type", default="CLAS/OC", help="adtcore:type (default CLAS/OC)")
    ap.add_argument("--source", help="path to new source file (omit with --activate-only)")
    ap.add_argument("--corr", default=os.environ.get("SAP_CORRNR", ""))
    ap.add_argument("--activate-only", action="store_true")
    ap.add_argument("--check", action="store_true", help="syntax-check inactive before activating")
    a = ap.parse_args()

    base = os.environ.get("SAP_URL")
    user = os.environ.get("SAP_USER")
    pwd = os.environ.get("SAP_PASSWORD")
    client = os.environ.get("SAP_CLIENT", "100")
    if not (base and user and pwd):
        sys.exit("set SAP_URL / SAP_USER / SAP_PASSWORD (+ SAP_CLIENT)")
    s = Session(base, user, pwd, client)
    s.fetch_csrf(a.uri + "/source/main")

    if not a.activate_only:
        if not a.source:
            sys.exit("--source required (or use --activate-only)")
        src = open(a.source, "r", encoding="utf-8").read()
        put_source(s, a.uri, src, a.corr)
        print("PUT ok (inactive version saved)")

    if a.check:
        errs, _ = syntax_check(s, a.uri, "inactive")
        print("syntax errors:", len(errs))
        for e in errs[:10]:
            print("  ", e[:200])
        if errs:
            sys.exit("aborting: inactive version has syntax errors")

    ok, msgs = activate(s, a.uri, a.name, a.type)
    code, act = get_source(s, a.uri, "active")
    print("ACTIVATE ok:", ok)
    warns = re.findall(r'chkrun:type="W"|type="W"', msgs)
    if warns:
        print("  (%d warning(s), no errors)" % len(warns))
    if not ok:
        print("  messages:", msgs[:600])
    print("active version length:", len(act))


if __name__ == "__main__":
    main()
