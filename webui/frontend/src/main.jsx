import React from "react";
import ReactDOM from "react-dom/client";
import "@cloudscape-design/global-styles/index.css";
import "./index.css";
import App from "./App.jsx";
import { initPwa } from "./usePush.js";

// Before render: the browser offers its install prompt exactly once and
// only if it is caught early, and the service worker (paging) registers
// on load.
initPwa();

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
