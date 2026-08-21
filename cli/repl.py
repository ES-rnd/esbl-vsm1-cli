"""Interactive REPL — parses input and dispatches to command handlers."""

import asyncio
import shlex
import traceback
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from .completer import EssCompleter
from .prompt import make_prompt

from .commands.scan import cmd_scan, cmd_stop, cmd_list_devices, cmd_clear
from .commands.connection import cmd_connect, cmd_disconnect
from .commands.gatt import cmd_list_services, cmd_list_characteristics
from .commands.sensors import cmd_subscribe, cmd_unsubscribe, cmd_provision, cmd_calibrate, cmd_configure, cmd_update, cmd_record, cmd_stop_record    # noqa
from .commands.help import cmd_help


def _extract_flag(args: list[str], flag: str) -> str | None:
    """
        Return the value following `flag` in args, or None.
    """

    if flag in args:
        i = args.index(flag)

        if i + 1 < len(args):
            return args[i + 1]

    return None


async def repl(stop: asyncio.Event):
    """
        Run the interactive REPL until `stop` is set or user exits.
    """

    session = PromptSession(
        completer=EssCompleter(), complete_while_typing=True)

    print("ESS BLE CLI — type `help` for commands. TAB to autocomplete.")

    cmd_help()

    while not stop.is_set():
        try:
            with patch_stdout():
                line = await session.prompt_async(make_prompt)

        except (EOFError, KeyboardInterrupt):
            break

        line = line.strip()

        if not line:
            continue

        try:
            tokens = shlex.split(line)

        except ValueError as e:
            print(f"Parse error: {e}")
            continue

        cmd, *args = tokens

        try:
            if cmd == "scan":
                await cmd_scan()
            elif cmd == "stop_scan":
                await cmd_stop()
            elif cmd == "list_devices":
                await cmd_list_devices()
            elif cmd == "disconnect":
                await cmd_disconnect()
            elif cmd == "clear":
                await cmd_clear()
            elif cmd == "help":
                cmd_help()
            elif cmd == "exit":
                break

            elif cmd == "connect":
                mac = _extract_flag(args, "-mac")
                if mac:
                    await cmd_connect(mac)
                else:
                    print("Usage: connect -mac <ADDRESS>")

            elif cmd == "subscribe":
                val = _extract_flag(args, "-module")

                if val:
                    await cmd_subscribe(val)
                else:
                    print("Usage: subscribe -module <temp>")

            elif cmd == "unsubscribe":
                val = _extract_flag(args, "-module")

                if val:
                    await cmd_unsubscribe(val)
                else:
                    print("Usage: unsubscribe -module <temp>")

            elif cmd == "record":
                val = _extract_flag(args, "-module")
                out = _extract_flag(args, "-out")
                if val:
                    await cmd_record(val, out)
                else:
                    print("Usage: record -module <name> [-out <file.csv>]")

            elif cmd == "stop_record":
                val = _extract_flag(args, "-module")
                if val:
                    await cmd_stop_record(val)
                else:
                    print("Usage: stop_record -module <name>")

            elif cmd == "list_services":
                await cmd_list_services()

            elif cmd == "list_characteristics":
                svc = _extract_flag(args, "--service")
                await cmd_list_characteristics(svc)

            elif cmd == "provision":
                wup = _extract_flag(args, "-wup")

                await cmd_provision(wup)

            # configure -module <sensor> [-<param> <value> ...]   schema-driven
            elif cmd == "configure" or cmd == "update" or cmd == "calibrate":
                module = _extract_flag(args, "-module")
                if module is None:
                    print("Usage: configure -module <sensor> [-<param> <value> ...]")   # noqa
                    continue

                # Collect ALL -flag value pairs (except -module)
                kv = {}
                ok = True
                i = 0

                while i < len(args):
                    a = args[i]

                    if a == "-module":
                        i += 2
                        continue

                    if a.startswith("-"):
                        if i + 1 >= len(args):
                            print(f"❌ Missing value for {a}")
                            ok = False
                            break
                        kv[a[1:]] = args[i + 1]
                        i += 2

                    else:
                        i += 1

                if ok:
                    if cmd == "configure":
                        await cmd_configure(module, kv)
                    elif cmd == "update":
                        await cmd_update(module, kv)
                    elif cmd == "calibrate":
                        await cmd_calibrate(module, kv)

            else:
                print(f"Unknown command: {cmd}  (try `help`)")

        # pylint: disable=broad-exception-caught
        except Exception as e:
            print(f"⚠️  Command '{cmd}' failed: {type(e).__name__}: {e}")
            traceback.print_exc()

        finally:
            print()

    stop.set()
