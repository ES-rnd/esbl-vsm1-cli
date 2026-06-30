"""State-aware dynamic prompt."""
from typing import Any
from prompt_toolkit.formatted_text import HTML

from ..state import ctx, State


def make_prompt() -> Any:
    """
        Return a formatted prompt based on the current state.
    """

    if ctx.state == State.SCANNING:
        return HTML("<ansicyan>(scanning)</ansicyan> ess&gt; ")

    if ctx.state == State.CONNECTING:
        return HTML("<ansiyellow>(connecting)</ansiyellow> ess&gt; ")

    if ctx.state == State.CONNECTED:
        return HTML(
            f"<ansigreen>(connected {ctx.connected_addr})</ansigreen> ess&gt; "
        )

    return HTML("<ansiwhite>(idle)</ansiwhite> ess&gt; ")
