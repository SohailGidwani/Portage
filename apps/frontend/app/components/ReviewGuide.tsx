"use client";

import Link from "next/link";
import { CopyButton } from "./ui";

export function ReviewGuide({ jobId, compact = false }: { jobId?: string; compact?: boolean }) {
  const short = jobId?.slice(0, 8) ?? "<job-id>";
  const exportCommand = `portage diff ${jobId ?? "<job-id>"} --output portage-${short}.patch`;
  const commands = `${exportCommand}\ngit apply --check portage-${short}.patch\ngit switch -c portage/${short}\ngit apply --index portage-${short}.patch\ncode .`;

  return (
    <section className={`review-guide${compact ? " compact" : ""}`}>
      <div className="section-heading">
        <div>
          <span className="kicker">Review in your editor</span>
          <h2>Move from generated diff to a normal code review</h2>
        </div>
        <CopyButton value={commands} label="Copy workflow" />
      </div>
      <div className="review-steps">
        <div><span>1</span><strong>Export</strong><p>Save the exact run diff as a patch.</p></div>
        <div><span>2</span><strong>Check</strong><p>Validate it against a clean checkout first.</p></div>
        <div><span>3</span><strong>Apply</strong><p>Use a disposable branch and stage the patch.</p></div>
        <div><span>4</span><strong>Review</strong><p>Open Source Control in your usual IDE.</p></div>
      </div>
      <pre className="command-block"><code>{commands}</code></pre>
      <p className="guide-note">
        Cursor: <code>cursor .</code> · Zed: <code>zed .</code> · JetBrains: open the checkout folder. Never apply a generated patch over uncommitted work. {!compact && <Link href="/guide">Read the full review guide</Link>}
      </p>
    </section>
  );
}
