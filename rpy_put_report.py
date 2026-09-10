"""
rpy_put_report.py - create or replace an ABAP report on an RFC-only system,
already ACTIVE, without going through ADT activation.

Why this exists
---------------
Activation does not work over the ADT-over-RFC bridge (see the README: the
endpoint is stateless per call, so the LOCK's enqueue and the activation call
live in different ADT sessions). For **reports** there is a way around it:
`RPY_PROGRAM_INSERT` is remote-enabled, and with `SAVE_INACTIVE = ' '` it writes
the program straight into its ACTIVE version. No activation needed, no Eclipse.

Measured limits of that path, all handled here:

  * `RPY_PROGRAM_UPDATE` is NOT remote-enabled (CALL_FUNCTION_NOT_REMOTE) and
    `RPY_PROGRAM_DELETE` does not exist as an RFC module, so replacing an
    existing report means: delete it through the bridge (ADT LOCK + DELETE),
    then insert it again over RFC.
  * After that ADT DELETE the **enqueue stays orphaned** (stateless bridge) and
    the next insert fails with EU 510 "already being edited". The lock must be
    released explicitly with `?_action=UNLOCK&lockHandle=...`, which this script
    always does.
  * No text pool function module is exposed over RFC (`RPY_TEXTPOOL_*` does not
    exist at all), but the ADT endpoint
    `/sap/bc/adt/textelements/programs/<prog>/source/{selections,symbols}`
    accepts a plain-text PUT and saves the active version directly. Hence
    `--seltexts` and `--symbols`.
  * Selection texts are capped at **30 characters**. Go over, and the backend
    rejects the whole PUT with a bare HTTP 406 that names no culprit (a wrong
    content type gives 415 instead, so 406 really does mean "too long"). This
    script checks before sending.

Usage
-----
    # RFC credentials from the environment
    set RFC_ASHOST=10.0.0.1  & set RFC_SYSNR=00  & set RFC_CLIENT=100
    set RFC_USER=DEVELOPER   & set RFC_PASSWD=...
    set RFC_SAPROUTER=/H/1.2.3.4/S/3299          & rem optional

    python rpy_put_report.py --prog ZFOO --title "My report" \
        --source ./zfoo.abap [--package $TMP] [--replace] \
        [--seltexts ./selection-texts.txt] [--symbols ./text-symbols.txt]

The text files hold one `NAME=Text` per line; `#` starts a comment.

`--replace` and the text pool options talk to the running bridge
(`$BRIDGE_URL`, else `http://127.0.0.1:$BRIDGE_PORT`, default port 8410); the
program insert itself only needs PyRFC.

Optionally, `--mcp <entry>` reads the RFC parameters from an MCP server entry in
Claude Desktop's `claude_desktop_config.json` instead of the environment.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

BRIDGE_URL = os.environ.get(
    "BRIDGE_URL", "http://127.0.0.1:%s" % os.environ.get("BRIDGE_PORT", "8410"))
COMMON = {"X-sap-adt-sessiontype": "stateful", "X-CSRF-Token": "ADT-RFC-BRIDGE"}
MCP_CONFIG = os.path.join(os.environ.get("APPDATA", ""), "Claude",
                          "claude_desktop_config.json")


def rfc_params(mcp_entry=None):
    if mcp_entry:
        with open(MCP_CONFIG, encoding="utf-8") as fh:
            env = json.load(fh)["mcpServers"][mcp_entry]["env"]
    else:
        env = os.environ
    missing = [k for k in ("RFC_ASHOST", "RFC_SYSNR", "RFC_CLIENT", "RFC_USER",
                           "RFC_PASSWD") if not env.get(k)]
    if missing:
        sys.exit("missing RFC parameters: %s" % ", ".join(missing))
    params = dict(user=env["RFC_USER"], passwd=env["RFC_PASSWD"],
                  ashost=env["RFC_ASHOST"], sysnr=env["RFC_SYSNR"],
                  client=env["RFC_CLIENT"])
    if env.get("RFC_SAPROUTER"):
        params["saprouter"] = env["RFC_SAPROUTER"]
    return params


def _call(method, path, extra=None, body=b""):
    headers = dict(COMMON)
    headers.update(extra or {})
    req = urllib.request.Request(BRIDGE_URL + path, data=body, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _lock(url):
    status, body = _call("POST", url + "?_action=LOCK&accessMode=MODIFY",
                         {"Accept": "application/vnd.sap.as+xml;"
                                    "dataname=com.sap.adt.lock.result"})
    handle = re.search(r"<LOCK_HANDLE>(.*?)</LOCK_HANDLE>", body)
    if not handle:
        raise RuntimeError("LOCK failed (HTTP %s). Is the bridge running on %s? %s"
                           % (status, BRIDGE_URL, body[:200]))
    return handle.group(1)


def adt_delete(prog):
    """Delete the program through the bridge; RPY_PROGRAM_DELETE has no RFC."""
    url = "/sap/bc/adt/programs/programs/%s" % prog.lower()
    handle = _lock(url)
    status, body = _call("DELETE", url + "?lockHandle=" + handle,
                         {"Accept": "application/*"})
    _call("POST", url + "?_action=UNLOCK&lockHandle=" + handle)
    if status not in (200, 204):
        raise RuntimeError("DELETE failed HTTP %s: %s" % (status, body[:200]))


def put_textpool(prog, pairs, kind):
    """kind is 'selections' (selection texts) or 'symbols' (text symbols)."""
    if kind == "selections":
        too_long = ["%s (%d)" % (n, len(v)) for n, v in pairs if len(v) > 30]
        if too_long:
            raise RuntimeError("selection texts over 30 characters: %s"
                               % ", ".join(too_long))
        text = "".join("%-8s=%s\n" % (n.upper(), v) for n, v in pairs)
    else:
        text = "".join("@MaxLength:%d\n%s=%s\n\n" % (len(v), n.upper(), v)
                       for n, v in pairs)

    url = "/sap/bc/adt/textelements/programs/%s" % prog.lower()
    handle = _lock(url)
    try:
        status, body = _call(
            "PUT", "%s/source/%s?lockHandle=%s" % (url, kind, handle),
            {"Content-Type": "application/vnd.sap.adt.textelements.%s.v1; "
                             "charset=utf-8" % kind,
             "Accept": "application/*"},
            text.encode("utf-8"))
        if status not in (200, 201, 204):
            raise RuntimeError("PUT %s failed HTTP %s: %s" % (kind, status, body[:200]))
    finally:
        _call("POST", url + "?_action=UNLOCK&lockHandle=" + handle)
    return len(pairs)


def read_pairs(path):
    pairs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            pairs.append((name.strip(), value.strip()))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prog", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--source", required=True, help="file holding the ABAP source")
    ap.add_argument("--package", default="$TMP")
    ap.add_argument("--replace", action="store_true", help="delete and recreate if it exists")
    ap.add_argument("--seltexts", help="file with selection texts, 'NAME=Text' per line")
    ap.add_argument("--symbols", help="file with text symbols, 'KEY=Text' per line")
    ap.add_argument("--mcp", help="read RFC parameters from this MCP entry instead of the environment")
    args = ap.parse_args()

    with open(args.source, encoding="utf-8") as fh:
        src = fh.read()
    too_long = [i + 1 for i, ln in enumerate(src.splitlines()) if len(ln) > 255]
    if too_long:
        sys.exit("source lines longer than 255 characters: %s" % too_long)
    lines = [{"LINE": ln} for ln in src.splitlines()]

    from pyrfc import Connection
    conn = Connection(**rfc_params(args.mcp))

    def insert():
        conn.call("RPY_PROGRAM_INSERT",
                  PROGRAM_NAME=args.prog,
                  PROGRAM_TYPE="1",          # 1 = executable program
                  TITLE_STRING=args.title,
                  DEVELOPMENT_CLASS=args.package,
                  SAVE_INACTIVE=" ",         # ' ' = write the ACTIVE version
                  SUPPRESS_DIALOG="X",
                  SOURCE_EXTENDED=lines)

    try:
        insert()
        print("created %s (%d lines) in %s" % (args.prog, len(lines), args.package))
    except Exception as exc:
        text = "%s %s" % (getattr(exc, "key", ""), exc)
        if "ALREADY_EXISTS" not in text and "CANCELLED" not in text:
            print("INSERT failed:", type(exc).__name__, text[:400])
            raise
        if not args.replace:
            sys.exit("%s already exists. Repeat with --replace." % args.prog)
        adt_delete(args.prog)
        insert()
        print("replaced %s (%d lines) in %s" % (args.prog, len(lines), args.package))

    if args.seltexts:
        print("selection texts written: %d"
              % put_textpool(args.prog, read_pairs(args.seltexts), "selections"))
    if args.symbols:
        print("text symbols written: %d"
              % put_textpool(args.prog, read_pairs(args.symbols), "symbols"))

    res = conn.call("RPY_PROGRAM_READ", PROGRAM_NAME=args.prog,
                    WITH_INCLUDELIST=" ", ONLY_SOURCE="X")
    read_back = res.get("SOURCE_EXTENDED") or res.get("SOURCE") or []
    print("read back from the system: %d lines" % len(read_back))


if __name__ == "__main__":
    main()
