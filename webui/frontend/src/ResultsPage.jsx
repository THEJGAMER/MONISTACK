import React, { useEffect, useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Modal from "@cloudscape-design/components/modal";

import { listResults, getResult, deleteResult } from "./api.js";
import MiniMarkdown from "./MiniMarkdown.jsx";
import { useClientPagination } from "./useClientPagination.js";

export default function ResultsPage({ pushFlash }) {
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState("");
  const [viewing, setViewing] = useState(null); // { filename, content }

  async function refresh() {
    setLoading(true);
    try {
      setResults(await listResults());
    } catch (e) {
      pushFlash("error", `Could not load saved results: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function handleView(filename) {
    try {
      const res = await getResult(filename);
      setViewing(res);
    } catch (e) {
      pushFlash("error", `Could not open ${filename}: ${e.message}`);
    }
  }

  async function handleDelete(filename) {
    try {
      await deleteResult(filename);
      setResults((prev) => prev.filter((r) => r.filename !== filename));
      pushFlash("success", `Deleted ${filename}.`);
    } catch (e) {
      pushFlash("error", `Could not delete ${filename}: ${e.message}`);
    }
  }

  const filtered = useMemo(() => {
    const q = filterText.toLowerCase();
    if (!q) return results;
    return results.filter((r) => (r.title || r.filename).toLowerCase().includes(q));
  }, [results, filterText]);

  const { pageItems, paginationProps } = useClientPagination(filtered, 10);

  return (
    <>
      <Container
        header={
          <Header
            variant="h2"
            description="Every command run is auto-saved here, backed by SQLite - not just what you explicitly saved."
          >
            Saved Results
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
              filteringPlaceholder="Search results..."
            />
          }
          pagination={<Pagination {...paginationProps} />}
          columnDefinitions={[
            { id: "title", header: "Result", cell: (r) => r.title || r.filename },
            { id: "saved_at", header: "Saved at", cell: (r) => r.saved_at },
            {
              id: "kind",
              header: "Kind",
              cell: (r) => <StatusIndicator type={r.auto_saved ? "info" : "success"}>{r.auto_saved ? "Auto" : "Manual"}</StatusIndicator>,
            },
            { id: "size", header: "Size", cell: (r) => `${r.size} B` },
            {
              id: "actions",
              header: "",
              cell: (r) => (
                <SpaceBetween size="xs" direction="horizontal">
                  <Button variant="inline-link" onClick={() => handleView(r.filename)}>
                    View
                  </Button>
                  <Button variant="inline-link" onClick={() => handleDelete(r.filename)}>
                    Delete
                  </Button>
                </SpaceBetween>
              ),
            },
          ]}
          empty={<Box textAlign="center">No saved results yet - every command run in the Console is auto-saved here.</Box>}
        />
      </Container>

      <Modal visible={!!viewing} onDismiss={() => setViewing(null)} header={viewing?.filename} size="large">
        {viewing && <MiniMarkdown source={viewing.content} />}
      </Modal>
    </>
  );
}
