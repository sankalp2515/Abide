import type { ReactNode } from "react";
import { PRODUCT_NAME, TAGLINE } from "../api";
import { SealMark } from "./ui";

/** Scroll to a section of the one page, keeping the URL shareable (#/?section=id). */
export function SectionLink({ id, children, className }: { id: string; children: ReactNode; className?: string }) {
  return <a href={`#/?section=${encodeURIComponent(id)}`} className={className} onClick={(event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = document.getElementById(id);
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({ block: "start", behavior: "smooth" });
    target.focus({ preventScroll: true });
  }}>{children}</a>;
}

export function LandingHeader({ themeToggle, status }: { themeToggle: ReactNode; status?: ReactNode }) {
  return <header className="landing-header">
    <div className="landing-header-inner">
      <a href="#/" className="landing-brand" aria-label={`${PRODUCT_NAME}, back to top`} onClick={(event) => {
        event.preventDefault();
        window.scrollTo({ top: 0, behavior: "smooth" });
      }}><SealMark /><span className="brand-text">{PRODUCT_NAME}</span></a>
      <div className="landing-header-actions">{status}<span className="landing-theme">{themeToggle}</span>
        <SectionLink id="try" className="pill pill-primary">Try it</SectionLink></div>
    </div>
  </header>;
}

export function LandingFooter() {
  return <footer className="landing-footer">
    <div><span className="brand-text">{PRODUCT_NAME}</span><p>{TAGLINE}</p></div>
    <nav aria-label="Footer"><SectionLink id="try">Try it</SectionLink><SectionLink id="evidence">Evidence</SectionLink><SectionLink id="integrate">Bring your own agent</SectionLink><SectionLink id="faq">Questions</SectionLink></nav>
    <p className="landing-footnote">Built with Gemini on Vertex AI, Google ADK and Model Armor · Demo actions run in a sandbox · Rules decide, AI advises</p>
  </footer>;
}
