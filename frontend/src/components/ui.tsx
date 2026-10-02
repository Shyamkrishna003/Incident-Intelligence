/** Small shared building blocks. Kept plain: each is a styled native element. */

import { useId, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode } from "react";

import { describeError } from "../lib/api";

type ButtonVariant = "primary" | "secondary" | "danger";

const BUTTON_STYLES: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-ink hover:brightness-110",
  secondary: "border border-hairline bg-surface text-ink hover:bg-wash",
  danger: "border border-hairline bg-surface text-critical-ink hover:bg-wash",
};

export function Button({
  variant = "secondary",
  busy = false,
  className = "",
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; busy?: boolean }) {
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled === true || busy}
      aria-busy={busy}
      className={`inline-flex items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-60 ${BUTTON_STYLES[variant]} ${className}`}
    >
      {busy && <Spinner small />}
      {children}
    </button>
  );
}

export function TextField({
  label,
  hint,
  error,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string; error?: string }) {
  const id = useId();
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium text-ink">
        {label}
      </label>
      <input
        id={id}
        {...rest}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className="rounded-md border border-hairline bg-surface px-3 py-2 text-sm text-ink placeholder:text-muted"
      />
      {error ? (
        <p id={`${id}-error`} className="text-sm text-critical-ink">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-sm text-ink-2">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-hairline bg-surface p-5 ${className}`}>
      {children}
    </section>
  );
}

export function Spinner({ small = false }: { small?: boolean }) {
  const size = small ? "h-4 w-4" : "h-6 w-6";
  return (
    <span
      aria-hidden="true"
      className={`inline-block ${size} animate-spin rounded-full border-2 border-current border-t-transparent`}
    />
  );
}

export function LoadingState({ label }: { label: string }) {
  return (
    <div role="status" className="flex items-center gap-3 py-8 text-sm text-ink-2">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-hairline px-6 py-10 text-center">
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      {children && <div className="mx-auto mt-2 max-w-prose text-sm text-ink-2">{children}</div>}
    </div>
  );
}

/** An error with what happened and, when possible, a way to try again. */
export function ErrorState({
  title = "Something went wrong",
  error,
  onRetry,
}: {
  title?: string;
  error: unknown;
  onRetry?: () => void;
}) {
  return (
    <div role="alert" className="rounded-lg border border-hairline bg-surface px-5 py-4">
      <h2 className="flex items-center gap-2 text-sm font-semibold text-critical-ink">
        <span aria-hidden="true">⚠</span>
        {title}
      </h2>
      <p className="mt-1 text-sm text-ink-2">{describeError(error)}</p>
      {onRetry && (
        <Button className="mt-3" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

/** Inline form-level message, announced to screen readers when it appears. */
export function FormMessage({ tone, children }: { tone: "error" | "info"; children: ReactNode }) {
  if (!children) return null;
  return (
    <p
      role={tone === "error" ? "alert" : "status"}
      className={`text-sm ${tone === "error" ? "text-critical-ink" : "text-ink-2"}`}
    >
      {children}
    </p>
  );
}

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-xl font-semibold text-ink">{title}</h1>
      {children}
    </div>
  );
}
