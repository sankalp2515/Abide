import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import "./index.css";
import "./landing.css";
import { Layout } from "./components/Layout";
import { Decisions } from "./pages/Decisions";
import { Delegate } from "./pages/Delegate";
import { Overview } from "./pages/Overview";
import { Policy } from "./pages/Policy";
import { Replay } from "./pages/Replay";
import { Review } from "./pages/Review";
import { Setup } from "./pages/Setup";

// Dark is the primary Abide theme; an explicit choice is remembered. Applied before first paint.
try {
  document.documentElement.classList.toggle("dark", localStorage.getItem("theme") !== "light");
} catch {
  document.documentElement.classList.add("dark");
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* HashRouter: works on any static host without rewrite rules */}
    <HashRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/delegate" element={<Delegate />} />
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
