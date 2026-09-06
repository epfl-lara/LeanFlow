/**
 * Webview state: the host's snapshot plus purely local view state.
 *
 * The host snapshot is authoritative and replaced wholesale on every push. View
 * state (selected tab, filters, the in-progress launch form) belongs to the
 * webview and is persisted so hiding the panel does not discard a half-filled
 * form.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";

import type {
  ActivityEvent,
  AppState,
  HostMessage,
  LaunchPlanPreview,
  LaunchRequest,
  ProfileDiffRow,
} from "../src/core/types";
import { launchRequestForViewState } from "../src/core/launch";
import { trackedRunForLiveStatus } from "../src/core/runSelection";
import { emptyLaunchRequest } from "../src/core/types";
import type { ProverSnapshot } from "../src/core/prover";
import { loadViewState, post, saveViewState } from "./vscodeApi";

export type TabId = "launch" | "live" | "logs" | "knobs" | "sweeps";

export interface ViewState {
  tab: TabId;
  form: LaunchRequest;
  logFilter: string[];
  logSearch: string;
  logRaw: boolean;
  logAgent: string;
  knobSearch: string;
  knobAblatableOnly: boolean;
  knobProfile: string;
  collapsedGroups: string[];
  diffLeft: string;
  diffRight: string;
}

const DEFAULT_VIEW: ViewState = {
  tab: "launch",
  form: emptyLaunchRequest(),
  logFilter: [],
  logSearch: "",
  logRaw: false,
  logAgent: "",
  knobSearch: "",
  knobAblatableOnly: false,
  knobProfile: "",
  collapsedGroups: [],
  diffLeft: "default",
  diffRight: "research",
};

function viewStateForPersistence(view: ViewState): ViewState {
  return { ...view, form: launchRequestForViewState(view.form) };
}

function persistViewState(view: ViewState): void {
  saveViewState(viewStateForPersistence(view));
}

interface Toast {
  id: number;
  level: "info" | "warn" | "error";
  message: string;
}

interface Store {
  app: AppState | null;
  view: ViewState;
  events: Record<string, ActivityEvent[]>;
  runLog: string;
  proverStates: Record<string, { snapshot: ProverSnapshot | null; error: string }>;
  preview: { plan: LaunchPlanPreview | null; error: string; loading: boolean };
  diff: { rows: ProfileDiffRow[]; error: string; loading: boolean };
  toasts: Toast[];
}

type Action =
  | { type: "host"; message: HostMessage }
  | { type: "view"; patch: Partial<ViewState> }
  | { type: "form"; patch: Partial<LaunchRequest> }
  | { type: "previewLoading" }
  | { type: "diffLoading" }
  | { type: "dismissToast"; id: number };

const initialView = (() => {
  const restored = loadViewState(DEFAULT_VIEW);
  const sanitized = viewStateForPersistence(restored);
  if (JSON.stringify(restored) !== JSON.stringify(sanitized)) {
    // Purge plaintext prompts or credential-shaped values written by older
    // versions. Prompts now live only in this document's React state and are
    // intentionally lost when the webview is recreated.
    persistViewState(sanitized);
  }
  return sanitized;
})();

const INITIAL: Store = {
  app: null,
  view: initialView,
  events: {},
  runLog: "",
  proverStates: {},
  preview: { plan: null, error: "", loading: false },
  diff: { rows: [], error: "", loading: false },
  toasts: [],
};

let toastId = 0;

function reduce(state: Store, action: Action): Store {
  switch (action.type) {
    case "view": {
      const view = { ...state.view, ...action.patch };
      persistViewState(view);
      return { ...state, view };
    }
    case "form": {
      const view = { ...state.view, form: { ...state.view.form, ...action.patch } };
      persistViewState(view);
      return { ...state, view };
    }
    case "previewLoading":
      return { ...state, preview: { ...state.preview, loading: true } };
    case "diffLoading":
      return { ...state, diff: { ...state.diff, loading: true } };
    case "dismissToast":
      return { ...state, toasts: state.toasts.filter((toast) => toast.id !== action.id) };
    case "host":
      return reduceHost(state, action.message);
    default:
      return state;
  }
}

function reduceHost(state: Store, message: HostMessage): Store {
  switch (message.type) {
    case "state":
      return { ...state, app: message.state };
    case "proverState":
      return { ...state, proverStates: { ...state.proverStates,
        [message.runId]: { snapshot: message.snapshot, error: message.error } } };
    case "events": {
      const existing = message.reset ? [] : (state.events[message.runId] ?? []);
      // The host already de-duplicates by cursor, but a reset push overlapping
      // an incremental one would otherwise double entries.
      const seen = new Set(existing.map((event) => event.event_id));
      const merged = existing.concat(
        message.events.filter((event) => !seen.has(event.event_id)),
      );
      return { ...state, events: { ...state.events, [message.runId]: merged } };
    }
    case "preview":
      return {
        ...state,
        preview: { plan: message.plan, error: message.error, loading: false },
      };
    case "diff":
      return { ...state, diff: { rows: message.rows, error: message.error, loading: false } };
    case "runLog":
      return { ...state, runLog: message.text };
    case "targetPicked": {
      const view = { ...state.view, form: { ...state.view.form, target: message.path } };
      persistViewState(view);
      return { ...state, view };
    }
    case "notify":
      return {
        ...state,
        toasts: [
          ...state.toasts.slice(-3),
          { id: (toastId += 1), level: message.level, message: message.message },
        ],
      };
    default:
      return state;
  }
}

interface StoreContextValue extends Store {
  setView: (patch: Partial<ViewState>) => void;
  setForm: (patch: Partial<LaunchRequest>) => void;
  requestPreview: (request: LaunchRequest) => void;
  requestDiff: (left: string, right: string) => void;
  dismissToast: (id: number) => void;
}

const StoreContext = createContext<StoreContextValue | null>(null);

export function StoreProvider(props: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reduce, INITIAL);
  const previewSeq = useRef(0);
  const diffSeq = useRef(0);

  useEffect(() => {
    const onMessage = (event: MessageEvent<HostMessage>) => {
      dispatch({ type: "host", message: event.data });
    };
    window.addEventListener("message", onMessage);
    post({ type: "ready" });
    return () => window.removeEventListener("message", onMessage);
  }, []);

  const setView = useCallback((patch: Partial<ViewState>) => {
    dispatch({ type: "view", patch });
  }, []);

  const setForm = useCallback((patch: Partial<LaunchRequest>) => {
    dispatch({ type: "form", patch });
  }, []);

  const requestPreview = useCallback((request: LaunchRequest) => {
    previewSeq.current += 1;
    dispatch({ type: "previewLoading" });
    post({ type: "preview", requestId: String(previewSeq.current), request });
  }, []);

  const requestDiff = useCallback((left: string, right: string) => {
    diffSeq.current += 1;
    dispatch({ type: "diffLoading" });
    post({ type: "diffProfiles", requestId: String(diffSeq.current), left, right });
  }, []);

  const dismissToast = useCallback((id: number) => {
    dispatch({ type: "dismissToast", id });
  }, []);

  const value = useMemo<StoreContextValue>(
    () => ({ ...state, setView, setForm, requestPreview, requestDiff, dismissToast }),
    [state, setView, setForm, requestPreview, requestDiff, dismissToast],
  );

  return <StoreContext.Provider value={value}>{props.children}</StoreContext.Provider>;
}

export function useStore(): StoreContextValue {
  const value = useContext(StoreContext);
  if (!value) {
    throw new Error("useStore must be used inside StoreProvider");
  }
  return value;
}

/** Return only an explicitly selected or active tracked run, never a stale default. */
export function useSelectedRun() {
  const { app } = useStore();
  if (!app) {
    return null;
  }
  return trackedRunForLiveStatus(app.runs, app.selectedRunId);
}
