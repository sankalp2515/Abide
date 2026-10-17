import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { PRODUCT_NAME } from "../api";
import { Wordmark } from "./ui";

/** Keep section navigation separate from HashRouter's route fragment. */
export function SectionLink({ id, children, className }: { id: string; children: ReactNode; className?: string }) {
  return <a href={`#/?section=${encodeURIComponent(id)}`} className={className} onClick={(event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = document.getElementById(id);
    if (!target) return;
    event.preventDefault();
    event.currentTarget.closest("details")?.removeAttribute("open");
    target.scrollIntoView({ block: "start", behavior: "smooth" });
    target.focus({ preventScroll: true });
  }}>{children}</a>;
}

const SECTIONS = [{ id: "how", label: "How it works" }, { id: "evidence", label: "Evidence" }, { id: "integrate", label: "Developers" }, { id: "pricing", label: "Pricing" }, { id: "faq", label: "Questions" }];

export function LandingHeader({ themeToggle }: { themeToggle: ReactNode }) {
  return <header className="lg-header">
    <div className="lg-wrap lg-header-inner">
      <Link to="/" aria-label={`${PRODUCT_NAME} home`} onClick={(event) => {
        if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) window.scrollTo({ top: 0, behavior: "smooth" });
      }}><Wordmark /></Link>
      <nav className="lg-nav" aria-label="Product">{SECTIONS.map((s) => <SectionLink key={s.id} id={s.id}>{s.label}</SectionLink>)}</nav>
      <div className="lg-header-actions">
        <span className="lg-theme">{themeToggle}</span>
        <Link to="/decisions" className="lg-signin">Console</Link>
        <Link to="/delegate" className="pill pill-primary pill-sm">Run a live task</Link>
      </div>
      <details className="lg-menu"><summary>Menu</summary><nav aria-label="Mobile product">
        {SECTIONS.map((s) => <SectionLink key={s.id} id={s.id}>{s.label}</SectionLink>)}
        <Link to="/decisions">Console</Link><div>{themeToggle}</div>
      </nav></details>
    </div>
  </header>;
}

export function LandingFooter() {
  return <footer className="lg-footer">
    <div className="lg-wrap lg-footer-inner">
      <div><Wordmark /><p>Hand off the task. Approve it once.</p></div>
      <nav aria-label="Footer"><Link to="/delegate">Run a live task</Link><Link to="/agents">Your agents</Link><Link to="/decisions">Console</Link><SectionLink id="evidence">Evidence</SectionLink><SectionLink id="integrate">Developers</SectionLink><a href="https://github.com/sankalp2515/Abide" rel="noreferrer" target="_blank">GitHub</a></nav>
      <p className="lg-footnote">Built on Google Cloud: Gemini on Vertex AI, Agent Development Kit, Model Armor, Cloud Run, Cloud SQL, BigQuery · Demo actions run in a sandbox · Rules decide, Gemini explains</p>
    </div>
  </footer>;
}
