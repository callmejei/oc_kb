// The UI's only door: the gateway. Every call carries the user's bearer token; nothing here decides
// access (the gateway and the database do). Errors surface the gateway's own message.

const TOKEN_KEY = "kb.token";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  init: RequestInit = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(`/api/v1${path}`, {
    method,
    headers,
    body: payload,
    ...init,
  });
  if (res.status === 401 && token) {
    setToken(null);
    window.location.assign("/login");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg =
        typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") ?? "";
  return (
    type.includes("application/json") ? res.json() : res.arrayBuffer()
  ) as Promise<T>;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, b?: unknown) => request<T>("POST", p, b ?? {}),
  patch: <T>(p: string, b?: unknown) => request<T>("PATCH", p, b ?? {}),
  del: <T>(p: string) => request<T>("DELETE", p),
  bytes: (p: string) => request<ArrayBuffer>("GET", p),
  gov: {
    get: <T>(p: string) => request<T>("GET", `/governance${p}`),
    post: <T>(p: string, b?: unknown) =>
      request<T>("POST", `/governance${p}`, b ?? {}),
    patch: <T>(p: string, b?: unknown) =>
      request<T>("PATCH", `/governance${p}`, b ?? {}),
  },
};

/** Upload with progress (fetch has no upload progress): XHR, same token, same door. */
export function uploadFile(
  space: string,
  file: File,
  asNewVersionOf: string | null,
  onProgress: (fraction: number) => void,
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const q = asNewVersionOf
      ? `?as_new_version_of=${encodeURIComponent(asNewVersionOf)}`
      : "";
    xhr.open("POST", `/api/v1/uploads/${encodeURIComponent(space)}${q}`);
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (e) =>
      e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let j: { detail?: unknown } = {};
      try {
        j = JSON.parse(xhr.responseText);
      } catch {
        /* empty */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(j as UploadResult);
      else
        reject(
          new ApiError(
            xhr.status,
            typeof j.detail === "string" ? j.detail : xhr.statusText,
          ),
        );
    };
    xhr.onerror = () => reject(new ApiError(0, "network error"));
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}

// ------------------------------------------------------------------------------------------- types
export interface Me {
  user_id: string;
  display_name: string;
  bu: string;
  spaces: string[];
  maker_bus: string[];
  checker_bus: string[];
  ops: boolean;
}

export interface Citation {
  n: number | string;
  kind: "document" | "data";
  chunk_id?: string | null;
  doc_id?: string | null;
  version_id?: string | null;
  version_no?: number | null;
  title?: string | null;
  section?: string | null;
  page?: number | null;
  page_end?: number | null;
  tables?: string[] | null;
  sql?: string | null;
  row_count?: number | null;
  metric?: {
    id: string;
    label: string;
    revision: number;
    group_by: string[];
  } | null;
}

export interface AskResponse {
  answer: string;
  citations: Citation[];
  grounded: boolean;
  route: string;
  notes: string[];
  query_id: string;
  latency_ms: number;
}

export interface UploadResult {
  doc_id: string;
  version_id: string;
  version_no: number;
  status: string;
  duplicate_of: string | null;
  sha256: string;
  size_bytes: number;
}

export const PROBLEMS = [
  "WRONG_ANSWER",
  "INCOMPLETE",
  "WRONG_CITATION",
  "OUTDATED",
  "TABLE_BROKEN",
  "OCR_ERROR",
  "READING_ORDER",
  "MISSING_TEXT",
  "WRONG_HEADING",
  "OTHER",
] as const;
export type Problem = (typeof PROBLEMS)[number];

export function label(s: string): string {
  return s
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/^./, (c) => c.toUpperCase());
}
