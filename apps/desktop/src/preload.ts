/**
 * The only bridge between the shared React app and the desktop main process. The
 * renderer runs sandboxed with context isolation; it gets these few calls and nothing
 * from Node. The session token passes to the main process in memory only.
 */

import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

export interface DesktopFolder {
  folderId: string;
  sourceId: string | null;
  name: string;
  root: string;
  pendingRemoval?: boolean;
  removalError?: string | null;
  status: unknown;
}

const bridge = {
  version: 1,
  setSession: (token: string | null): Promise<void> => ipcRenderer.invoke("eaios:session", token),
  chooseFolder: (): Promise<string | null> => ipcRenderer.invoke("eaios:choose-folder"),
  addFolder: (root: string): Promise<DesktopFolder> => ipcRenderer.invoke("eaios:add-folder", root),
  removeFolder: (folderId: string, fromServer = false): Promise<DesktopFolder | null> =>
    ipcRenderer.invoke("eaios:remove-folder", folderId, fromServer),
  listFolders: (): Promise<DesktopFolder[]> => ipcRenderer.invoke("eaios:list-folders"),
  syncNow: (folderId: string): Promise<void> => ipcRenderer.invoke("eaios:sync-now", folderId),
  onStatus: (listener: (status: unknown) => void): (() => void) => {
    const handler = (_: IpcRendererEvent, status: unknown) => listener(status);
    ipcRenderer.on("eaios:status", handler);
    return () => ipcRenderer.removeListener("eaios:status", handler);
  },
};

contextBridge.exposeInMainWorld("eaiosDesktop", bridge);
