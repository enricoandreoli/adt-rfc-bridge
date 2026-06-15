# Using Claude for SAP ABAP development on RFC-only / SAProuter systems

*Claude can write, read and refactor ABAP directly in your SAP system through an
MCP server (`vsp`) that speaks the ADT REST API. But some SAP systems are only
reachable over RFC through a SAProuter, where that HTTP API can't get through.
Here's how I closed that gap with a small local ADT-over-RFC bridge, so you get
the full Claude + SAP ABAP experience even on locked-down systems, with no
changes on the SAP side.*

---

## TL;DR

- **You can use Claude as an AI pair programmer for SAP ABAP.** Tools like
  **[`vsp` (vibing-steampunk)][vsp]** expose SAP's **ABAP Development Tools (ADT)**
  to Claude through the **Model Context Protocol (MCP)**, so Claude can read,
  search, edit and check ABAP source straight in your system.
- That integration talks to SAP using the **ADT REST API over HTTP**.
- Many real-world SAP systems are reachable **only through a SAProuter**, and many
  of those routers permit SAP's native **NI** routes (DIAG, gateway) while
  **denying raw HTTP routing to the ICM**. The HTTP-based Claude/SAP link then
  **cannot connect**, even though **Eclipse ADT can**.
- Eclipse can because, in that situation, it does **not** use plain HTTP: it
  **tunnels ADT over RFC** to the gateway, through the standard function module
  **`SADT_REST_RFC_ENDPOINT`**.
- The fix is a small **local HTTP-to-RFC bridge**: it accepts ADT HTTP requests on
  `localhost` and forwards each one over RFC via **[PyRFC][pyrfc]** (whose
  `saprouter` parameter traverses the router natively). Point `vsp` (and therefore
  Claude) at the bridge and you get the full ADT experience. **Nothing changes on
  the customer side.**
- Code + install guide: **[adt-rfc-bridge on GitHub][repo]**.

---

## Using Claude with SAP ABAP: a quick primer

If you haven't tried it yet: **Claude can act as an AI developer inside your SAP
system.** The bridge between the two is an **MCP server**. Model Context Protocol
is the open standard that lets an AI assistant call external tools. For SAP ABAP,
the MCP server I use is **[`vsp` (vibing-steampunk)][vsp]**: it connects to SAP
via the **ABAP Development Tools (ADT)** REST API and gives Claude operations like
*read source*, *search objects*, *edit source*, *run ABAP checks*, and *debug*.

So a typical setup is:

```
  Claude (Claude Desktop / Claude Code)  --MCP-->  vsp  --ADT REST over HTTP-->  SAP
```

With that in place you can ask Claude things like *"find all reports in package
Z* that call this function module and add a guard clause"*, and it works against
the live ABAP repository, the same objects you'd open in Eclipse or SE80.

There's just **one catch**, and it's what this post is about.

## The problem: the Claude/SAP HTTP link can't get through some SAProuters

`vsp`, and therefore Claude, talks to SAP **only over HTTP** (ADT REST). For
most systems that's fine: you give it an `https://host:44300`-style URL and you're
done. But I work across several SAP customers, and one category of system refused
to connect. Several of them are reachable **only through a SAProuter**. You
connect with a route string like `/H/router/S/3299/H/appserver/...`.

Here's the catch. A SAProuter route string is **not HTTP**. It is SAP's own
**NI (Network Interface)** protocol. You cannot put a `/H/.../S/...` string into
an `SAP_URL` or an `HTTPS_PROXY`, because an HTTP client has no idea what to do
with it.

"Fine," I thought, "the router will just forward me to the ICM's HTTP port."
Often it won't. A SAProuter only forwards what its **`saprouttab`** allows, and
on these systems the `saprouttab`:

- **permits** NI-native routes to the dispatcher and **gateway** ports (DIAG,
  RFC), and
- **denies** *raw* routing to the ICM HTTP/HTTPS port.

So there is **no HTTP path at all** from my PC to those systems through the
router, which means no Claude + SAP ABAP either. And, an important constraint of
consulting, **I cannot ask the customer to change anything**: no new Web
Dispatcher, no `saprouttab` edits.

Yet **Eclipse ADT connects to these exact systems and works perfectly.** That was
the clue: if Eclipse can do ADT against a system where raw HTTP is blocked, then
Eclipse is **not** doing ADT over plain HTTP.

## The investigation: what is Eclipse actually doing?

I wanted evidence, not guesses. (Everything below was done against connectivity I
already had, without probing customer systems blindly, and never by hammering
logons, which would lock the user.)

**Step 1, confirm the HTTP door is shut.** I wrote a tiny SAProuter client to
test, at the NI level only, which routes the router would accept:

- A route to the **gateway / dispatcher** ports → **permitted** (`NI_PONG`).
- A route to the **ICM HTTP(S)** port in **raw** mode → **`route permission
  denied`**.

So: NI-native yes, raw HTTP no. That matches the symptom exactly.

**Step 2, watch Eclipse.** I put a small **logging TCP proxy** in front of the
router on `127.0.0.1`, pointed Eclipse's SAProuter string at the proxy (rewriting
the first hop), and connected once. The capture was decisive:

- SAP GUI's route went to the **DIAG** port, as expected.
- **Eclipse ADT's route went to the gateway / RFC port**, and the payload was
  **RFC serialisation**, not HTTP. Inside it, in length-prefixed fields, were
  unmistakable ADT artefacts: `HTTP/1.1`, a `HEADER_FIELDS` table, ADT paths like
  `/sap/bc/adt/core/discovery`.

In other words: **Eclipse takes each ADT HTTP request, serialises it, and sends
it over RFC to the gateway.** On the SAP side a function module receives that
request, runs it against the ADT framework internally, and returns the HTTP
response, again over RFC.

**Step 3, name the function module.** A bit of research plus the captured field
names pointed straight at it:

> **`SADT_REST_RFC_ENDPOINT`**, "Endpoint for ADT REST Framework."
> - IMPORT `REQUEST` (`SADT_REST_REQUEST`): request line (method, URI, version),
>   a `HEADER_FIELDS` table, and an `xstring` body.
> - EXPORT `RESPONSE` (`SADT_REST_RESPONSE`): status line, header fields, body.

That is precisely an HTTP request/response carried inside an RFC call. This is
how ADT works over RFC, and the authorisations needed (`S_RFC` for the FM plus
the ADT resource authorisations) are the same ones your user already has if
Eclipse works.

## The solution: a local HTTP-to-RFC bridge for Claude + SAP

If Eclipse can map ADT HTTP onto `SADT_REST_RFC_ENDPOINT`, so can a small script.
And there's a perfect building block on the Python side: **[PyRFC][pyrfc]**, the
official Python binding to the SAP NW RFC SDK. Crucially, **PyRFC accepts a
`saprouter` connection parameter** and traverses the router **natively**, exactly
like JCo/Eclipse, with no NI reimplementation needed.

So the bridge sits between Claude's MCP server and SAP:

```
  Claude --MCP--> vsp (HTTP) --> bridge on 127.0.0.1:<port>
         --> PyRFC (RFC + saprouter) --> SADT_REST_RFC_ENDPOINT (SAP)
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

### Two adaptations to keep the ADT client happy

Replaying captured Eclipse traffic exposed exactly two mismatches between "HTTP as
an ADT client expects it" and "HTTP as the function module delivers it":

1. **`HEAD` → `GET`.** `SADT_REST_RFC_ENDPOINT` rejects the HTTP `HEAD` method
   with a 400. The bridge silently issues a `GET` and drops the response body, so
   the client still receives a valid `HEAD` response.
2. **Synthesise `X-CSRF-Token`.** Over HTTP, ADT hands out a CSRF token that the
   client must echo back on writes. Over RFC there is **no HTTP session**, so no
   token is issued. ADT clients refuse to write without one. Since the function
   module is already authenticated by the **RFC logon** and does not validate
   CSRF, the bridge returns a placeholder token on `fetch` requests, just enough
   to satisfy the client.

With those two fixes in place, `vsp` behaves exactly as if it were talking to a
normal ICM, and Claude is none the wiser.

## Try it yourself

Full code, configuration template and a step-by-step guide are in the repo:
**[adt-rfc-bridge][repo]**. The short version:

**Prerequisites**

- **SAP NW RFC SDK** on your library path. On Windows it usually comes **with SAP
  GUI** (`sapnwrfc.dll`); otherwise download "SAP NWRFC SDK 7.50" from the SAP
  Support Portal.
- **An x64 Python.** The NW RFC SDK is x86-64 only, so even on ARM Windows you
  need an x64 Python (an arm64 Python cannot load the x64 SDK).
- **PyRFC** matching that Python/SDK: `pip install pyrfc` (or a prebuilt wheel
  from the PyRFC releases).
- **`vsp`** (or any HTTP-based ADT client) and a Claude client that speaks MCP
  (Claude Desktop or Claude Code).
- A user with the ADT/RFC authorisations. **If Eclipse ADT works, you have
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

**Verify** with the built-in self-test, one ADT discovery call all the way to
SAP and back:

```bash
python adt_rfc_bridge.py selftest
# SELFTEST status: 200 OK
# content-type: application/atomsvc+xml
# body bytes: 4187
```

A `200` with an `atomsvc+xml` body means the whole chain works.

**Run** the bridge and point `vsp` at it:

```bash
python adt_rfc_bridge.py            # listens on http://127.0.0.1:8410
# then, for vsp:  SAP_URL=http://127.0.0.1:8410  (+ SAP_USER / SAP_PASSWORD / SAP_CLIENT)
```

**Wire it into Claude.** The repo includes a small **MCP launcher**
(`vsp_launch.py`) that a Claude client (Claude Desktop) can start directly: it
auto-starts the bridge if needed, then hands over to `vsp`. You add **one MCP
server entry per SAP client**, each with its own `BRIDGE_PORT` and RFC settings.
Because the bridge connects lazily, idle clients never log on to SAP. After that,
Claude can develop ABAP on that system like any other.

## Result

After this, **Claude drives ABAP development on a system whose only ingress is an
RFC-permitting SAProuter.** System info, object search, reading and editing
source, running checks: the **full ADT experience**, on a system where plain HTTP
never gets through, **with zero changes on the customer side.** The same trick
works for any HTTP-only ADT client, not just `vsp`.

And it is **backend-agnostic**: confirmed working on **S/4HANA and classic ECC /
R/3 alike**. The only requirement is that the system has the ADT backend, i.e. the
`SADT_REST_RFC_ENDPOINT` function module exists (ABAP Development Tools ship on SAP
NetWeaver 7.31+ with the relevant support packages). Rule of thumb: if Eclipse ADT
can open the system, this bridge can drive it, so you can bring Claude to
long-running ECC landscapes, not only the latest stack.

## Limitations & safety

- You need the NW RFC SDK and an RFC user with the ADT authorisations, i.e. a
  setup where **Eclipse ADT already works**. If Eclipse can't connect either,
  this won't magically open a door.
- The bridge reuses a single serialised RFC connection: simple and lock-safe, but
  not designed for many parallel clients pounding one bridge.
- It bridges the ADT REST surface exposed by `SADT_REST_RFC_ENDPOINT`, which is
  what Eclipse uses, so everyday development works; very exotic ICM-only
  endpoints are out of scope.
- **Account-lockout safety:** each ADT call is one RFC logon attempt. If a call
  fails with an *authentication* error, stop and fix the credentials, never
  loop/retry, or SAP will lock the user. The bridge itself never auto-retries a
  failed logon.
- **Supportability:** this is a community solution. It calls a standard SAP
  function module the same way Eclipse ADT does, but this is not an officially
  documented integration point. Test it in a non-production system first, and
  make sure it fits your organisation's security and connectivity policies.

## Credits & links

- **Code & guide:** [adt-rfc-bridge][repo]
- **`vsp` / vibing-steampunk**, the ABAP ADT MCP server that lets Claude develop
  in SAP, and the HTTP-only client this bridge was built to serve:
  [oisee/vibing-steampunk][vsp]
- **PyRFC**, the Python ↔ NW RFC SDK binding that traverses the SAProuter:
  [SAP-archive/PyRFC][pyrfc]
- **`SADT_REST_RFC_ENDPOINT`**, the standard SAP function module that dispatches
  ADT REST over RFC (the same one Eclipse ADT uses)
- **Model Context Protocol (MCP)**, the open standard that connects Claude to
  tools like `vsp`.

*If this helped you get Claude working against your SAP ABAP systems, say hi in
the comments, and tell me which other "HTTP-only tool vs. RFC-only system"
situations you'd like to see bridged.*

[vsp]: https://github.com/oisee/vibing-steampunk
[pyrfc]: https://github.com/SAP-archive/PyRFC
[repo]: https://github.com/enricoandreoli/adt-rfc-bridge
