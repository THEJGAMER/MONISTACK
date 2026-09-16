// The audit log, as its own tab on the Events page.
import React, { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import Pagination from "@cloudscape-design/components/pagination";
import TextFilter from "@cloudscape-design/components/text-filter";
import Badge from "@cloudscape-design/components/badge";

import { useClientPagination } from "./useClientPagination.js";
import { getAuditLog } from "./api.js";

export default function AuditLogTab({ pushFlash }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState("");

  async function refresh() {
    setLoading(true);
    try {
      setEntries(await getAuditLog(500));
    } catch (e) {
      pushFlash("error", `Could not load audit log: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = entries.filter((e) => {
    const q = filterText.toLowerCase();
    if (!q) return true;
    return (
      e.action.toLowerCase().includes(q) ||
      e.actor.toLowerCase().includes(q) ||
      (e.target || "").toLowerCase().includes(q)
    );
  });
  const { pageItems, paginationProps } = useClientPagination(filtered, 15);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="What people did in Switchboard - manual resolves, event and rule configuration changes, device edits - with who and when. The Events tab covers what the devices did."
          actions={
            <Button iconName="refresh" loading={loading} onClick={refresh}>
              Refresh
            </Button>
          }
        >
          Audit &amp; event log
        </Header>
      }
    >
      <Table
        variant="embedded"
        loading={loading}
        items={pageItems}
        filter={
          <TextFilter
            filteringText={filterText}
            onChange={({ detail }) => setFilterText(detail.filteringText)}
            filteringPlaceholder="Search action, user or target..."
          />
        }
        pagination={<Pagination {...paginationProps} />}
        columnDefinitions={[
          { id: "ts", header: "Time", cell: (e) => new Date(e.ts).toLocaleString() },
          { id: "actor", header: "User", cell: (e) => e.actor },
          { id: "action", header: "Action", cell: (e) => <Badge>{e.action}</Badge> },
          { id: "target", header: "Target", cell: (e) => e.target || "-" },
          {
            id: "detail",
            header: "Detail",
            cell: (e) => {
              if (!e.detail) return "-";
              if (e.detail.note) return e.detail.note;
              if (e.detail.comment) return e.detail.comment;
              // Skip null/undefined values - an action taken without an
              // optional note used to render a literal "note=null", which
              // reads like a recorded value rather than the absence of one.
              const parts = Object.entries(e.detail)
                .filter(([k, v]) => k !== "labels" && v !== null && v !== undefined)
                .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
              return parts.length ? parts.join(", ") : "-";
            },
          },
        ]}
        empty={<Box textAlign="center">Nothing recorded yet.</Box>}
      />
    </Container>
  );
}
