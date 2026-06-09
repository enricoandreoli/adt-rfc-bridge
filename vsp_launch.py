"""
MCP launcher for an RFC-only SAP client (Claude Desktop / any MCP host).

The MCP host starts *this* script as the server command. It:

  1. ensures the ADT-over-RFC bridge is running on BRIDGE_PORT, starting it
     detached if it is not (the bridge connects to SAP lazily, so nothing logs
     on to SAP until the first ADT call);
  2. execs the ADT client (default: vsp), which reads
     SAP_URL=http://127.0.0.1:<BRIDGE_PORT> from the environment and speaks ADT
     to the bridge. stdio is inherited so the MCP stdin/stdout reach the client.

Everything is taken from the environment so one launcher serves any client.
Configure these in the MCP server's env block (see README.md):

  Bridge:  BRIDGE_PORT, RFC_ASHOST, RFC_SYSNR, RFC_CLIENT, RFC_USER,
           RFC_PASSWD, RFC_SAPROUTER
  Client:  SAP_URL (= http://127.0.0.1:<BRIDGE_PORT>), SAP_USER, SAP_PASSWORD,
           SAP_CLIENT, and any other variables your ADT client expects.

  Paths (override if your install differs):
           BRIDGE_PYTHON  python.exe that can load the x64 NW RFC SDK + pyrfc
           BRIDGE_SCRIPT  path to adt_rfc_bridge.py
           ADT_CLIENT     path to the ADT client executable (e.g. vsp.exe)
"""
import os
import sys
import socket
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Python that can load the (x64) NW RFC SDK and pyrfc. Override via BRIDGE_PYTHON.
BRIDGE_PYTHON = os.environ.get("BRIDGE_PYTHON", sys.executable)
BRIDGE_SCRIPT = os.environ.get("BRIDGE_SCRIPT", os.path.join(HERE, "adt_rfc_bridge.py"))
# The HTTP-only ADT client to hand control to once the bridge is up.
ADT_CLIENT = os.environ.get("ADT_CLIENT", "vsp")

# Windows: start the bridge fully detached so it outlives this launcher.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main():
    port = int(os.environ.get("BRIDGE_PORT", "8410"))
    if not listening(port):
        kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if os.name == "nt":
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([BRIDGE_PYTHON, BRIDGE_SCRIPT], **kwargs)
        for _ in range(80):  # wait up to ~8s for the bridge to bind the port
            if listening(port):
                break
            time.sleep(0.1)

    # Hand over to the ADT client; it inherits our stdio (the MCP pipes) and env.
    sys.exit(subprocess.call([ADT_CLIENT]))


if __name__ == "__main__":
    main()
