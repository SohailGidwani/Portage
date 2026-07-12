"use client";

import { useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { ReviewGuide } from "../components/ReviewGuide";
import { CopyButton } from "../components/ui";
import { ApiKeySummary, Me, createApiKey, getMe, listApiKeys, loginUrl, revokeApiKey, tryRefresh } from "../lib/api";

export default function GuidePage() {
  const [me, setMe] = useState<Me | null>(null);
  const [keys, setKeys] = useState<ApiKeySummary[]>([]);
  const [newKey, setNewKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    (async () => {
      await tryRefresh();
      const user = await getMe();
      setMe(user);
      if (user) setKeys(await listApiKeys());
      setAuthChecked(true);
    })();
  }, []);

  async function generateKey() {
    setBusy(true);
    const created = await createApiKey("local CLI");
    setNewKey(created.key);
    setKeys(await listApiKeys());
    setBusy(false);
  }

  return (
    <AppShell eyebrow="Workflow guide" title="Review Portage changes safely" description="The browser explains the run; your checkout and IDE remain the source of truth.">
      <section className="guide-hero"><span className="kicker">Recommended workflow</span><h2>Treat every generated migration like a pull request</h2><p>Export the exact patch from a completed run, validate it against the intended revision, apply it on a clean branch, and use the same review and test tools your team already trusts.</p></section>
      <ReviewGuide />

      <section className="surface credential-card">
        <div>
          <span className="kicker">CLI credentials</span>
          <h2>Connect the terminal to a GitHub-authenticated control plane</h2>
          <p>Generate a revocable API key, copy it once, and expose it as <code>PORTAGE_API_KEY</code>. Only a SHA-256 hash is stored by Portage.</p>
        </div>
        {!authChecked ? <span className="muted">Checking session…</span> : !me ? <a className="button primary" href={loginUrl()}>Sign in to generate a key</a> : <button className="button primary" onClick={generateKey} disabled={busy}>{busy ? "Generating…" : "Generate CLI key"}</button>}
        {newKey && <div className="new-key"><strong>Copy this now—it will not be shown again.</strong><div><code>{newKey}</code><CopyButton value={newKey} label="Copy key" /></div><pre className="command-block"><code>{`export PORTAGE_API_KEY=${newKey}\nexport PORTAGE_API=http://localhost:8000`}</code></pre></div>}
        {keys.length > 0 && <div className="key-list"><span className="kicker">Active keys</span>{keys.map((key) => <div key={key.id}><span><strong>{key.name}</strong><small>Created {new Date(key.created_at).toLocaleDateString()}{key.last_used_at ? ` · used ${new Date(key.last_used_at).toLocaleDateString()}` : " · never used"}</small></span><button className="button ghost" onClick={async () => { await revokeApiKey(key.id); setKeys(await listApiKeys()); }}>Revoke</button></div>)}</div>}
      </section>

      <div className="guide-grid">
        <section className="surface guide-section"><span className="guide-number">01</span><h2>Start from the same revision</h2><p>If the run used a pinned SHA, check out that SHA before applying the patch. A patch can fail—or worse, apply ambiguously—against a different codebase state.</p><pre className="command-block"><code>{`git status --short\ngit switch --detach <pinned-sha>\ngit switch -c portage/review-<job-id>`}</code></pre></section>
        <section className="surface guide-section"><span className="guide-number">02</span><h2>Validate before applying</h2><p><code>git apply --check</code> is read-only. It confirms that the patch matches your checkout without changing files.</p><pre className="command-block"><code>git apply --check migration.patch</code></pre></section>
        <section className="surface guide-section"><span className="guide-number">03</span><h2>Open your normal review surface</h2><p>Apply with <code>--index</code> so the migrated files appear as staged changes. Then use Source Control, tests, blame, and language tooling normally.</p><pre className="command-block"><code>{`git apply --index migration.patch\ncode .        # VS Code\ncursor .      # Cursor\nzed .         # Zed`}</code></pre></section>
        <section className="surface guide-section"><span className="guide-number">04</span><h2>Know what each signal means</h2><ul className="check-list"><li><strong>Outcome</strong> is the honest migration verdict.</li><li><strong>Full suite</strong> is the final repository test result.</li><li><strong>Oracle integrity</strong> proves protected tests were not weakened.</li><li><strong>Recovery</strong> records retries, rollback decisions, and escalation.</li></ul></section>
      </div>
      <section className="surface safety-callout"><div><span className="kicker">Safety boundary</span><h2>Portage does not apply the patch to your working checkout.</h2></div><p>That handoff remains explicit so uncommitted work cannot be overwritten and so you choose the branch, revision, and review policy.</p></section>
    </AppShell>
  );
}
