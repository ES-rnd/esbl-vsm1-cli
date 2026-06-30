"""Shared command helpers/guards."""

from ...state import ctx, State


def require_connected() -> bool:
    """
        Guard: ensure we're connected before running GATT commands.
    """

    if (ctx.state != State.CONNECTED
            or ctx.client is None or not ctx.client.is_connected):

        print("⚠️  Not connected. Use `connect -mac <ADDRESS>` first.")

        return False

    return True


def format_props(props) -> str:
    """
        Format characteristic properties as a compact tag list.
    """

    flags = {
        "read":                        "R",
        "write":                       "W",
        "write-without-response":      "Wx",
        "notify":                      "N",
        "indicate":                    "I",
        "broadcast":                   "B",
        "authenticated-signed-writes": "Sw",
        "extended-properties":         "Ext",
    }

    return ",".join(flags.get(p, p) for p in props)
