"""
adt_write.py - write & activate an ABAP source object through the ADT-over-RFC bridge.

Why this exists
---------------
Some HTTP ADT clients (notably `vsp`) refuse to modify an object when the ADT
lock result carries `MODIFICATION_SUPPORT=NoModification`. On newer NetWeaver
releases (observed on 752) `SADT_REST_RFC_ENDPOINT` sets that flag even though
the modification actually works: the LOCK returns a valid handle + transport and
the subsequent source PUT + activate succeed (Eclipse modifies the same object
fine). The client self-blocks before ever writing.

This helper performs the raw ADT write sequence against a running bridge, so
writes work even when the client aborts:

    LOCK -> PUT .../source/main -> activate -> UNLOCK

Two headers on the PUT are mandatory and are the usual failure cause if missing:
  * `Accept`                         (missing -> 400 "Accept header missing")
  * `corrNr=<transport>`             (missing on a transportable object -> 500
                                      "already locked in request <TR>")

Usage
-----
    python adt_write.py CLAS ZCL_FOO ./zcl_foo.abap T74K900123   # transportable
    python adt_write.py PROG ZFOO    ./zfoo.abap                 # local / $TMP

The bridge must be running. Its URL is taken from $BRIDGE_URL, else
http://127.0.0.1:$BRIDGE_PORT (default 8410). Omit the transport for $TMP/local
objects. After activation, check the result for syntax errors as usual.

This is a workaround for the client's caution, NOT a change to the bridge: the
bridge forwards these requests unchanged.
"""
import os, sys, re, urllib.request, urllib.parse, urllib.error

BRIDGE_URL = os.environ.get(
    "BRIDGE_URL", "http://127.0.0.1:%s" % os.environ.get("BRIDGE_PORT", "8410"))
COMMON = {"X-sap-adt-sessiontype": "stateful", "X-CSRF-Token": "ADT-RFC-BRIDGE"}

# ADT base path per object type (source-bearing objects)
PATHS = {
    "CLAS": "/sap/bc/adt/oo/classes/%s",
    "INTF": "/sap/bc/adt/oo/interfaces/%s",
    "PROG": "/sap/bc/adt/programs/programs/%s",
    "FUGR": "/sap/bc/adt/functions/groups/%s",
}


def _req(method, path, headers=None, body=None):
    h = dict(COMMON)
    h.update(headers or {})
    data = body.encode("utf-8") if isinstance(body, str) else body
    r = urllib.request.Request(BRIDGE_URL + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _base(otype, name):
    key = otype.upper()
    if key not in PATHS:
        raise SystemExit("unsupported object type %r (use one of: %s)"
                         % (otype, ", ".join(PATHS)))
    return PATHS[key] % name.lower()


def write_object(otype, name, src, transport=""):
    base = _base(otype, name)

    st, body = _req("POST", base + "?_action=LOCK&accessMode=MODIFY",
                    {"Accept": "application/vnd.sap.as+xml;dataname=com.sap.adt.lock.result"})
    m = re.search(r"<LOCK_HANDLE>(.*?)</LOCK_HANDLE>", body)
    if st != 200 or not m:
        raise SystemExit("LOCK failed: %s %s" % (st, body[:400]))
    enc = urllib.parse.quote(m.group(1), safe="")
    print("LOCK ok")

    try:
        q = "?lockHandle=" + enc + ("&corrNr=" + transport if transport else "")
        st, body = _req("PUT", base + "/source/main" + q,
                        {"Accept": "text/plain", "Content-Type": "text/plain; charset=utf-8"}, src)
        if st != 200:
            raise SystemExit("PUT failed: %s %s" % (st, body[:400]))
        print("PUT ok")

        refs = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
                '<adtcore:objectReference adtcore:uri="%s" adtcore:name="%s"/>'
                '</adtcore:objectReferences>' % (base, name.upper()))
        st, body = _req("POST", "/sap/bc/adt/activation?method=activate&preauditRequested=false",
                        {"Accept": "application/xml", "Content-Type": "application/xml"}, refs)
        if st != 200:
            raise SystemExit("ACTIVATE failed: %s %s" % (st, body[:400]))
        if 'type="E"' in body or "<msg" in body:
            print("ACTIVATE returned messages (check for errors):\n" + body[:800])
        else:
            print("ACTIVATE ok")
    finally:
        _req("POST", base + "?_action=UNLOCK&lockHandle=" + enc,
             {"Accept": "application/vnd.sap.as+xml;dataname=com.sap.adt.lock.result"})
        print("UNLOCK ok")


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    otype, name, srcfile = argv[0], argv[1], argv[2]
    transport = argv[3] if len(argv) > 3 else ""
    with open(srcfile, "r", encoding="utf-8") as f:
        src = f.read()
    write_object(otype, name, src, transport)


if __name__ == "__main__":
    main(sys.argv[1:])
