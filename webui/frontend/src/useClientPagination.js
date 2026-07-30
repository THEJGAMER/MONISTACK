import { useEffect, useMemo, useState } from "react";

// Cloudscape's Pagination component is presentation-only - the caller is
// expected to slice its own items. Shared here since four different tables
// (Devices, Saved Results, Console's Recent results, Syslog) all need the
// same "slice + page-count + reset when the filtered set shrinks" logic.
export function useClientPagination(items, pageSize = 10) {
  const [currentPageIndex, setCurrentPageIndex] = useState(1);
  const pagesCount = Math.max(1, Math.ceil(items.length / pageSize));

  useEffect(() => {
    if (currentPageIndex > pagesCount) setCurrentPageIndex(1);
  }, [pagesCount, currentPageIndex]);

  const pageItems = useMemo(() => {
    const start = (currentPageIndex - 1) * pageSize;
    return items.slice(start, start + pageSize);
  }, [items, currentPageIndex, pageSize]);

  const paginationProps = {
    currentPageIndex,
    pagesCount,
    onChange: ({ detail }) => setCurrentPageIndex(detail.currentPageIndex),
  };

  return { pageItems, paginationProps };
}
