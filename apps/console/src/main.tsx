import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import "./index.css";
import "./landing.css";
import { Layout } from "./components/Layout";
import { Evaluation } from "./pages/Evaluation";
import { Connect } from "./pages/Connect";
import { Decisions } from "./pages/Decisions";
import { Inbox } from "./pages/Inbox";
import { Tasks } from "./pages/Tasks";
import { Overview } from "./pages/Overview";
import { Policy } from "./pages/Policy";
import { Replay } from "./pages/Replay";
import { Review } from "./pages/Review";
import { Setup } from "./pages/Setup";

// Ledger paper (light) is the primary theme; "Night shift" (dark) is remembered when chosen. Before first paint.
try {
  document.documentElement.classList.toggle("dark", localStorage.getItem("theme") === "dark");
} catch {
  /* storage unavailable: light */
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* HashRouter: works on any static host without rewrite rules */}
    <HashRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/delegate" element={<Tasks />} />
          <Route path="/agents" element={<Tasks />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/activity" element={<Decisions />} />
          <Route path="/connect" element={<Connect />} />
          <Route path="/evaluation" element={<Evaluation />} />
          <Route path="/replay" element={<Replay />} />
          <Route path="/decisions" element={<Decisions />} />
          <Route path="/review" element={<Review />} />
          <Route path="/policy" element={<Policy />} />
          <Route path="/setup" element={<Setup />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </HashRouter>
  </StrictMode>,
);
