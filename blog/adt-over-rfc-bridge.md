# Bringing an HTTP-only ABAP ADT client to RFC-only / SAProuter SAP systems

*How I gave an HTTP-only ADT tool (`vsp`) full access to a SAP system that is
only reachable over RFC through a SAProuter — by tunnelling ADT over RFC the same
way Eclipse does, with a small local bridge. No changes on the SAP side.*

---

## TL;DR

- Modern AI/MCP-based ABAP tooling such as **[`vsp` (vibing-steampunk)][vsp]**
  talks to SAP using the **ADT REST API over HTTP**.
- Many real-world systems are reachable **only through a SAProuter**, and many of
  those routers permit SAP's native **NI** routes (DIAG, gateway) while
  **denying raw HTTP routing to the ICM**. An HTTP-only client then simply
  **cannot connect** — even though **Eclipse ADT can**.
- Eclipse can because, in that situation, it does **not** use plain HTTP: it
  **tunnels ADT over RFC** to the gateway, through the standard function module
  **`SADT_REST_RFC_ENDPOINT`**.
- The fix is a small **local HTTP→RFC bridge**: it accepts ADT HTTP requests on
  `localhost` and forwards each one over RFC via **[PyRFC][pyrfc]** (whose
  `saprouter` parameter traverses the router natively). Point your HTTP client at
  the bridge and you get the full ADT experience — **nothing changes on the
  customer side.**
- Code + install guide: **[adt-rfc-bridge on GitHub][repo]**.

---

## The goal

I work across several SAP customers and I wanted the same modern, AI-assisted
ABAP development experience on **all** of them. The tool I use, `vsp`
(vibing-steampunk), is an **ABAP Development Tools (ADT) MCP server and CLI**: it
exposes SAP development operations — read/search/edit source, run checks, debug —
to an AI assistant through the ADT REST API.

`vsp` speaks **only HTTP** (ADT REST). For most systems that's fine: you give it
an `https://host:44300`-style URL and you're done. But one category of system
refused to work, and chasing down *why* turned into a small investigation worth
sharing — because the fix is reusable by anyone in the same situation.

## The problem: HTTP can't get through some SAProuters

Several of my target systems are reachable **only through a SAProuter** — you
connect with a route string like `/H/router/S/3299/H/appserver/...`.

Here's the catch. A SAProuter route string is **not HTTP**. It is SAP's own
**NI (Network Interface)** protocol. You cannot put a `/H/.../S/...` string into
an `SAP_URL` or an `HTTPS_PROXY` — an HTTP client has no idea what to do with it.

"Fine," I thought, "the router will just forward me to the ICM's HTTP port."
Often it won't. A SAProuter only forwards what its **`saprouttab`** allows, and
on these systems the `saprouttab`:

- **permits** NI-native routes to the dispatcher and **gateway** ports (DIAG,
  RFC), and
- **denies** *raw* routing to the ICM HTTP/HTTPS port.

So there is **no HTTP path at all** from my PC to those systems through the
router. And — an important constraint of consulting — **I cannot ask the customer
to change anything**: no new Web Dispatcher, no `saprouttab` edits.

Yet **Eclipse ADT connects to these exact systems and works perfectly.** That was
the clue: if Eclipse can do ADT against a system where raw HTTP is blocked, then
Eclipse is **not** doing ADT over plain HTTP.

## The investigation: what is Eclipse actually doing?

I wanted evidence, not guesses. (Everything below was done against connectivity I
already had, without probing customer systems blindly — and never by hammering
logons, which would lock the user.)

**Step 1 — Confirm the HTTP door is shut.** I wrote a tiny SAProuter client to
test, at the NI level only, which routes the router would accept:

- A route to the **gateway / dispatcher** ports → **permitted** (`NI_PONG`).
- A route to the **ICM HTTP(S)** port in **raw** mode → **`route permission
  denied`**.

So: NI-native yes, raw HTTP no. That matches the symptom exactly.

**Step 2 — Watch Eclipse.** I put a small **logging TCP proxy** in front of the
router on `127.0.0.1`, pointed Eclipse's SAProuter string at the proxy (rewriting
the first hop), and connected once. The capture was decisive:

- SAP GUI's route went to the **DIAG** port — as expected.
- **Eclipse ADT's route went to the gateway / RFC port**, and the payload was
  **RFC serialisation**, not HTTP. Inside it, in length-prefixed fields, were
  unmistakable ADT artefacts: `HTTP/1.1`, a `HEADER_FIELDS` table, ADT paths like
  `/sap/bc/adt/core/discovery`.

In other words: **Eclipse takes each ADT HTTP request, serialises it, and sends
it over RFC to the gateway.** On the SAP side a function module receives that
request, runs it against the ADT framework internally, and returns the HTTP
response — again over RFC.

**Step 3 — Name the function module.** A bit of research plus the captured field
names pointed straight at it:

> **`SADT_REST_RFC_ENDPOINT`** — "Endpoint for ADT REST Framework."
> - IMPORT `REQUEST` (`SADT_REST_REQUEST`): request line (method, URI, version),
>   a `HEADER_FIELDS` table, and an `xstring` body.
> - EXPORT `RESPONSE` (`SADT_REST_RESPONSE`): status line, header fields, body.

That is precisely an HTTP request/response carried inside an RFC call. This is
how ADT works over RFC, and the authorisations needed (`S_RFC` for the FM plus
the ADT resource authorisations) are the same ones your user already has if
Eclipse works.

## The solution: a local HTTP→RFC bridge

If Eclipse can map ADT HTTP onto `SADT_REST_RFC_ENDPOINT`, so can a small script.
And there's a perfect building block on the Python side: **[PyRFC][pyrfc]**, the
official Python binding to the SAP NW RFC SDK. Crucially, **PyRFC accepts a
`saprouter` connection parameter** and traverses the router **natively**, exactly
like JCo/Eclipse — no NI reimplementation needed.

So the bridge is just:

```
  vsp (HTTP)  -->  bridge on 127.0.0.1:<port>  -->  PyRFC (RFC + saprouter)  -->  SADT_REST_RFC_ENDPOINT
```

For each incoming ADT HTTP request the bridge:

1. fills `SADT_REST_REQUEST` with the method, URI, headers and body;
2. calls `SADT_REST_RFC_ENDPOINT` over a single, persistent, lock-serialised RFC
   connection (so ADT object locks survive across calls);
3. maps `SADT_REST_RESPONSE` back to a normal HTTP response.

The heart of it is small enough to show:

```python
def adt_call(method, uri, headers, body):
    # The FM rejects HTTP HEAD -> issue GET, the HTTP layer drops the body.
    fm_method = "GET" if method == "HEAD" else method
    req = {
        "REQUEST_LINE": {"METHOD": fm_method, "URI": uri, "VERSION": "HTTP/1.1"},
        "HEADER_FIELDS": [{"NAME": k, "VALUE": v} for (k, v) in headers],
        "MESSAGE_BODY": body or b"",
    }
    with _lock:
        conn = get_conn()                                   # lazy, reused RFC conn
        res = conn.call("SADT_REST_RFC_ENDPOINT", REQUEST=req)
    r = res["RESPONSE"]
    code = int(r["STATUS_LINE"]["STATUS_CODE"])
    out_headers = [(h["NAME"], h["VALUE"]) for h in r["HEADER_FIELDS"]]
    return code, r["STATUS_LINE"]["REASON_PHRASE"], out_headers, r["MESSAGE_BODY"]
```

PyRFC connects with, in essence:

```python
Connection(
    ashost="<appserver>", sysnr="00", client="100",
    user="<user>", passwd="<password>",
    saprouter="/H/<router>/S/3299",   # <-- traverses the SAProuter natively
)
```

### Two adaptations to keep HTTP clients happy

Replaying captured Eclipse traffic exposed exactly two mismatches between "HTTP as
an ADT client expects it" and "HTTP as the function module delivers it":

1. **`HEAD` → `GET`.** `SADT_REST_RFC_ENDPOINT` rejects the HTTP `HEAD` method
   with a 400. The bridge silently issues a `GET` and drops the response body, so
   the client still receives a valid `HEAD` response.
2. **Synthesise `X-CSRF-Token`.** Over HTTP, ADT hands out a CSRF token that the
   client must echo back on writes. Over RFC there is **no HTTP session**, so no
   token is issued. ADT clients refuse to write without one. Since the function
   module is already authenticated by the **RFC logon** and does not validate
   CSRF, the bridge returns a placeholder token on `fetch` requests — just enough
   to satisfy the client.

With those two fixes in place, the HTTP-only client behaves exactly as if it were
talking to a normal ICM.

## Try it yourself

Full code, configuration template and a step-by-step guide are in the repo:
**[adt-rfc-bridge][repo]**. The short version:

**Prerequisites**

- **SAP NW RFC SDK** on your library path. On Windows it usually comes **with SAP
  GUI** (`sapnwrfc.dll`); otherwise download "SAP NWRFC SDK 7.50" from the SAP
  Support Portal.
- **An x64 Python** — the NW RFC SDK is x86-64 only, so even on ARM Windows you
  need an x64 Python (an arm64 Python cannot load the x64 SDK).
- **PyRFC** matching that Python/SDK: `pip install pyrfc` (or a prebuilt wheel
  from the PyRFC releases).
- A user with the ADT/RFC authorisations — **if Eclipse ADT works, you have
  them.**

> ⚠️ Architecture must match end to end: **x64 SDK ↔ x64 Python ↔ x64 PyRFC**.
> That mismatch is the #1 setup pitfall.

**Configure** (copy `.env.example` to `.env` and fill in):

```
RFC_ASHOST=<appserver host as SAP sees it>
RFC_SYSNR=00
RFC_CLIENT=100
RFC_USER=<user>
RFC_PASSWD=<password>
RFC_SAPROUTER=/H/<router>/S/3299   # omit for direct (no-router) access
BRIDGE_PORT=8410
```

**Verify** with the built-in self-test — one ADT discovery call all the way to
SAP and back:

```bash
python adt_rfc_bridge.py selftest
# SELFTEST status: 200 OK
# content-type: application/atomsvc+xml
# body bytes: 4187
```

A `200` with an `atomsvc+xml` body means the whole chain works.

**Run** the bridge and point your HTTP-only client at it:

```bash
python adt_rfc_bridge.py            # listens on http://127.0.0.1:8410
# then, for vsp:  SAP_URL=http://127.0.0.1:8410  (+ SAP_USER / SAP_PASSWORD / SAP_CLIENT)
```

The repo also includes a small **MCP launcher** (`vsp_launch.py`) that an MCP
host like Claude Desktop can start directly: it auto-starts the bridge if needed,
then hands over to the ADT client. You add **one MCP server entry per SAP
client**, each with its own `BRIDGE_PORT` and RFC settings. Because the bridge
connects lazily, idle clients never log on to SAP.

## Result

After this, `vsp` — an HTTP-only tool — drives ADT against a system whose **only**
ingress is an RFC-permitting SAProuter. System info, object search, reading and
editing source, the lot: the **full ADT experience**, on a system where plain
HTTP never gets through, **with zero changes on the customer side.**

## Limitations & safety

- You need the NW RFC SDK and an RFC user with the ADT authorisations — i.e. a
  setup where **Eclipse ADT already works**. If Eclipse can't connect either,
  this won't magically open a door.
- The bridge reuses a single serialised RFC connection: simple and lock-safe, but
  not designed for many parallel clients pounding one bridge.
- It bridges the ADT REST surface exposed by `SADT_REST_RFC_ENDPOINT` — which is
  what Eclipse uses, so everyday development works; very exotic ICM-only
  endpoints are out of scope.
- **Account-lockout safety:** each ADT call is one RFC logon attempt. If a call
  fails with an *authentication* error, stop and fix the credentials — never
  loop/retry, or SAP will lock the user. The bridge itself never auto-retries a
  failed logon.
- **Supportability:** this is a community solution. It calls a standard SAP
  function module the same way Eclipse ADT does, but this is not an officially
  documented integration point — test it in a non-production system first, and
  make sure it fits your organisation's security and connectivity policies.

## Credits & links

- **Code & guide:** [adt-rfc-bridge][repo]
- **`vsp` / vibing-steampunk** — the HTTP-only ABAP ADT MCP client this was built
  to serve: [oisee/vibing-steampunk][vsp]
- **PyRFC** — Python ↔ NW RFC SDK binding that traverses the SAProuter:
  [SAP-archive/PyRFC][pyrfc]
- **`SADT_REST_RFC_ENDPOINT`** — the standard SAP function module that dispatches
  ADT REST over RFC (the same one Eclipse ADT uses)

*If this saved you a day of packet-staring, say hi in the comments — and tell me
which other "HTTP-only tool vs. RFC-only system" situations you'd like to see
bridged.*

[vsp]: https://github.com/oisee/vibing-steampunk
[pyrfc]: https://github.com/SAP-archive/PyRFC
[repo]: https://github.com/enricoandreoli/adt-rfc-bridge
