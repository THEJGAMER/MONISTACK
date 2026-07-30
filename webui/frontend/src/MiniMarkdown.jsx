import React from "react";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";

// A tiny renderer for exactly the Markdown subset this app generates itself
// (## headings, **bold**, `inline code`, ```text code fences``` ). Not a
// general-purpose Markdown parser - deliberately not pulling in a full
// remark/rehype dependency chain for three constructs we fully control.

function parseInline(text, keyPrefix) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter((p) => p !== "");
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={`${keyPrefix}-${i}`}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return (
        <Box key={`${keyPrefix}-${i}`} variant="code" display="inline">
          {part.slice(1, -1)}
        </Box>
      );
    }
    return part;
  });
}

export default function MiniMarkdown({ source }) {
  const lines = (source || "").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      blocks.push({ type: "code", content: codeLines.join("\n") });
      continue;
    }
    if (line.startsWith("## ")) {
      blocks.push({ type: "heading", content: line.slice(3) });
      i++;
      continue;
    }
    if (line.trim() === "") {
      i++;
      continue;
    }
    blocks.push({ type: "text", content: line.replace(/\s+$/, "") });
    i++;
  }

  return (
    <SpaceBetween size="s">
      {blocks.map((block, idx) => {
        if (block.type === "heading") {
          return (
            <Box key={idx} variant="h3">
              {parseInline(block.content, idx)}
            </Box>
          );
        }
        if (block.type === "code") {
          return (
            <Box key={idx} variant="code" display="block" className="terminal-output">
              {block.content}
            </Box>
          );
        }
        return <Box key={idx}>{parseInline(block.content, idx)}</Box>;
      })}
    </SpaceBetween>
  );
}
