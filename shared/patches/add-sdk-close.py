#!/usr/bin/env python3
"""Add a Close() method to the fforchino vector-go-sdk.

The upstream SDK opens a gRPC connection to the robot in vector.New() but
never exposes a way to close it — *.Vector has no Close(). Every connection
therefore leaks until the robot's small SDK connection budget fills and it
stops responding (the "drops after a question or two" failure).

This patch keeps the underlying *grpc.ClientConn on the Vector struct and
adds a Close() method that releases it. The install scripts pull the pinned
SDK commit into chipper/third_party/vector-go-sdk and add a go.mod `replace`
directive; this script then patches that local copy. fix-connection-leak.py
adds the matching `defer robot.Close()` calls in Wire-Pod's kgsim.go.

Idempotent. Modifies pkg/vector/vector.go of the local SDK copy.
"""
import sys
from pathlib import Path

SENTINEL = "func (v *Vector) Close()"

# 1. Keep the underlying gRPC connection on the struct.
ANCHOR_STRUCT = (
    "// Vector is the struct containing info about Vector\n"
    "type Vector struct {\n"
    "\tConn vectorpb.ExternalInterfaceClient\n"
    "\tCfg  options\n"
    "}\n"
)
REPLACE_STRUCT = (
    "// Vector is the struct containing info about Vector\n"
    "type Vector struct {\n"
    "\tConn vectorpb.ExternalInterfaceClient\n"
    "\tCfg  options\n"
    "\t// grpcConn is the underlying gRPC connection. Kept so callers can\n"
    "\t// release it via Close() — without this every vector.New() leaks a\n"
    "\t// connection to the robot (the robot's SDK has a small connection\n"
    "\t// budget and wedges once it fills up).\n"
    "\tgrpcConn *grpc.ClientConn\n"
    "}\n"
)

# 2. Add the Close() method.
ANCHOR_METHOD = (
    "func (v *Vector) GetIPAddress() string {\n"
    "\ttargetIP := strings.Split(v.Cfg.Target, \":\")[0]\n"
    "\treturn targetIP\n"
    "}\n"
    "\n"
    "// New returns either a vector struct, or an error on failure\n"
)
REPLACE_METHOD = (
    "func (v *Vector) GetIPAddress() string {\n"
    "\ttargetIP := strings.Split(v.Cfg.Target, \":\")[0]\n"
    "\treturn targetIP\n"
    "}\n"
    "\n"
    "// Close releases the underlying gRPC connection to the robot. Safe to call\n"
    "// multiple times and on a nil-conn Vector.\n"
    "func (v *Vector) Close() error {\n"
    "\tif v == nil || v.grpcConn == nil {\n"
    "\t\treturn nil\n"
    "\t}\n"
    "\terr := v.grpcConn.Close()\n"
    "\tv.grpcConn = nil\n"
    "\treturn err\n"
    "}\n"
    "\n"
    "// New returns either a vector struct, or an error on failure\n"
)

# 3. Capture the connection in New().
ANCHOR_NEW = (
    "\tr := Vector{\n"
    "\t\tConn: vectorpb.NewExternalInterfaceClient(c.Conn()),\n"
    "\t\tCfg:  cfg,\n"
    "\t}\n"
)
REPLACE_NEW = (
    "\tr := Vector{\n"
    "\t\tConn:     vectorpb.NewExternalInterfaceClient(c.Conn()),\n"
    "\t\tCfg:      cfg,\n"
    "\t\tgrpcConn: c.Conn(),\n"
    "\t}\n"
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[sdk-close] {path.name} already patched.")
        return False
    for anchor in (ANCHOR_STRUCT, ANCHOR_METHOD, ANCHOR_NEW):
        if anchor not in src:
            print(f"[sdk-close] anchor not found in {path}", file=sys.stderr)
            sys.exit(1)
    src = src.replace(ANCHOR_STRUCT, REPLACE_STRUCT, 1)
    src = src.replace(ANCHOR_METHOD, REPLACE_METHOD, 1)
    src = src.replace(ANCHOR_NEW, REPLACE_NEW, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[sdk-close] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-vector-go-sdk/pkg/vector/vector.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))
