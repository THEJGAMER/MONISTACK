import React, { useEffect, useState } from "react";
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
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";

import { listResults, getResult, deleteResult, exportResultUrl } from "./api.js";
import MiniMarkdown from "./MiniMarkdown.jsx";

const PAGE_SIZE = 10;

export default function ResultsPage({ pushFlash }) {
  const [results, setResults] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState("");
  const [currentPageIndex, setCurrentPageIndex] = useState(1);
  const [viewing, setViewing] = useState(null); // { filename, content }

  // Server-side pagination + search - the results table is backed by
  // Postgres and grows without bound (every command run auto-saves a
  // row), so slicing a client-fetched array stops working once that
  // table is bigger than one page load's worth of rows.
  async function refresh(page, q) {
    setLoading(true);
    try {
      const res = await listResults({ page, pageSize: PAGE_SIZE, q: q || undefined });
      setResults(res.items);
      setTotal(res.total);
    } catch (e) {
      pushFlash("error", `Could not load saved results: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh(currentPageIndex, filterText);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentPageIndex, filterText]);

  // Reset to page 1 whenever the search text changes so the user isn't
  // stranded on a page number that no longer exists for the new filter.
  useEffect(() => {
    setCurrentPageIndex(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterText]);

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
      pushFlash("success", `Deleted ${filename}.`);
      refresh(currentPageIndex, filterText);
    } catch (e) {
      pushFlash("error", `Could not delete ${filename}: ${e.message}`);
    }
  }

  const pagesCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <>
      <Container
        header={
          <Header
            variant="h2"
            description="Every command run is auto-saved here, backed by Postgres - not just what you explicitly saved."
          >
            Saved Results
          </Header>
        }
      >
        <Table
          variant="embedded"
          loading={loading}
          items={results}
          filter={
            <TextFilter
              filteringText={filterText}
              onChange={({ detail }) => setFilterText(detail.filteringText)}
              filteringPlaceholder="Search results..."
            />
          }
          pagination={
            <Pagination
              currentPageIndex={currentPageIndex}
              pagesCount={pagesCount}
              onChange={({ detail }) => setCurrentPageIndex(detail.currentPageIndex)}
            />
          }
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
                  <ButtonDropdown
                    variant="inline-icon"
                    ariaLabel={`Export ${r.filename}`}
                    items={[
                      { id: "json", text: "Export JSON" },
                      { id: "csv", text: "Export CSV" },
                    ]}
                    onItemClick={({ detail }) => {
                      window.location.href = exportResultUrl(r.filename, detail.id);
                    }}
                  />
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
