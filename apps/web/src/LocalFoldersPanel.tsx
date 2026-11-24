import { Alert, Button, Card, StatusPill, type StatusTone } from "@eaios/ui";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";
import {
  desktop,
  localFolders,
  type DesktopFolder,
  type DesktopSyncStatus,
  type ServerFolder,
} from "./desktop";

const PHASE: Record<DesktopSyncStatus["phase"], { tone: StatusTone; label: string }> = {
  idle: { tone: "ok", label: "Up to date" },
  syncing: { tone: "unknown", label: "Syncing" },
  offline: { tone: "warn", label: "Offline — will retry" },
  signed_out: { tone: "warn", label: "Paused until you sign in" },
  folder_gone: { tone: "error", label: "Removed on the server" },
  error: { tone: "error", label: "Needs attention" },
};

/**
 * Desktop only: folders on this computer that sync to the organization. The server
 * decides who may read them: you, plus the groups you choose here (only groups you
 * belong to). File-system permissions on this computer are not used.
 */
export function LocalFoldersPanel({ token, groups }: { token: string; groups: string[] }) {
  const bridge = desktop();
  const [folders, setFolders] = useState<DesktopFolder[]>([]);
  const [server, setServer] = useState<Record<string, ServerFolder>>({});
  const [status, setStatus] = useState<Record<string, DesktopSyncStatus>>({});
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!bridge) return;
    try {
      const [local, remote] = await Promise.all([bridge.listFolders(), localFolders.list(token)]);
      setFolders(local);
      setServer(Object.fromEntries(remote.map((f) => [f.source_id, f])));
      setStatus((prev) => {
        const next = { ...prev };
        for (const f of local) if (f.status) next[f.folderId] = f.status;
        return next;
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load folders.");
    }
  }, [bridge, token]);

  useEffect(() => {
    void refresh();
    if (!bridge) return;
    return bridge.onStatus((s) => setStatus((prev) => ({ ...prev, [s.folderId]: s })));
  }, [bridge, refresh]);

  if (!bridge) return null;

  const add = async () => {
    setError(null);
    const root = await bridge.chooseFolder();
    if (!root) return;
    try {
      await bridge.addFolder(root);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the folder.");
    }
  };

  const toggleShare = async (folder: ServerFolder, group: string) => {
    const next = folder.shared_groups.includes(group)
      ? folder.shared_groups.filter((g) => g !== group)
      : [...folder.shared_groups, group];
    try {
      await localFolders.share(token, folder.source_id, next);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change sharing.");
    }
  };

  // Removal from the organization is done by the desktop main process, which keeps the
  // folder (on disk) as "removal pending" until the server confirms, and retries it at
  // the next sign-in or app start. The folder never disappears on a failed request.
  const remove = async (folder: DesktopFolder, fromServer: boolean) => {
    setError(null);
    try {
      const pending = await bridge.removeFolder(folder.folderId, fromServer);
      if (pending?.pendingRemoval) {
        setError("The folder could not be removed from the organization yet. It stays listed; retry when online.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the folder.");
    } finally {
      await refresh();
    }
  };

  return (
    <Card title="Local folders">
      <div className="flex flex-col gap-4 text-sm">
        <p className="text-muted-fg">
          Files in these folders are searchable by you, and by the groups you share with. Hidden,
          temporary and dependency files, links and files over 10 MB are never uploaded.
        </p>
        {error ? <Alert>{error}</Alert> : null}
        {folders.length === 0 ? <p className="text-muted-fg">No folders yet.</p> : null}
        <ul className="divide-y divide-border">
          {folders.map((f) => {
            const s = status[f.folderId];
            const remote = f.sourceId ? server[f.sourceId] : undefined;
            const phase = f.pendingRemoval
              ? { tone: "warn" as StatusTone, label: "Removal pending" }
              : s
                ? PHASE[s.phase]
                : { tone: "unknown" as StatusTone, label: "Starting" };
            return (
              <li key={f.folderId} className="flex flex-col gap-2 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{f.name}</span>
                  <StatusPill tone={phase.tone}>{phase.label}</StatusPill>
                  <span className="text-xs text-muted-fg break-all">{f.root}</span>
                </div>
                <div className="text-xs text-muted-fg">
                  {remote ? `${remote.active_items} files on the server · ` : ""}
                  {s ? `${s.pending} waiting · ${s.skipped} skipped locally` : ""}
                  {s?.unreadable ? ` · ${s.unreadable} unreadable (kept, retrying)` : ""}
                  {f.pendingRemoval
                    ? ` · removal from the organization pending${f.removalError ? ` (${f.removalError.replace(/_/g, " ")})` : ""}`
                    : ""}
                  {s?.lastSyncAt ? ` · last sync ${new Date(s.lastSyncAt).toLocaleTimeString()}` : ""}
                  {s?.detail ? ` · ${s.detail}` : ""}
                </div>
                {remote ? (
                  <div className="flex flex-wrap items-center gap-3 text-xs">
                    <span className="text-muted-fg">Shared with:</span>
                    {groups.length === 0 ? <span className="text-muted-fg">you only</span> : null}
                    {groups.map((g) => (
                      <label key={g} className="flex items-center gap-1">
                        <input
                          type="checkbox"
                          checked={remote.shared_groups.includes(g)}
                          onChange={() => void toggleShare(remote, g)}
                        />
                        {g}
                      </label>
                    ))}
                  </div>
                ) : null}
                {f.pendingRemoval ? (
                  <div className="flex flex-wrap gap-2">
                    <Button variant="secondary" onClick={() => void remove(f, true)}>
                      Retry removal
                    </Button>
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    <Button variant="secondary" onClick={() => void bridge.syncNow(f.folderId)}>
                      Sync now
                    </Button>
                    <Button variant="secondary" onClick={() => void remove(f, false)}>
                      Stop syncing
                    </Button>
                    <Button variant="secondary" onClick={() => void remove(f, true)}>
                      Remove from organization
                    </Button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
        <div>
          <Button onClick={() => void add()}>Add a folder</Button>
        </div>
      </div>
    </Card>
  );
}
