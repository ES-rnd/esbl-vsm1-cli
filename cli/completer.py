"""Context-aware command/argument completer."""

from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML

from ..state import ctx
from ..sensors.registry import SENSOR_REGISTRY
from pathlib import Path

COMMANDS = [
    "scan", "stop_scan",
    "connect", "disconnect",
    "list_devices", "list_services", "list_characteristics",
    "subscribe", "unsubscribe",
    "record", "stop_record",
    "configure",
    "calibrate",
    "provision",
    "update",
    "clear", "help", "exit",
]


class EssCompleter(Completer):
    """
        Context-aware completer: commands, flags, MAC addresses, sensor keys.
    """

    def get_completions(self, document, complete_event) -> Any:
        text = document.text_before_cursor
        tokens = text.split()

        # 1) Top-level commands
        if len(tokens) == 0 or (len(tokens) == 1 and not text.endswith(" ")):
            word = tokens[0] if tokens else ""
            for cmd in COMMANDS:
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word))
            return

        cmd = tokens[0]

        # 2) connect → -mac then known MAC addresses
        if cmd == "connect":
            if len(tokens) == 1 or (len(tokens) == 2 and not text.endswith(" ")):   # noqa
                partial = tokens[1] if len(tokens) == 2 else ""
                if "-mac".startswith(partial):
                    yield Completion("-mac", start_position=-len(partial))
                return

            if "-mac" in tokens:
                partial = "" if text.endswith(" ") else tokens[-1]

                for addr, label in ctx.known_addrs():
                    if addr.lower().startswith(partial.lower()):
                        yield Completion(
                            addr,
                            start_position=-len(partial),
                            display=HTML(
                                f"<ansigreen>{addr}</ansigreen>  "
                                f"<ansiyellow>{label}</ansiyellow>"
                            ),
                        )
            return

                # calibrate → -module <imu|fft>, and imu has extra int options (10..100)
        if cmd == "calibrate":
            next_is_partial = not text.endswith(" ")
            partial = tokens[-1] if next_is_partial else ""
            prev = tokens[-2 if next_is_partial else -1] if len(tokens) > 1 else ""   # noqa

            ALLOWED_MODULES = ("imu", "fft")
            IMU_OPTIONS = ("z_offset", "mag_xy", "jitter")

            # Find selected module (-module <X>) if any
            sel = None
            if "-module" in tokens:
                i = tokens.index("-module")
                if i + 1 < len(tokens):
                    sel = tokens[i + 1]

            # right after `-module` → suggest allowed modules only
            if prev == "-module":
                for key in ALLOWED_MODULES:
                    if key.startswith(partial):
                        yield Completion(key, start_position=-len(partial))
                return

            # imu selected
            if sel == "imu":
                # right after a known imu flag → suggest int values 10..100
                if prev.startswith("-") and prev[1:] in IMU_OPTIONS:
                    for n in range(10, 101):
                        s = str(n)
                        if s.startswith(partial):
                            yield Completion(s, start_position=-len(partial))
                    return

                # otherwise suggest remaining unused flag names
                used = {t[1:] for t in tokens if t.startswith("-") and t != "-module"}  # noqa
                for opt in IMU_OPTIONS:
                    if opt in used:
                        continue
                    f = f"-{opt}"
                    if f.startswith(partial):
                        yield Completion(f, start_position=-len(partial))
                return

            # no -module yet → suggest -module
            if "-module" not in tokens and "-module".startswith(partial):
                yield Completion("-module", start_position=-len(partial))

            return

        # 3) configure → -module <name> / -<param> <value>  (schema-driven per sensor)  # noqa      

        if cmd == "configure" or cmd == "update":
            next_is_partial = not text.endswith(" ")
            partial = tokens[-1] if next_is_partial else ""
            prev = tokens[-2 if next_is_partial else -1] if len(tokens) > 1 else ""     # noqa

            # Find selected sensor (-module <X>) if any
            sel = None
            if "-module" in tokens:
                i = tokens.index("-module")
                if i + 1 < len(tokens):
                    sel = tokens[i + 1]

            # update -file <bin>
            if cmd == "update" and prev == "-file":
                parent = Path.cwd()

                for f in sorted(parent.glob("*.bin")):
                    name = f.name

                    if name.startswith(partial):
                        yield Completion(
                            name,
                            start_position=-len(partial),
                        )

                return

            # 3a) right after `-module` → suggest sensor keys
            if prev == "-module" and cmd != "update":
                for key in SENSOR_REGISTRY.keys():
                    if key.startswith(partial) and key != "fota":
                        yield Completion(key, start_position=-len(partial))

                return
            elif prev == "-module" and cmd == "update":
                yield Completion("fota", start_position=-len(partial))

            # 3b) sensor is selected → use its PARAMS schema
            if sel and sel in SENSOR_REGISTRY:
                schema = SENSOR_REGISTRY[sel].PARAMS

                # right after a known param flag → suggest valid values
                if prev.startswith("-") and prev[1:] in schema:
                    spec = schema[prev[1:]]

                    if spec["type"] == "choice":
                        for c in spec["choices"]:
                            if c.startswith(partial):
                                yield Completion(c, start_position=-len(partial))    # noqa

                    elif spec["type"] == "int":
                        for n in range(spec["min"], spec["max"] + 1):
                            s = str(n)
                            if s.startswith(partial):
                                yield Completion(s, start_position=-len(partial))    # noqa

                    return

                # otherwise suggest remaining unused flag names for this sensor
                used = {t[1:] for t in tokens if t.startswith("-") and t != "-module"}   # noqa

                for k in schema.keys():
                    if k in used:
                        continue
                    f = f"-{k}"
                    if f.startswith(partial):
                        yield Completion(f, start_position=-len(partial))

                return

            # 3c) no -module yet → suggest -module
            if "-module" not in tokens and "-module".startswith(partial):
                yield Completion("-module", start_position=-len(partial))

            return

        # 4) subscribe / unsubscribe → -module <key>
        if cmd in ("subscribe", "unsubscribe"):
            if len(tokens) == 1 or (len(tokens) == 2 and not text.endswith(" ")):   # noqa
                partial = tokens[1] if len(tokens) == 2 else ""
                if "-module".startswith(partial):
                    yield Completion("-module", start_position=-len(partial))
                return

            if "-module" in tokens:
                partial = "" if text.endswith(" ") else tokens[-1]

                for key in SENSOR_REGISTRY:
                    if key.startswith(partial) and key != "fota":
                        yield Completion(key, start_position=-len(partial))

                return

                # provision -> -wup <wake_period>
        if cmd == "provision":
            next_is_partial = not text.endswith(" ")
            partial = tokens[-1] if next_is_partial else ""
            prev = tokens[-2 if next_is_partial else -1] if len(tokens) > 1 else ""

            WUP_OPTIONS = (
                "RTC_DISABLED",
                "RTC_30_SECS",
                "RTC_1_MIN",
                "RTC_15_MIN",
                "RTC_30_MIN",
                "RTC_1_HOUR",
                "RTC_4_HOURS",
                "RTC_8_HOURS",
                "RTC_12_HOURS",
                "RTC_1_DAY",
            )

            # right after -wup -> suggest wakeup values
            if prev == "-wup":
                for opt in WUP_OPTIONS:
                    if opt.startswith(partial):
                        yield Completion(
                            opt,
                            start_position=-len(partial),
                        )
                return

            # suggest -wup flag
            if "-wup" not in tokens:
                if "-wup".startswith(partial):
                    yield Completion(
                        "-wup",
                        start_position=-len(partial),
                    )
                return

            return

        # 5) record / stop_record → -module <key> [-out <file>]
        if cmd in ("record", "stop_record"):
            next_is_partial = not text.endswith(" ")
            partial = tokens[-1] if next_is_partial else ""
            prev    = tokens[-2 if next_is_partial else -1] if len(tokens) > 1 else ""   # noqa

            # right after `-module` → suggest sensor keys
            if prev == "-module":
                for key in SENSOR_REGISTRY:
                    if key.startswith(partial):
                        yield Completion(key, start_position=-len(partial))
                return

            # remaining flags
            has_module = "-module" in tokens
            has_out = "-out" in tokens

            flags = []

            if not has_module:
                flags.append("-module")

            if cmd == "record" and not has_out:
                flags.append("-out")

            for f in flags:
                if f.startswith(partial):
                    yield Completion(f, start_position=-len(partial))

            return
