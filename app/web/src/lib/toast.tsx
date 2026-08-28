"use client";

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

/**
 * Toasts are for things you cannot already see.
 *
 * A "saved" toast on a form whose value visibly changed is noise, so this is
 * used for failures and for the results of work that finished somewhere else.
 * They stack at a corner and never shift layout.
 */
type Kind = "ok" | "err" | "info";

/** A toast that is worth acting on carries the action, rather than making the
 *  reader go and find it -- an update announcement opens the updater. */
export interface ToastAction {
  label: string;
  run: () => void;
}

export interface ToastOptions {
  action?: ToastAction;
  /** Milliseconds on screen. Longer for anything the reader must decide about. */
  duration?: number;
}

interface Toast {
  id: number;
  kind: Kind;
  message: string;
  action?: ToastAction;
}

interface ToastApi {
  push: (kind: Kind, message: string, options?: ToastOptions) => void;
  /** Run an async action; failures become an error toast. Returns success. */
  guard: (action: () => Promise<unknown>, okMessage?: string) => Promise<boolean>;
}

const ToastContext = createContext<ToastApi>({ push: () => {}, guard: async () => false });
let seq = 1;
const DEFAULT_MS = 4600;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const push = useCallback(
    (kind: Kind, message: string, options?: ToastOptions) => {
      const id = seq++;
      setToasts((t) => [...t, { id, kind, message, action: options?.action }]);
      window.setTimeout(() => dismiss(id), options?.duration ?? DEFAULT_MS);
    },
    [dismiss],
  );

  const guard = useCallback(
    async (action: () => Promise<unknown>, okMessage?: string) => {
      try {
        await action();
        if (okMessage) push("ok", okMessage);
        return true;
      } catch (e) {
        push("err", e instanceof Error ? e.message : String(e));
        return false;
      }
    },
    [push],
  );

  return (
    <ToastContext.Provider value={{ push, guard }}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast--${t.kind}`}>
            <span className="toast__mark" aria-hidden="true" />
            <span className="toast__body">{t.message}</span>
            {t.action && (
              <button
                className="toast__action"
                onClick={() => {
                  t.action?.run();
                  dismiss(t.id);
                }}
              >
                {t.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);
