import type { ReactNode } from "react";
import { Icon, type IconName } from "../shell/Icon";

export function ErrorBanner({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="alert alert--error" role="alert">
      <span>{error.message}</span>
      {onRetry && (
        <button type="button" className="button button--ghost button--small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export interface EmptyProps {
  title: string;
  /** What this is for and what the user can do next. */
  description?: ReactNode;
  /** The next step, usually one primary button or link. */
  action?: ReactNode;
  icon?: IconName;
}

/** Never just "nothing here": say what the page is for and offer the next step. */
export function EmptyState({ title, description, action, icon = "sparkles", children }: EmptyProps & { children?: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty__icon" aria-hidden="true">
        <Icon name={icon} size={20} />
      </span>
      <p className="empty__title">{title}</p>
      {description && <p className="empty__body">{description}</p>}
      {children && <div className="empty__body">{children}</div>}
      {action && <div className="empty__action">{action}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}
