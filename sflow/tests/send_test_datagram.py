#!/usr/bin/env python3
"""Emit a synthetic but structurally valid sFlow v5 flow sample.

Exists so the collector -> Postgres path can be proven without waiting on
switch configuration: it builds a real sFlow v5 datagram (XDR, one flow
sample carrying a raw Ethernet/IPv4/TCP header) and sends it to sfacctd.
If a row appears in sflow_flows afterwards, every hop works.

Usage: send_test_datagram.py <collector-ip> [port] [agent-ip]
"""
import socket
import struct
import sys


def _ipv4_header(src, dst, proto=6, payload_len=20):
    ver_ihl, tos, total_len, ident, flags_frag, ttl = 0x45, 0, 20 + payload_len, 0x1234, 0, 64
    hdr = struct.pack("!BBHHHBBH4s4s", ver_ihl, tos, total_len, ident, flags_frag,
                      ttl, proto, 0, socket.inet_aton(src), socket.inet_aton(dst))
    return hdr


def _tcp_header(sport, dport):
    return struct.pack("!HHIIBBHHH", sport, dport, 1, 0, 5 << 4, 0x18, 8192, 0, 0)


def build_datagram(agent_ip, src_ip, dst_ip, sport, dport, in_if, out_if, sampling_rate=1024):
    eth = b"\x00\x1b\x21\x54\xf7\xf5" + b"\x14\x18\x77\x8d\x04\x90" + struct.pack("!H", 0x0800)
    packet = eth + _ipv4_header(src_ip, dst_ip) + _tcp_header(sport, dport)
    # XDR opaque data is padded to a 4-byte boundary.
    pad = (-len(packet)) % 4
    header_bytes = packet + b"\x00" * pad

    # Flow record: format 1 = raw packet header.
    rec_body = struct.pack("!IIII", 1, len(packet), 0, len(packet)) + header_bytes
    record = struct.pack("!II", 1, len(rec_body)) + rec_body

    sample_body = struct.pack(
        "!IIIIIIII",
        1,               # sample sequence number
        0 << 24 | in_if,  # source_id: type 0 (ifIndex) | index
        sampling_rate,
        1024,            # sample pool
        0,               # drops
        in_if,
        out_if,
        1,               # one flow record follows
    ) + record
    sample = struct.pack("!II", 1, len(sample_body)) + sample_body  # type 1 = flow sample

    return struct.pack("!II4sIIII", 5, 1, socket.inet_aton(agent_ip), 0, 1, 12345, 1) + sample


def main():
    collector = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 6343
    agent = sys.argv[3] if len(sys.argv) > 3 else "192.168.4.106"

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    # A handful of distinct flows so "top talkers" has something to rank.
    for i, (src, dst, sp, dp, iface) in enumerate([
        ("192.168.4.50", "8.8.8.8",       51000, 443, 1),
        ("192.168.4.50", "8.8.8.8",       51001, 443, 1),
        ("192.168.4.77", "192.168.0.146", 44000, 5432, 2),
        ("192.168.4.90", "1.1.1.1",       33000, 53, 3),
    ]):
        s.sendto(build_datagram(agent, src, dst, sp, dp, iface, 48), (collector, port))
        sent += 1
    print(f"sent {sent} sFlow v5 datagrams to {collector}:{port} as agent {agent}")


if __name__ == "__main__":
    main()
