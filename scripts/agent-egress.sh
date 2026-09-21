#!/usr/bin/env bash
#
# Egress containment for the agent sandboxes (SPA-94).
#
#     sudo scripts/agent-egress.sh apply|revert|status
#
# Agents run arbitrary model-chosen tool calls with a shell and a network. On a
# shared machine that sits INSIDE a corporate network, the failure that matters
# is not a wasted request — it is an agent sweeping RFC1918 and a security team
# arriving to ask why. These rules make that impossible rather than unlikely.
#
# Why not a domain allowlist. Half the benchmark cases legitimately reach the
# public internet (yahoo-finance, fetch, scholarly, arxiv), and their hosts are
# not derivable: `yfinance` resolves its own endpoints, `scholarly` talks to
# Google Scholar, and `fetch` retrieves whatever URL the model picks. A list
# built from the case files would be both incomplete and brittle — CDN addresses
# rotate under it. So the rules constrain the SHAPE of the traffic, which is what
# distinguishes a scan from work, instead of guessing its destinations:
#
#   1. traffic inside our own bridge is untouched  (agent -> its own PG lane)
#   2. DNS is allowed                              (to the host resolver)
#   3. every RFC1918 / link-local / CGNAT destination is DROPPED — the actual
#      CISO risk, and the one rule that is never relaxed
#   4. outbound TCP is allowed only on 80/443      — a port scan needs ports
#   5. new outbound connections are rate-limited   — a host sweep needs rate;
#      real tool use does not come 60-a-second
#   6. anything else is dropped and LOGGED, so a case blocked by mistake is
#      visible in the counters instead of failing silently
#
# Every rule matches ``-i <our bridge>``, so the other projects on this host are
# untouched even though DOCKER-USER is a shared chain. `revert` removes exactly
# what `apply` added, by comment tag, and nothing else.

set -euo pipefail

NETWORK="${EVALHIVE_NETWORK:-evalhive_evalhive-net}"
TAG="evalhive-egress"
# A scan opens many connections fast; an agent fetching pages does not. 60/s with
# a 120 burst is far above real tool use and far below any useful sweep.
RATE="${EVALHIVE_EGRESS_RATE:-60/second}"
BURST="${EVALHIVE_EGRESS_BURST:-120}"

PRIVATE_NETS=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10)

die() { echo "FATAL: $*" >&2; exit 1; }

command -v iptables >/dev/null || die "iptables not found (run with sudo, or install it)"
command -v docker >/dev/null || die "docker not found"

bridge_name() {
    local id
    id="$(docker network inspect "${NETWORK}" -f '{{.Id}}' 2>/dev/null)" || return 1
    local named
    named="$(docker network inspect "${NETWORK}" -f '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null)"
    if [[ -n "${named}" && "${named}" != "<no value>" ]]; then
        printf '%s' "${named}"
    else
        printf 'br-%s' "${id:0:12}"
    fi
}

# --------------------------------------------------------------------- apply --

do_apply() {
    local br; br="$(bridge_name)" || die "network ${NETWORK} not found — bring the stack up first"
    ip link show "${br}" >/dev/null 2>&1 || die "bridge ${br} does not exist on this host"
    echo "==> bridge: ${br}  (network ${NETWORK})"

    do_revert_quiet

    # Rules are INSERTED, so they land at the top of DOCKER-USER in reverse order
    # of insertion. They are added here last-to-first, which leaves them running
    # in the order the comments above describe.
    local ins=(iptables -I DOCKER-USER 1)

    # 6. Default for this bridge: log a sample, then drop.
    "${ins[@]}" -i "${br}" -j DROP -m comment --comment "${TAG}:default-drop"
    "${ins[@]}" -i "${br}" -m limit --limit 10/minute -j LOG \
        --log-prefix "${TAG}-drop " -m comment --comment "${TAG}:log"

    # 4. Outbound TCP only on the web ports.
    "${ins[@]}" -i "${br}" -p tcp -m multiport --dports 80,443 -j RETURN \
        -m comment --comment "${TAG}:web-ok"

    # 5. Rate-limit new outbound connections (the shape of a sweep). Inserted
    #    AFTER the web rule so it ends up BEFORE it in the chain — `-I 1` pushes
    #    each rule on top, so insertion order is the reverse of evaluation order.
    #    Get this backwards and the limiter sits behind the 80/443 RETURN, where
    #    it can never fire: a scan over https would sail straight past it, which
    #    is precisely the traffic it exists to catch.
    "${ins[@]}" -i "${br}" -p tcp --syn -m hashlimit \
        --hashlimit-above "${RATE}" --hashlimit-burst "${BURST}" \
        --hashlimit-mode srcip --hashlimit-name "${TAG}" \
        -j DROP -m comment --comment "${TAG}:ratelimit"

    # 3. Never let a sandbox reach the internal network. Placed after the
    #    intra-bridge RETURN below, so an agent still reaches its own PG lane.
    local net
    for net in "${PRIVATE_NETS[@]}"; do
        "${ins[@]}" -i "${br}" -d "${net}" -j DROP \
            -m comment --comment "${TAG}:no-internal"
    done

    # 2. DNS out (the host resolver); without it nothing resolves.
    "${ins[@]}" -i "${br}" -p udp --dport 53 -j RETURN -m comment --comment "${TAG}:dns"
    "${ins[@]}" -i "${br}" -p tcp --dport 53 -j RETURN -m comment --comment "${TAG}:dns"

    # 1. Anything staying inside our own bridge is none of this policy's business.
    "${ins[@]}" -i "${br}" -o "${br}" -j RETURN -m comment --comment "${TAG}:intra"

    # --- and the same for the host itself -----------------------------------
    #
    # DOCKER-USER hangs off FORWARD, which only sees traffic being routed
    # THROUGH the host. A packet addressed to the host's own IP goes to INPUT
    # instead and never meets any rule above — so with FORWARD alone a sandbox
    # still reaches every port the neighbours publish on 0.0.0.0 (their
    # Postgres on 5432, their Redis on 6379, their APIs). Verified before
    # adding this: a container on our network got a real answer from the host's
    # :8002. The block is not complete until INPUT is covered too.
    #
    # DNS is allowed through because the container resolver forwards to the
    # host; everything else from our bridge to the host is dropped.
    local iins=(iptables -I INPUT 1)
    "${iins[@]}" -i "${br}" -j DROP -m comment --comment "${TAG}:host-drop"
    "${iins[@]}" -i "${br}" -p tcp --dport 53 -j ACCEPT -m comment --comment "${TAG}:host-dns"
    "${iins[@]}" -i "${br}" -p udp --dport 53 -j ACCEPT -m comment --comment "${TAG}:host-dns"

    echo "==> applied. Established connections are unaffected; this governs new ones."
    do_status
}

# -------------------------------------------------------------------- revert --

do_revert_quiet() {
    # Delete by comment tag, one at a time, until none are left. Never touches a
    # rule this script did not add — which is the whole point on a shared host.
    #
    # Match the tag alone, NOT `--comment <tag>`: iptables -S quotes the comment
    # (`--comment "evalhive-egress:intra"`), so the prefixed pattern silently
    # matched nothing, revert removed nothing, and every apply stacked a second
    # copy of the ruleset on top of the first. A rollback that quietly does
    # nothing is worse than none at all.
    local n guard=0 chain
    for chain in DOCKER-USER INPUT; do
        while :; do
            n="$(iptables -S "${chain}" 2>/dev/null | grep -n -- "${TAG}:" | head -1 | cut -d: -f1)" || true
            [[ -z "${n}" ]] && break
            # `-S` prints the chain declaration first, so the rule index is n-1.
            iptables -D "${chain}" "$(( n - 1 ))"
            (( ++guard > 200 )) && die "revert looped past 200 deletions — aborting rather than flailing at a shared chain"
        done
    done
}

do_revert() { do_revert_quiet; echo "==> removed all ${TAG} rules"; do_status; }

# -------------------------------------------------------------------- status --

do_status() {
    echo
    echo "DOCKER-USER rules tagged ${TAG}:"
    iptables -L DOCKER-USER -n -v --line-numbers 2>/dev/null \
        | grep -E "${TAG}|Chain|pkts" | sed 's/^/  /' || echo "  (none)"
    echo
    echo "INPUT rules tagged ${TAG} (host-local reach):"
    iptables -S INPUT 2>/dev/null | grep -- "${TAG}:" | sed 's/^/  /' || echo "  (none)"
    echo
    echo "Everything else in DOCKER-USER (other projects — must stay untouched):"
    iptables -S DOCKER-USER 2>/dev/null | grep -v -- "${TAG}:" | sed 's/^/  /'
}

case "${1:-}" in
    apply)  do_apply ;;
    revert) do_revert ;;
    status) do_status ;;
    *) sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
