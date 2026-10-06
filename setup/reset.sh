#!/bin/bash
# reset.sh — tear down the testbed cleanly. Idempotent: safe to run anytime.
# Removes netem qdiscs, deletes namespaces (veth pairs die with them).
set -u

for dev in r-s r-c; do
    sudo ip netns exec router tc qdisc del dev "$dev" root 2>/dev/null || true
done

for n in client router server; do
    sudo ip netns del "$n" 2>/dev/null && echo "netns $n deleted" || echo "netns $n already absent"
done

# any stray root-ns veth leftovers
for link in c-r r-c s-r r-s; do
    sudo ip link del "$link" 2>/dev/null || true
done

echo "RESET DONE"
