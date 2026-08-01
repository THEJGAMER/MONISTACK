"""The command allowlist. This is the only thing that decides what CLI text
ever reaches a device - the frontend only ever sends (category_id,
command_id[, params]) and this module maps that to a literal command
string. There is no path from client input to arbitrary CLI text.

Deliberately read-only (`show ...`) and deliberately excludes
`show running-config` - it can contain secrets (SNMP communities, local
user hashes) that don't belong surfaced in a browser without more thought
than a v1 tool warrants.
"""

COMMAND_TREE = [
    {
        "id": "system",
        "label": "System",
        "items": [
            {"id": "version", "label": "Version", "cmd": "show version"},
            {"id": "system_info", "label": "System Info", "cmd": "show system"},
            {"id": "inventory", "label": "Inventory", "cmd": "show inventory"},
            {"id": "cpu", "label": "CPU Utilization", "cmd": "show processes cpu"},
            {"id": "memory", "label": "Memory", "cmd": "show memory"},
            {"id": "proc_memory", "label": "Memory by Process", "cmd": "show processes memory"},
            {"id": "environment", "label": "Environment (fans / PSU / temp)", "cmd": "show environment"},
            {"id": "clock", "label": "Clock", "cmd": "show clock detail"},
        ],
    },
    {
        "id": "interfaces",
        "label": "Interfaces",
        "items": [
            {"id": "if_status", "label": "Status (all ports)", "cmd": "show interfaces status"},
            {"id": "if_desc", "label": "Descriptions", "cmd": "show interfaces description"},
            {"id": "if_brief", "label": "IP Interface Brief", "cmd": "show ip interface brief"},
            {"id": "switchport", "label": "Switchport VLAN Membership", "cmd": "show interfaces switchport"},
            {
                "id": "if_detail",
                "label": "Interface Detail (counters)",
                "cmd": "show interfaces {port}",
                "param": "port",
            },
            {
                "id": "if_transceiver",
                "label": "Transceiver Diagnostics",
                "cmd": "show interfaces {port} transceiver",
                "param": "port",
            },
        ],
    },
    {
        "id": "port_channels",
        "label": "Port Channels",
        "items": [
            {"id": "pc_brief", "label": "Port-Channel Brief (all)", "cmd": "show interfaces port-channel brief"},
            {
                "id": "pc_detail",
                "label": "Port-Channel Detail (counters)",
                "cmd": "show interfaces port-channel {port_channel}",
                "param": "port_channel",
            },
            {
                "id": "lacp_detail",
                "label": "LACP Detail",
                "cmd": "show lacp {port_channel}",
                "param": "port_channel",
            },
        ],
    },
    {
        "id": "l2",
        "label": "Layer 2",
        "items": [
            {"id": "vlan", "label": "VLANs", "cmd": "show vlan"},
            {"id": "mac", "label": "MAC Address Table", "cmd": "show mac-address-table"},
            {"id": "stp", "label": "Spanning Tree (brief)", "cmd": "show spanning-tree 0 brief"},
        ],
    },
    {
        "id": "l3",
        "label": "Layer 3",
        "items": [
            {"id": "route", "label": "IP Route Table", "cmd": "show ip route"},
            {"id": "route_static", "label": "Static Routes", "cmd": "show ip route static"},
            {"id": "route_summary", "label": "Route Summary", "cmd": "show ip route summary"},
            {"id": "protocols", "label": "Routing Protocols Summary", "cmd": "show ip protocols"},
            {"id": "arp", "label": "ARP Table", "cmd": "show arp"},
        ],
    },
    {
        "id": "ospf",
        "label": "OSPF",
        "items": [
            {"id": "ospf_process", "label": "Process Summary", "cmd": "show ip ospf"},
            {"id": "ospf_neighbor", "label": "Neighbors", "cmd": "show ip ospf neighbor"},
            {"id": "ospf_interface", "label": "Interfaces", "cmd": "show ip ospf interface"},
            {"id": "ospf_database", "label": "Link-State Database", "cmd": "show ip ospf database"},
        ],
    },
    {
        "id": "neighbors",
        "label": "Neighbors",
        "items": [
            {"id": "lldp", "label": "LLDP Neighbors", "cmd": "show lldp neighbors"},
        ],
    },
    {
        "id": "logs",
        "label": "Logging",
        "items": [
            {"id": "logbuf", "label": "Recent Log Buffer", "cmd": "show logging"},
        ],
    },
    {
        "id": "diagnostics",
        "label": "Diagnostics",
        "items": [
            {"id": "alarms", "label": "Alarms (Minor/Major)", "cmd": "show alarms"},
            {"id": "ntp_status", "label": "NTP Status", "cmd": "show ntp status"},
            {"id": "ntp_assoc", "label": "NTP Associations", "cmd": "show ntp associations"},
            {"id": "cam_usage", "label": "CAM/TCAM Usage", "cmd": "show cam-usage"},
            {"id": "dhcp_snooping", "label": "DHCP Snooping Bindings", "cmd": "show ip dhcp snooping binding"},
            {"id": "vrrp", "label": "VRRP Groups", "cmd": "show vrrp"},
            {"id": "redundancy", "label": "Stack/Redundancy Status", "cmd": "show redundancy"},
            {"id": "users", "label": "Active Sessions/Users", "cmd": "show users"},
            {"id": "privilege", "label": "Current Privilege Level", "cmd": "show privilege"},
        ],
    },
]


JUNOS_COMMAND_TREE = [
    {
        "id": "system",
        "label": "System",
        "items": [
            {"id": "version", "label": "Version", "cmd": "show version"},
            {"id": "hardware", "label": "Hardware Inventory", "cmd": "show chassis hardware"},
            {"id": "routing_engine", "label": "Routing Engine (CPU/memory/temp)", "cmd": "show chassis routing-engine"},
            {"id": "uptime", "label": "Uptime", "cmd": "show system uptime"},
            {"id": "environment", "label": "Environment (fans / PSU / temp)", "cmd": "show chassis environment"},
            {"id": "chassis_alarms", "label": "Chassis Alarms", "cmd": "show chassis alarms"},
            {"id": "system_alarms", "label": "System Alarms", "cmd": "show system alarms"},
        ],
    },
    {
        "id": "interfaces",
        "label": "Interfaces",
        "items": [
            {"id": "if_terse", "label": "Status (all interfaces)", "cmd": "show interfaces terse"},
            {"id": "if_desc", "label": "Descriptions", "cmd": "show interfaces descriptions"},
            {
                "id": "if_optics",
                "label": "Transceiver Diagnostics",
                "cmd": "show interfaces diagnostics optics {port}",
                "param": "port",
            },
        ],
    },
    {
        "id": "port_channels",
        "label": "Port Channels",
        "items": [
            {
                "id": "pc_detail",
                "label": "Aggregate Interface Detail (counters)",
                "cmd": "show interfaces {port_channel} extensive",
                "param": "port_channel",
            },
            {
                "id": "lacp_detail",
                "label": "LACP Detail",
                "cmd": "show lacp interfaces {port_channel}",
                "param": "port_channel",
            },
        ],
    },
    {
        "id": "l2",
        "label": "Layer 2",
        "items": [
            {"id": "vlans", "label": "VLANs", "cmd": "show vlans"},
            {"id": "eth_switching", "label": "Ethernet Switching Table", "cmd": "show ethernet-switching table"},
            {"id": "stp", "label": "Spanning Tree Bridge", "cmd": "show spanning-tree bridge"},
        ],
    },
    {
        "id": "neighbors",
        "label": "Neighbors",
        "items": [
            {"id": "lldp", "label": "LLDP Neighbors", "cmd": "show lldp neighbors"},
        ],
    },
]

OPNSENSE_COMMAND_TREE = [
    {
        "id": "system",
        "label": "System",
        "items": [
            {"id": "version", "label": "Version", "cmd": "uname -a"},
            {"id": "uptime", "label": "Uptime / Load", "cmd": "uptime"},
            {"id": "top", "label": "CPU / Memory (top)", "cmd": "top -b -d 1"},
        ],
    },
    {
        "id": "interfaces",
        "label": "Interfaces",
        "items": [
            {"id": "ifconfig", "label": "Interface Status", "cmd": "ifconfig -a"},
            {"id": "netstat_i", "label": "Interface Counters", "cmd": "netstat -i"},
        ],
    },
    {
        "id": "routing",
        "label": "Routing",
        "items": [
            {"id": "routes", "label": "Routing Table", "cmd": "netstat -rn"},
            {"id": "arp", "label": "ARP Table", "cmd": "arp -an"},
        ],
    },
    {
        "id": "firewall",
        "label": "Firewall (pf)",
        "items": [
            {"id": "pf_info", "label": "State Table Info", "cmd": "pfctl -s info"},
            {"id": "pf_rules", "label": "Active Rules", "cmd": "pfctl -s rules"},
            {"id": "pf_nat", "label": "NAT Rules", "cmd": "pfctl -s nat"},
        ],
    },
]

COMMAND_TREES = {"os9": COMMAND_TREE, "junos": JUNOS_COMMAND_TREE, "opnsense": OPNSENSE_COMMAND_TREE}


def find_command(category_id, command_id, platform="os9"):
    tree = COMMAND_TREES.get(platform, COMMAND_TREE)
    for cat in tree:
        if cat["id"] != category_id:
            continue
        for item in cat["items"]:
            if item["id"] == command_id:
                return item
    return None
