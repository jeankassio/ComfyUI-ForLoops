"""
JkNodes - ForLoop nodes for ComfyUI.

Implements real in-graph loops using ComfyUI's graph-expansion execution model
(the same mechanism used by the official execution-inversion examples).

Each loop is an Open/Close pair:

    For Open  --(index/value)-->  [ your loop body ]  --(value)-->  For Close

The body that you want to repeat must connect into the *Close* node (so it
becomes part of the loop). On every iteration the Close node clones the body
sub-graph and feeds the next index/accumulator back into the Open node.

Nodes:
    For Open / For Close            -> integer counter loop (Python `range`)
    For Each Open / For Each Close  -> iterate over up to 50 connected inputs
                                       of any (matching) type
"""

from comfy_execution.graph_utils import GraphBuilder, is_link

CATEGORY = "JkNodes"
MAX_FOREACH_INPUTS = 50

# Distinct flow-control wire types so a "For" loop can't be wired into a
# "For Each" close (and vice-versa).
FOR_FLOW = "JK_FOR_FLOW"
FOREACH_FLOW = "JK_FOREACH_FLOW"


class AnyType(str):
    """A type that is considered equal to every other type ('*' wildcard)."""

    def __ne__(self, other):
        return False

    def __eq__(self, other):
        return True


ANY = AnyType("*")


# --------------------------------------------------------------------------- #
# Shared loop-close machinery
# --------------------------------------------------------------------------- #
def _collect_loop_body(open_id, close_id, dynprompt):
    """
    Return every node that belongs to the loop body.

    The body is everything *downstream* of the Open node (i.e. every node that
    consumes the loop index / accumulator, directly or transitively), bounded
    by the Close node. This deliberately includes sink/output nodes such as
    Save Image or Preview, so any work that uses the loop value is repeated on
    every iteration -- not only the nodes that feed back into Close.
    """
    # Build a consumer map: for each node, which nodes read one of its outputs.
    consumers = {}
    for node_id in dynprompt.all_node_ids():
        node = dynprompt.get_node(node_id)
        for value in (node.get("inputs", {}) if node else {}).values():
            if is_link(value):
                consumers.setdefault(value[0], []).append(node_id)

    contained = {}
    stack = [open_id]
    while stack:
        node_id = stack.pop()
        if node_id in contained:
            continue
        contained[node_id] = True
        # The Close node is the boundary: never walk past it (nodes that
        # consume Close's output are *after* the loop, not part of it).
        if node_id == close_id:
            continue
        for consumer in consumers.get(node_id, []):
            if consumer not in contained:
                stack.append(consumer)

    contained[open_id] = True
    contained[close_id] = True
    return contained


def _loop_close(flow, dynprompt, unique_id, accumulator):
    """
    Decide whether to run another iteration.

    `flow` is the dict produced by the matching Open node and carries:
        open_id : node id of the Open node (for this iteration)
        i       : current 0-based iteration index
        n       : total number of iterations
    """
    open_id = flow["open_id"]
    i = flow["i"]
    n = flow["n"]

    # Loop finished -> emit the accumulated value.
    if n <= 0 or i + 1 >= n:
        return (accumulator,)

    # Otherwise clone the loop body and run the next iteration.
    contained = _collect_loop_body(open_id, unique_id, dynprompt)

    graph = GraphBuilder()
    for node_id in contained:
        original = dynprompt.get_node(node_id)
        node = graph.node(
            original["class_type"],
            "Recurse" if node_id == unique_id else node_id,
        )
        node.set_override_display_id(node_id)

    for node_id in contained:
        original = dynprompt.get_node(node_id)
        node = graph.lookup_node("Recurse" if node_id == unique_id else node_id)
        for key, value in original["inputs"].items():
            if is_link(value) and value[0] in contained:
                parent = graph.lookup_node(value[0])
                node.set_input(key, parent.out(value[1]))
            else:
                node.set_input(key, value)

    # Advance the loop state on the cloned Open node.
    new_open = graph.lookup_node(open_id)
    new_open.set_input("_i", i + 1)
    new_open.set_input("initial_value", accumulator)

    my_clone = graph.lookup_node("Recurse")
    return {
        "result": (my_clone.out(0),),
        "expand": graph.finalize(),
    }


# --------------------------------------------------------------------------- #
# For  (integer counter loop)
# --------------------------------------------------------------------------- #
_INT_LIMIT = 0x7FFFFFFF


class JkForOpen:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "start": ("INT", {"default": 0, "min": -_INT_LIMIT, "max": _INT_LIMIT,
                                   "tooltip": "First integer emitted by 'index'."}),
                "end": ("INT", {"default": 10, "min": -_INT_LIMIT, "max": _INT_LIMIT,
                                "tooltip": "Stop value (exclusive), like Python range(start, end). "
                                           "start=0, end=5 -> 0,1,2,3,4."}),
            },
            "optional": {
                "initial_value": (ANY, {"tooltip": "Optional starting value threaded through the loop "
                                                   "and returned by For Close."}),
                # Internal loop counter, set automatically on each iteration.
                "_i": ("INT", {"default": 0, "forceInput": True}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = (FOR_FLOW, "INT", ANY)
    RETURN_NAMES = ("flow", "index", "value")
    FUNCTION = "run"
    CATEGORY = CATEGORY

    def run(self, start, end, initial_value=None, _i=0, unique_id=None, **_):
        start = int(start)
        end = int(end)
        i = int(_i)
        n = max(0, end - start)
        index = start + i
        flow = {"open_id": unique_id, "i": i, "n": n}
        return (flow, index, initial_value)


class JkForClose:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "flow": (FOR_FLOW, {}),
            },
            "optional": {
                "value": (ANY, {"tooltip": "Connect your loop body here so it runs every iteration. "
                                           "Its value is threaded back into For Open and returned "
                                           "when the loop ends."}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("value",)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def run(self, flow, value=None, dynprompt=None, unique_id=None):
        return _loop_close(flow, dynprompt, unique_id, value)


# --------------------------------------------------------------------------- #
# For Each  (iterate over connected inputs)
# --------------------------------------------------------------------------- #
class JkForEachOpen:
    @classmethod
    def INPUT_TYPES(cls):
        optional = {
            "initial_value": (ANY, {"tooltip": "Optional accumulator threaded through the loop."}),
            "_i": ("INT", {"default": 0, "forceInput": True}),
        }
        for k in range(MAX_FOREACH_INPUTS):
            optional["value%d" % k] = (ANY, {})
        return {
            "required": {},
            "optional": optional,
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = (FOREACH_FLOW, "INT", ANY, ANY)
    RETURN_NAMES = ("flow", "index", "item", "value")
    FUNCTION = "run"
    CATEGORY = CATEGORY

    def run(self, initial_value=None, _i=0, unique_id=None, **kwargs):
        values = []
        for k in range(MAX_FOREACH_INPUTS):
            key = "value%d" % k
            if key in kwargs and kwargs[key] is not None:
                values.append(kwargs[key])

        n = len(values)
        i = int(_i)
        if i < 0:
            i = 0
        if n > 0 and i >= n:
            i = n - 1

        item = values[i] if n > 0 else None
        flow = {"open_id": unique_id, "i": i, "n": n}
        return (flow, i, item, initial_value)


class JkForEachClose:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "flow": (FOREACH_FLOW, {}),
            },
            "optional": {
                "value": (ANY, {"tooltip": "Connect your loop body here so it runs for every item."}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("value",)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def run(self, flow, value=None, dynprompt=None, unique_id=None):
        return _loop_close(flow, dynprompt, unique_id, value)


NODE_CLASS_MAPPINGS = {
    "JkForOpen": JkForOpen,
    "JkForClose": JkForClose,
    "JkForEachOpen": JkForEachOpen,
    "JkForEachClose": JkForEachClose,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "JkForOpen": "For Open",
    "JkForClose": "For Close",
    "JkForEachOpen": "For Each Open",
    "JkForEachClose": "For Each Close",
}
