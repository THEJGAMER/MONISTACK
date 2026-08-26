"""Read-only: decode NetFlow v9 flowset composition straight off the wire.

Answers the one question that matters when the collector is running and
the table stays empty: is the exporter actually sending templates, or
only data records that nothing can decode?
"""
import socket, struct, sys, time
src_filter, secs = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
s.settimeout(1.0)
domains, kinds, end = {}, {}, time.time() + secs
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
        i += flen
print(f"      {secs}s of NetFlow v9 from {src_filter}")
for dom in sorted(domains):
    seqs = domains[dom]
    print(f"      observation domain {dom}: {len(seqs)} packets, seqno {min(seqs)}..{max(seqs)}")
    for (d, kind), n in sorted(kinds.items()):
        if d == dom:
            print(f"          {kind:20} x{n}")
if not domains:
    print("      nothing captured")
