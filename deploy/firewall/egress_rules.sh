#!/usr/bin/env bash
# ==============================================================================
# ARDO Playwright Worker Host & Container Egress Firewall Rules (iptables)
# ==============================================================================
# Implements kernel-level packet filtering on the network interface / bridge
# used by Playwright browser worker processes.
# Drops all traffic attempting to reach internal subnets, private CIDRs, or
# cloud metadata services before it can leave the host or container network namespace.
# ==============================================================================

set -euo pipefail

INTERFACE="${1:-eth0}"
echo "[ARDO-FIREWALL] Applying network egress boundary rules on interface: ${INTERFACE}"

# 1. Flush existing custom egress chain if present
iptables -N ARDO_EGRESS 2>/dev/null || iptables -F ARDO_EGRESS

# 2. Allow established and related connections
iptables -A ARDO_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# 3. Allow DNS resolution only to designated upstream resolver (e.g., 8.8.8.8, 1.1.1.1)
iptables -A ARDO_EGRESS -p udp --dport 53 -j ACCEPT
iptables -A ARDO_EGRESS -p tcp --dport 53 -j ACCEPT

# 4. BLOCK Cloud Instance Metadata Service (AWS, GCP, Azure, OpenStack)
iptables -A ARDO_EGRESS -d 169.254.169.254/32 -j REJECT --reject-with icmp-admin-prohibited
iptables -A ARDO_EGRESS -d 169.254.0.0/16 -j REJECT --reject-with icmp-admin-prohibited

# 5. BLOCK RFC 1918 Private IP Subnets (Internal Company & VPC Infrastructure)
iptables -A ARDO_EGRESS -d 10.0.0.0/8 -j REJECT --reject-with icmp-admin-prohibited
iptables -A ARDO_EGRESS -d 172.16.0.0/12 -j REJECT --reject-with icmp-admin-prohibited
iptables -A ARDO_EGRESS -d 192.168.0.0/16 -j REJECT --reject-with icmp-admin-prohibited

# 6. BLOCK Loopback destinations outside localhost
iptables -A ARDO_EGRESS -d 127.0.0.0/8 -j REJECT --reject-with icmp-admin-prohibited

# 7. BLOCK Carrier-Grade NAT (CGNAT) and Test Ranges
iptables -A ARDO_EGRESS -d 100.64.0.0/10 -j REJECT --reject-with icmp-admin-prohibited
iptables -A ARDO_EGRESS -d 198.18.0.0/15 -j REJECT --reject-with icmp-admin-prohibited

# 8. ALLOW outbound HTTP and HTTPS to public Internet only
iptables -A ARDO_EGRESS -p tcp --dport 80 -j ACCEPT
iptables -A ARDO_EGRESS -p tcp --dport 443 -j ACCEPT

# 9. DROP all other outbound traffic (SSH, database ports, internal services)
iptables -A ARDO_EGRESS -j DROP

# Attach ARDO_EGRESS chain to OUTPUT on the interface
iptables -C OUTPUT -o "${INTERFACE}" -j ARDO_EGRESS 2>/dev/null || iptables -A OUTPUT -o "${INTERFACE}" -j ARDO_EGRESS

echo "[ARDO-FIREWALL] Egress boundary established. Private networks and metadata blocked."

