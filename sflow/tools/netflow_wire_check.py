"""Read-only: decode NetFlow v9 flowset composition straight off the wire.

Answers the one question that matters when the collector is running and
the table stays empty: is the exporter actually sending templates, or
only data records that nothing can decode?
"""
import socket, struct, sys, time
src_filter, secs = sys.argv[1], int(sys.argv[2])

# The subset worth naming. Field 1 (IN_BYTES) is the one that matters
# most: NetFlow v9 lets the exporter choose its width, and a 4-byte
# counter cannot represent a flow above 4 GiB - it wraps or clamps, and
# the result looks like a real number rather than an overflow.
FIELD_NAMES = {
    1: "IN_BYTES", 2: "IN_PKTS", 4: "PROTOCOL", 5: "TOS",
    7: "L4_SRC_PORT", 8: "IPV4_SRC_ADDR", 10: "INPUT_SNMP",
    11: "L4_DST_PORT", 12: "IPV4_DST_ADDR", 14: "OUTPUT_SNMP",
    21: "LAST_SWITCHED", 22: "FIRST_SWITCHED", 27: "IPV6_SRC_ADDR",
    28: "IPV6_DST_ADDR", 61: "DIRECTION",
}


def _decode_template(body, out):
    """Field types and widths for each template in a template flowset."""
    i = 0
    while i + 4 <= len(body):
        tid, count = struct.unpack("!HH", body[i:i + 4])
        i += 4
        fields = []
        for _ in range(count):
            if i + 4 > len(body):
                break
            ftype, flen = struct.unpack("!HH", body[i:i + 4])
            i += 4
            fields.append((FIELD_NAMES.get(ftype, f"type {ftype}"), flen))
        if fields:
            out[tid] = fields
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
# A big receive buffer, because the default drops most of a busy
# exporter's traffic: an early run captured 41 of 275 packets (seqno
# 128..403), which is fine for "is anything arriving" and useless for
# "did a template arrive", since templates are rare and missing one
# looks exactly like the exporter never sending them.
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
s.settimeout(1.0)
domains, kinds, templates, end = {}, {}, {}, time.time() + secs
while time.time() < end:
    try:
        raw = s.recvfrom(65535)[0]
    except socket.timeout:
        continue
    if len(raw) < 42:
        continue
    et = struct.unpack("!H", raw[12:14])[0]; off = 14
    if et == 0x8100:
        et = struct.unpack("!H", raw[16:18])[0]; off = 18
    if et != 0x0800:
        continue
    ip = raw[off:]
    if ip[9] != 17 or socket.inet_ntoa(ip[12:16]) != src_filter:
        continue
    ihl = (ip[0] & 0x0F) * 4
    p = ip[ihl + 8:]
    if len(p) < 20 or struct.unpack("!H", p[:2])[0] != 9:
        continue
    count, _, _, seq, dom = struct.unpack("!HIIII", p[2:20])
    domains.setdefault(dom, []).append(seq)
    i = 20
    while i + 4 <= len(p):
        fsid, flen = struct.unpack("!HH", p[i:i + 4])
        if flen < 4:
            break
        kind = ("TEMPLATE" if fsid == 0 else
                "OPTIONS-TEMPLATE" if fsid == 1 else f"data(tmpl {fsid})")
        kinds[(dom, kind)] = kinds.get((dom, kind), 0) + 1
        if fsid == 0:
            _decode_template(p[i + 4:i + flen], templates)
        i += flen
print(f"      {secs}s of NetFlow v9 from {src_filter}")
for dom in sorted(domains):
    seqs = domains[dom]
    print(f"      observation domain {dom}: {len(seqs)} packets, seqno {min(seqs)}..{max(seqs)}")
    for (d, kind), n in sorted(kinds.items()):
        if d == dom:
            print(f"          {kind:20} x{n}")
if templates:
    for tid, fields in sorted(templates.items()):
        print(f"      template {tid}:")
        for name, width in fields:
            flag = ""
            if name == "IN_BYTES" and width <= 4:
                flag = f"   <- {width*8}-bit: cannot exceed {2**(width*8)-1:,} bytes per flow"
            print(f"          {name:16} {width} bytes{flag}")
else:
    print("      no template flowset captured in this window")
if not domains:
    print("      nothing captured")
