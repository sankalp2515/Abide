import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { PRODUCT_NAME } from "../api";
import { Arrow, SealMark } from "./ui";

/** Keep section navigation separate from HashRouter's route fragment. */
export function SectionLink({ id, children, className }: { id: string; children: ReactNode; className?: string }) {
  return <a href={`#/?section=${encodeURIComponent(id)}`} className={className} onClick={(event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = document.getElementById(id);
    if (!target) return;
    event.preventDefault();
    event.currentTarget.closest("details")?.removeAttribute("open");
    target.scrollIntoView({ block: "start", behavior: "instant" });
    target.focus({ preventScroll: true });
  }}>{children}</a>;
}

const SECTIONS = [{ id: "why", label: "Why" }, { id: "how", label: "How it works" }, { id: "evidence", label: "Evidence" }, { id: "integrate", label: "Developers" }, { id: "faq", label: "FAQ" }];

export function LandingHeader({ themeToggle }: { themeToggle: ReactNode }) {
  return <header className="landing-header">
    <div className="landing-header-inner">
      <Link to="/" className="landing-brand" aria-label={`${PRODUCT_NAME} home`} onClick={(event) => {
        if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) window.scrollTo({ top: 0, behavior: "instant" });
      }}><SealMark /><span className="brand-text">{PRODUCT_NAME}</span></Link>
      <nav className="landing-desktop-nav" aria-label="Product">{SECTIONS.map((s) => <SectionLink key={s.id} id={s.id}>{s.label}</SectionLink>)}</nav>
      <details className="landing-menu"><summary>Menu</summary><nav aria-label="Mobile product">
        {SECTIONS.map((s) => <SectionLink key={s.id} id={s.id}>{s.label}</SectionLink>)}
        <Link to="/decisions">Open console</Link><div>{themeToggle}</div>
      </nav></details>
      <div className="landing-header-actions"><Link to="/decisions" className="landing-console-link">Open console ↗</Link><span className="landing-theme">{themeToggle}</span><Link to="/delegate" className="pill pill-primary"><span className="landing-demo-long">Start free trial</span><span className="landing-demo-short">Try it</span><Arrow /></Link></div>
    </div>
  </header>;
}

export function LandingFooter() {
  return <footer className="landing-footer">
    <div><span className="brand-text">{PRODUCT_NAME}</span><p>Hand off the task. Approve it once.</p></div>
    <nav aria-label="Footer"><Link to="/delegate">Start free trial</Link><Link to="/decisions">Console</Link><SectionLink id="evidence">Evidence</SectionLink><SectionLink id="integrate">Developers</SectionLink><SectionLink id="faq">FAQ</SectionLink></nav>
    <p className="landing-footnote">Built with Gemini on Vertex AI, Google ADK and Model Armor · Demo actions run in a sandbox · Rules decide, AI advises</p>
  </footer>;
}
