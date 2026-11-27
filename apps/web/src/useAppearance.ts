import { useEffect, useState } from "react";
import { applyAppearance, loadAppearance, saveAppearance, type Appearance } from "./appearance";

/** App-wide appearance state: applied to <html>, remembered on this device, and
 * re-applied when the device switches light/dark while the mode is "system". */
export function useAppearance(): [Appearance, (next: Appearance) => void] {
  const [appearance, setAppearance] = useState<Appearance>(loadAppearance);
  useEffect(() => {
    applyAppearance(appearance);
    saveAppearance(appearance);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => appearance.mode === "system" && applyAppearance(appearance);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [appearance]);
  return [appearance, setAppearance];
}

