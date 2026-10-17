import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, useLocation } from "react-router-dom";
import "./index.css";
import "./abide.css";
import { Layout } from "./components/Layout";
import { Delegate } from "./pages/Delegate";
import { Overview } from "./pages/Overview";

// Dark is the primary theme; an explicit choice is remembered. Applied before first paint.
try {
  document.documentElement.classList.toggle("dark", localStorage.getItem("theme") !== "light");
} catch {
  document.documentElement.classList.add("dark");
}

/** One page. A shared link such as #/?section=try opens at that section. */
function Page() {
  const { search } = useLocation();
  useEffect(() => {
    const id = new URLSearchParams(search).get("section");
    const target = id ? document.getElementById(id) : null;
    if (target) { target.scrollIntoView({ behavior: "instant" }); target.focus({ preventScroll: true }); }
  }, [search]);
  return <Overview workspace={<Delegate />} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <HashRouter>
      <Layout><Page /></Layout>
    </HashRouter>
  </StrictMode>,
);
