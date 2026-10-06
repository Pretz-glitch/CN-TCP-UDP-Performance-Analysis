#!/bin/bash
# topology.sh — build the client -> router -> server testbed.
# Idempotent: safe to re-run; existing namespaces/links/addresses are reused.
# Layout:
#   [client ns] ---- veth ---- [router ns] ---- veth ---- [server ns]
#   10.0.1.1        10.0.1.2 | 10.0.2.2          10.0.2.1
set -u

C_IP="10.0.1.1/24"; R_C_IP="10.0.1.2/24"
R_S_IP="10.0.2.2/24"; S_IP="10.0.2.1/24"

ns_exists() { sudo ip netns list 2>/dev/null | grep -qw "$1"; }
link_exists() { sudo ip -n "$1" link show "$2" >/dev/null 2>&1; }
addr_exists() { sudo ip -n "$1" addr show dev "$2" 2>/dev/null | grep -qw "$3"; }

for n in client router server; do
    if ns_exists "$n"; then echo "netns $n exists, reusing"; else sudo ip netns add "$n"; echo "netns $n created"; fi
    sudo ip -n "$n" link set lo up
done

# veth pairs (created in root ns, then moved)
if ! link_exists client c-r && ! sudo ip link show c-r >/dev/null 2>&1; then
    sudo ip link add c-r type veth peer name r-c; echo "veth c-r/r-c created"
fi
if ! link_exists server s-r && ! sudo ip link show s-r >/dev/null 2>&1; then
    sudo ip link add s-r type veth peer name r-s; echo "veth s-r/r-s created"
fi

# move ends into namespaces (skip if already there)
link_exists client c-r || sudo ip link set c-r netns client 2>/dev/null || true
link_exists router r-c  || sudo ip link set r-c netns router 2>/dev/null || true
link_exists server s-r  || sudo ip link set s-r netns server 2>/dev/null || true
link_exists router r-s  || sudo ip link set r-s netns router 2>/dev/null || true

# addresses
addr_exists client c-r 10.0.1.1 || sudo ip -n client addr add "$C_IP" dev c-r
addr_exists router r-c 10.0.1.2 || sudo ip -n router addr add "$R_C_IP" dev r-c
addr_exists router r-s 10.0.2.2 || sudo ip -n router addr add "$R_S_IP" dev r-s
addr_exists server s-r 10.0.2.1 || sudo ip -n server addr add "$S_IP" dev s-r

# links up
sudo ip -n client link set c-r up
sudo ip -n router link set r-c up
sudo ip -n router link set r-s up
sudo ip -n server link set s-r up

# routes (default via router)
sudo ip -n client route replace default via 10.0.1.2
sudo ip -n server route replace default via 10.0.2.2

# forwarding on router
sudo ip netns exec router sysctl -qw net.ipv4.ip_forward=1

# offloads OFF on every veth (else captures lie: oversized frames, wrong counts)
for spec in "client c-r" "router r-c" "router r-s" "server s-r"; do
    set -- $spec
    sudo ip netns exec "$1" ethtool -K "$2" tso off gso off gro off 2>/dev/null || true
done

echo "TOPOLOGY READY"
