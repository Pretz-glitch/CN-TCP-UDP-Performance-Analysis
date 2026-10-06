#!/bin/bash
# netem.sh — apply impairment on the ROUTER interfaces (never on endpoints).
# Usage: netem.sh <delay_ms_per_direction> <loss_pct> [rate_mbit]
#   delay: applied on BOTH router ifaces, so added RTT = 2 x delay
#          (e.g. 50 => ~100ms RTT). 0 = no delay.
#   loss:  applied ONLY in client->server direction (dev r-s, facing server),
#          so configured loss == data-packet loss; ACKs unimpaired.
#   rate:  bottleneck bandwidth in Mbit/s (default 10).
# Examples:
#   netem.sh 0 0        # baseline: no impairment, still 10mbit bottleneck
#   netem.sh 50 1       # ~100ms RTT, 1% data-direction loss
set -u

if [ $# -lt 2 ]; then echo "Usage: $0 <delay_ms> <loss_pct> [rate_mbit]"; exit 1; fi
DELAY_MS="$1"; LOSS_PCT="$2"; RATE="${3:-10}"

# replace semantics: clear old qdiscs first
sudo ip netns exec router tc qdisc del dev r-s root 2>/dev/null || true
sudo ip netns exec router tc qdisc del dev r-c root 2>/dev/null || true

# client->server direction (router iface facing server): delay + loss + rate
ARGS_S=""
[ "$DELAY_MS" != "0" ] && ARGS_S="$ARGS_S delay ${DELAY_MS}ms"
[ "$LOSS_PCT" != "0" ] && ARGS_S="$ARGS_S loss ${LOSS_PCT}%"
ARGS_S="$ARGS_S rate ${RATE}mbit"
# shellcheck disable=SC2086
sudo ip netns exec router tc qdisc add dev r-s root netem $ARGS_S

# server->client direction (router iface facing client): delay + rate, NO loss
ARGS_C=""
[ "$DELAY_MS" != "0" ] && ARGS_C="$ARGS_C delay ${DELAY_MS}ms"
ARGS_C="$ARGS_C rate ${RATE}mbit"
# shellcheck disable=SC2086
sudo ip netns exec router tc qdisc add dev r-c root netem $ARGS_C

echo "NETEM: delay=${DELAY_MS}ms/dir (RTT~$((2 * DELAY_MS))ms), loss=${LOSS_PCT}% (c->s only), rate=${RATE}mbit"
sudo ip netns exec router tc qdisc show dev r-s | head -1
sudo ip netns exec router tc qdisc show dev r-c | head -1
