"""list_services / list_characteristics commands."""

from typing import Any

from ...config import ESS_SERVICE_PREFIX
from ...state import ctx
from .base import require_connected, format_props


async def cmd_list_services() -> Any:
    """
        List all GATT services exposed by the connected device.
    """

    if not require_connected():
        return

    services = ctx.client.services

    if services is None or not list(services):
        services = await ctx.client.get_services()

    print(f"\n📡 Services on {ctx.connected_addr}:")
    print(f"{'#':>3}  {'HANDLE':>6}  {'UUID':<40}  DESCRIPTION")
    print("-" * 90)

    for i, svc in enumerate(services):
        print(f"{i:>3}  {svc.handle:>6}  {str(svc.uuid):<40}  {svc.description or ''}") # noqa

    print()


async def cmd_list_characteristics(svc_uuid: str | None = None):
    """
        List ESS (0000fe*) characteristics; optional --service <UUID> filter.
    """

    if not require_connected():
        return

    services = ctx.client.services

    if services is None or not list(services):
        services = await ctx.client.get_services()

    target_services = [
        s for s in services
        if (svc_uuid and str(s.uuid).lower() == svc_uuid.lower())
        or (svc_uuid is None and str(s.uuid).lower().startswith(
            ESS_SERVICE_PREFIX))
    ]

    if not target_services:
        print("⚠️  No ESS (0000fe*) services found.")

        return

    print(f"\n🔑 ESS Characteristics on {ctx.connected_addr}:")

    for svc in target_services:
        print(f"\n  ── Service {svc.uuid}")
        print(f"     {'HANDLE':>6}  {'UUID':<40}  {'PROPS':<14}")
        print("     " + "-" * 70)

        for ch in svc.characteristics:
            props = format_props(ch.properties)
            print(f"     {ch.handle:>6}  {str(ch.uuid):<40}  {props:<14}")

    print()
