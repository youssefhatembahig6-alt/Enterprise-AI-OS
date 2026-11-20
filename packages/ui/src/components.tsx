import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";
import { cn } from "./cn";

type ButtonVariant = "primary" | "secondary";

export function Button({
  variant = "primary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      className={cn(
        "inline-flex h-10 items-center justify-center rounded-md px-4 text-sm font-medium",
        "transition-colors focus-visible:outline-2 focus-visible:outline-offset-2",
        "focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-50",
        variant === "primary"
          ? "bg-primary text-primary-fg hover:opacity-90"
          : "border border-border bg-transparent hover:bg-muted",
        className,
      )}
      {...props}
    />
  );
}

export function Field({
  label,
  id,
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: string; id: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        className={cn(
          "h-10 rounded-md border border-border bg-bg px-3 text-sm",
          "focus-visible:outline-2 focus-visible:outline-ring",
          className,
        )}
        {...props}
      />
    </div>
  );
}

export function Card({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="w-full rounded-lg border border-border bg-card p-6 shadow-sm">
      {title ? <h2 className="mb-4 text-lg font-semibold">{title}</h2> : null}
      {children}
    </section>
  );
}

export type StatusTone = "ok" | "warn" | "error" | "unknown";

export function StatusPill({ tone, children }: { tone: StatusTone; children: ReactNode }) {
  const tones: Record<StatusTone, string> = {
    ok: "bg-ok-bg text-ok-fg",
    warn: "bg-warn-bg text-warn-fg",
    error: "bg-error-bg text-error-fg",
    unknown: "bg-muted text-muted-fg",
  };
  return (
    <span
      role="status"
      className={cn("inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium", tones[tone])}
    >
      {children}
    </span>
  );
}

export function Alert({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-error-fg/30 bg-error-bg px-3 py-2 text-sm text-error-fg"
    >
      {children}
    </div>
  );
}
