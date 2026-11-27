import { Alert, Button, Card, StatusPill, type StatusTone } from "@eaios/ui";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { admin, type TenantSettings } from "./admin";
import { ActionsPanel, type ReportSource } from "./ActionsPanel";
import { AdminPanel } from "./AdminPanel";
import { AppearanceSettings } from "./AppearanceSettings";
import { useAppearance } from "./useAppearance";
import { ChatPage, type ShellView } from "./ChatPage";
import { api, ApiError, type Identity } from "./api";
import { desktop } from "./desktop";
import { DocumentsPanel } from "./DocumentsPanel";
import { LocalFoldersPanel } from "./LocalFoldersPanel";
import { ReportPanel } from "./ReportPanel";
import { useApiStatus, type ApiStatus } from "./useApiStatus";

interface Session {
  token: string; // memory only
  identity: Identity;
}

function statusView(status: ApiStatus): { tone: StatusTone; label: string; detail?: string } {
  switch (status.kind) {
    case "checking":
      return { tone: "unknown", label: "Checking API…" };
    case "unreachable":
      return { tone: "error", label: "API unreachable", detail: "The backend is not responding." };
    case "degraded": {
      const failing = Object.entries(status.readiness.checks)
        .filter(([, v]) => v !== "ok")
        .map(([k]) => k);
      return {
        tone: "warn",
        label: "API degraded",
        detail: `Unavailable: ${failing.join(", ") || "unknown"}`,
      };
    }
    case "ready":
      return { tone: "ok", label: "API ready" };
  }
}

function ApiStatusBar({ status }: { status: ApiStatus }) {
  const view = statusView(status);
  const model =
    status.kind === "ready" || status.kind === "degraded" ? status.readiness.model_inference : null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <StatusPill tone={view.tone}>{view.label}</StatusPill>
      {view.detail ? <span className="text-muted-fg">{view.detail}</span> : null}
      {model ? (
        <span className="text-muted-fg">
          Model inference:{" "}
          {model === "not_configured"
            ? "not configured"
            : model === "fake"
              ? "FAKE (development double, not a model)"
              : "external (unchecked)"}
        </span>
      ) : null}
    </div>
  );
}

function SignInForm({ disabled, onSignedIn }: { disabled: boolean; onSignedIn: (s: Session) => void }) {
  const [tenant, setTenant] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { access_token } = await api.login(tenant.trim(), email.trim(), password);
      const identity = await api.me(access_token);
      setPassword("");
      onSignedIn({ token: access_token, identity });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.code === "rate_limited"
            ? "Too many failed attempts. Please wait and try again."
            : err.code === "invalid_credentials"
              ? "Sign-in failed. Check your organization, email and password."
              : err.message,
        );
      } else {
        setError("Could not reach the API.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="avatar">N</span>
        <h3>Sign in to NileTech Assistant</h3>
      </div>
      <p className="note">
        Use your organization account. Your access is loaded from the server, never from the browser.
      </p>
      <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)} noValidate>
        <div className="fld">
          <label htmlFor="tenant">Organization</label>
          <input id="tenant" autoComplete="organization" placeholder="organization-slug" value={tenant}
            onChange={(e) => setTenant(e.target.value)} required />
        </div>
        <div className="fld">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" autoComplete="username" value={email}
            onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="fld">
          <label htmlFor="password">Password</label>
          <input id="password" type="password" autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </div>
        {error ? <p className="err" role="alert">{error}</p> : null}
        <button type="submit" className="btn" disabled={disabled || busy || !tenant || !email || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-1 gap-1 py-2 sm:grid-cols-3">
      <dt className="text-sm text-muted-fg">{label}</dt>
      <dd className="text-sm break-words sm:col-span-2">{value}</dd>
    </div>
  );
}

function IdentityView({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const { identity } = session;
  const caps = identity.capabilities
    .map((c) => (c.scope_department ? `${c.capability} (${c.scope_department})` : c.capability))
    .join(", ");
  return (
    <Card title="Your identity">
      <p className="mb-4 text-sm text-muted-fg">
        These details come from the server for your current session. They cannot be changed from
        the browser.
      </p>
      <dl className="divide-y divide-border">
        <Row label="Name" value={identity.display_name} />
        <Row label="Email" value={identity.email} />
        <Row label="Organization" value={`${identity.tenant.name} (${identity.tenant.slug})`} />
        <Row label="Roles" value={identity.roles.join(", ") || "none"} />
        <Row label="Capabilities" value={caps || "none"} />
        <Row label="Groups" value={identity.groups.join(", ") || "none"} />
        <Row label="Department" value={identity.attributes.department ?? "not set"} />
        <Row label="Country" value={identity.attributes.country ?? "not set"} />
        <Row label="Clearance" value={identity.attributes.clearance ?? "not set"} />
        <Row label="Policy version" value={String(identity.policy_epoch)} />
        <Row
          label="Session expires"
          value={new Date(identity.session_expires_at).toLocaleString()}
        />
      </dl>
      <div className="mt-6 flex justify-end">
        <Button variant="secondary" onClick={onSignOut}>
          Sign out
        </Button>
      </div>
    </Card>
  );
}

export function App() {
  const status = useApiStatus();
  const [appearance, setAppearance] = useAppearance();
  const bridge = desktop();
  const [session, setSession] = useState<Session | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [settings, setSettings] = useState<TenantSettings | null>(null);
  const [desktopSession, setDesktopSession] = useState<{ session: Session; ready: boolean } | null>(null);
  const [reportSource, setReportSource] = useState<ReportSource | null>(null);
  const [view, setView] = useState<ShellView>("chat");
  const onReportPublished = useCallback((runId: string, title: string) => setReportSource({ runId, title }), []);
  // "Email this report" binds the email form to the report that was clicked, even when a
  // newer report was published since. The run reference tells same-titled reports apart.
  const onEmailReport = useCallback(
    (runId: string, title: string) => setReportSource({ runId, title: `${title} · report ${runId.slice(0, 8)}` }),
    [],
  );

  useEffect(() => {
    let current = true;
    // The panel waits for identity, watcher setup and pending removal retries in Electron.
    void bridge?.setSession(session?.token ?? null).then(
      () => { if (current && session) setDesktopSession({ session, ready: true }); },
      () => { if (current && session) setDesktopSession({ session, ready: false }); },
    );
    setDesktopSession(null);
    setSettings(null);
    setReportSource(null);
    if (session) {
      void admin.tenantSettings(session.token).then(
        (value) => { if (current) setSettings(value); },
        () => { if (current) setSettings(null); },
      );
    }
    return () => { current = false; };
  }, [bridge, session]);

  const signOut = useCallback(async () => {
    const current = session;
    setSession(null);
    if (!current) return;
    try {
      await api.logout(current.token);
      setNotice("You have signed out.");
    } catch {
      setNotice("Signed out locally. The server session will expire on its own.");
    }
  }, [session]);

  if (!session) {
    return (
      <div className="eaios-signin">
        <div className="flex w-full max-w-[400px] flex-col gap-3">
          <SignInForm
            disabled={status.kind === "unreachable"}
            onSignedIn={(s) => {
              setNotice(null);
              setView("chat");
              setSession(s);
            }}
          />
          <div className="status">
            <ApiStatusBar status={status} />
          </div>
          {notice ? <p className="note">{notice}</p> : null}
        </div>
      </div>
    );
  }

  const caps = session.identity.capabilities.map((c) => c.capability);
  const showActivity = ["resource.admin", "identity.admin", "audit.read", "tenant.config"].some((c) =>
    caps.includes(c),
  );
  const fake =
    (status.kind === "ready" || status.kind === "degraded") && status.readiness.model_inference === "fake";

  return (
    // Keyed by session: a new sign-in starts with an empty, memory-only chat history.
    <ChatPage
      key={session.token}
      token={session.token}
      identity={session.identity}
      packs={settings?.enabled_packs ?? null}
      modelLabel={
        status.kind === "unreachable"
          ? "API unreachable"
          : fake
            ? "FAKE model (development double)"
            : null
      }
      canEmail={Boolean(settings?.enabled_packs.includes("actions"))}
      showActivity={showActivity}
      view={view}
      onNavigate={setView}
      onReportPublished={onReportPublished}
      onEmailReport={onEmailReport}
      onSignOut={() => void signOut()}
      pages={{
        files: (
          <>
            <h1>Files and sources</h1>
            <DocumentsPanel
              token={session.token}
              canUpload={
                caps.includes("resource.admin") && Boolean(settings?.enabled_connectors.includes("upload"))
              }
            />
            {bridge && settings?.enabled_connectors.includes("local_folder") ? (
              desktopSession?.session === session && desktopSession.ready ? (
                <LocalFoldersPanel token={session.token} groups={session.identity.groups} />
              ) : (
                <Card title="Local folders">
                  {desktopSession?.session === session ? (
                    <Alert>Could not connect local folders to this account. Sign in again to retry.</Alert>
                  ) : (
                    <p className="text-sm text-muted-fg" role="status">Loading local folders…</p>
                  )}
                </Card>
              )
            ) : null}
          </>
        ),
        approvals: (
          <>
            <h1>Approvals</h1>
            <p className="note">
              Emails are drafted from a verified report — the latest one from chat, or one you run
              here — and are sent only after you approve the exact version.
            </p>
            {settings?.enabled_packs.includes("actions") ? (
              <ActionsPanel token={session.token} source={reportSource} />
            ) : (
              <p className="note">The actions pack is not enabled for your organization.</p>
            )}
            <ReportPanel
              token={session.token}
              packs={settings?.enabled_packs ?? null}
              onReportPublished={onReportPublished}
            />
          </>
        ),
        activity: (
          <>
            <h1>Activity log and administration</h1>
            <AdminPanel
              token={session.token}
              identity={session.identity}
              settings={settings}
              onSettings={setSettings}
            />
          </>
        ),
        settings: (
          <>
            <h1>Settings</h1>
            <AppearanceSettings appearance={appearance} onChange={setAppearance} />
            <h2>Account</h2>
            <ApiStatusBar status={status} />
            <IdentityView session={session} onSignOut={() => void signOut()} />
          </>
        ),
      }}
    />
  );
}
