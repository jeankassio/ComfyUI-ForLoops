import { app } from "../../scripts/app.js";

// Node identifiers (must match NODE_CLASS_MAPPINGS in nodes.py)
const FOR_OPEN = "JkForOpen";
const FOREACH_OPEN = "JkForEachOpen";
const VALUE_PREFIX = "value";
const MAX_INPUTS = 50;

function removeInternalCounter(node) {
    // The "_i" input is set programmatically during loop expansion; hide it.
    for (let i = node.inputs ? node.inputs.length - 1 : -1; i >= 0; i--) {
        if (node.inputs[i].name === "_i") {
            node.removeInput(i);
        }
    }
}

function getConnectedType(node) {
    // Returns the type of the first connected dynamic value input, or null.
    if (!node.inputs) return null;
    for (const input of node.inputs) {
        if (input.name.startsWith(VALUE_PREFIX) && input.link != null) {
            const link = app.graph.links[input.link];
            if (!link) continue;
            const origin = app.graph.getNodeById(link.origin_id);
            if (origin && origin.outputs && origin.outputs[link.origin_slot]) {
                const t = origin.outputs[link.origin_slot].type;
                if (t && t !== "*") return t;
            }
        }
    }
    return null;
}

function syncForEachInputs(node) {
    // Remove every empty (unconnected) value input.
    for (let i = node.inputs.length - 1; i >= 0; i--) {
        const input = node.inputs[i];
        if (input.name.startsWith(VALUE_PREFIX) && input.link == null) {
            node.removeInput(i);
        }
    }

    // Re-number the remaining (connected) value inputs contiguously.
    let counter = 0;
    for (const input of node.inputs) {
        if (input.name.startsWith(VALUE_PREFIX)) {
            input.name = VALUE_PREFIX + counter;
            counter++;
        }
    }

    // Always keep exactly one trailing empty slot (up to the maximum).
    if (counter < MAX_INPUTS) {
        node.addInput(VALUE_PREFIX + counter, "*");
    }

    // Propagate the connected type so every slot (and the item/value outputs)
    // share it; revert to wildcard when nothing is connected.
    const applied = getConnectedType(node) || "*";
    for (const input of node.inputs) {
        if (input.name.startsWith(VALUE_PREFIX)) {
            input.type = applied;
        }
    }
    if (node.outputs) {
        for (const out of node.outputs) {
            if (out.name === "item" || out.name === "value") {
                out.type = applied;
            }
        }
    }
}

// Reconcile after the current call stack settles. This is important during
// workflow loading: LiteGraph restores inputs/links across several calls, so
// we wait one frame and then fix things up exactly once (avoids removing
// sockets that are about to receive a restored link).
function scheduleReconcile(node, isForEach) {
    if (node.__jkPending) return;
    node.__jkPending = true;
    requestAnimationFrame(() => {
        node.__jkPending = false;
        try {
            removeInternalCounter(node);
            if (isForEach) syncForEachInputs(node);
            node.setDirtyCanvas(true, true);
        } catch (e) {
            console.error("[JkNodes] reconcile failed", e);
        }
    });
}

app.registerExtension({
    name: "JkNodes.ForLoops",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const isOpen = nodeData.name === FOR_OPEN || nodeData.name === FOREACH_OPEN;
        if (!isOpen) return;
        const isForEach = nodeData.name === FOREACH_OPEN;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated ? onCreated.apply(this, arguments) : undefined;
            scheduleReconcile(this, isForEach);
            return r;
        };

        if (isForEach) {
            const onConnectionsChange = nodeType.prototype.onConnectionsChange;
            nodeType.prototype.onConnectionsChange = function (type) {
                const r = onConnectionsChange
                    ? onConnectionsChange.apply(this, arguments)
                    : undefined;
                if (type === 1 /* LiteGraph.INPUT */) {
                    scheduleReconcile(this, true);
                }
                return r;
            };
        }
    },
});
