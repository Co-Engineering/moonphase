import { accessToken } from './supabase'

const API_URL: string = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8787'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await accessToken()
  if (!token) throw new ApiError(401, 'Not signed in.')

  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...(init.headers ?? {}),
    },
  })

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const body = text ? JSON.parse(text) : null

  if (!response.ok) {
    // FastAPI puts the message in `detail`, but validation errors make it an
    // array of objects — flatten so the UI always has a string to show.
    const detail = body?.detail
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join('; ')
          : `Request failed with status ${response.status}`
    throw new ApiError(response.status, message)
  }

  return body as T
}

// --- types ------------------------------------------------------------------

export type SshAuthMode = 'password_bootstrap' | 'managed_key' | 'provided_key'
export type HarnessKind = 'claude_code' | 'opencode'

export interface Organization {
  id: string
  name: string
  slug: string
  is_personal: boolean
  role: string | null
  created_at: string
}

export interface Server {
  id: string
  org_id: string
  name: string
  host: string
  port: number
  ssh_user: string
  ssh_auth_mode: SshAuthMode
  status: 'pending' | 'bootstrapping' | 'online' | 'offline' | 'error'
  status_detail: string | null
  host_key_fingerprint: string | null
  docker_version: string | null
  managed_public_key: string | null
  last_seen_at: string | null
  created_at: string
  project_count: number
}

export interface ServerBootstrap {
  server: Server
  status: 'online' | 'error' | 'awaiting_key_install'
  detail: string | null
  public_key_to_install: string | null
}

export interface Project {
  id: string
  org_id: string
  server_id: string
  server_name: string | null
  name: string
  slug: string
  harness: HarnessKind
  repo_url: string | null
  container_name: string | null
  status: 'creating' | 'running' | 'stopped' | 'error'
  status_detail: string | null
  preview_port: number | null
  preview_url: string | null
  created_at: string
}

export interface Session {
  id: string
  project_id: string
  tmux_session: string
  harness: HarnessKind
  state: string
  started_at: string | null
  last_attached_at: string | null
  transcript_path: string | null
}

export interface HarnessInfo {
  kind: string
  display_name: string
  supported_auth_modes: string[]
  available: boolean
}

export interface CreateServerInput {
  name: string
  host: string
  port: number
  ssh_user: string
  auth_mode: SshAuthMode
  password?: string
  private_key?: string
  passphrase?: string
  auto_install_docker: boolean
  org_id?: string
}

export interface CreateProjectInput {
  server_id: string
  name: string
  harness: HarnessKind
  repo_url?: string | null
  harness_auth_mode?: 'api_key' | 'oauth' | null
  api_key?: string | null
  preview_port?: number | null
}

// --- endpoints --------------------------------------------------------------

export const api = {
  organizations: () => request<Organization[]>('/api/organizations'),
  harnesses: () => request<HarnessInfo[]>('/api/harnesses'),

  servers: () => request<Server[]>('/api/servers'),
  server: (id: string) => request<Server>(`/api/servers/${id}`),
  createServer: (input: CreateServerInput) =>
    request<ServerBootstrap>('/api/servers', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  bootstrapServer: (id: string) =>
    request<ServerBootstrap>(`/api/servers/${id}/bootstrap`, { method: 'POST' }),
  testServer: (id: string) => request<Server>(`/api/servers/${id}/test`, { method: 'POST' }),
  deleteServer: (id: string) =>
    request<void>(`/api/servers/${id}`, { method: 'DELETE' }),

  projects: (serverId?: string) =>
    request<Project[]>(`/api/projects${serverId ? `?server_id=${serverId}` : ''}`),
  project: (id: string) => request<Project>(`/api/projects/${id}`),
  createProject: (input: CreateProjectInput) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(input) }),
  startProject: (id: string) =>
    request<Project>(`/api/projects/${id}/start`, { method: 'POST' }),
  stopProject: (id: string) =>
    request<Project>(`/api/projects/${id}/stop`, { method: 'POST' }),
  deleteProject: (id: string, deleteVolumes = false) =>
    request<void>(`/api/projects/${id}?delete_volumes=${deleteVolumes}`, {
      method: 'DELETE',
    }),

  sessions: (projectId: string) => request<Session[]>(`/api/projects/${projectId}/sessions`),
  startSession: (projectId: string, restart = false) =>
    request<Session>(`/api/projects/${projectId}/sessions/start`, {
      method: 'POST',
      body: JSON.stringify({ restart }),
    }),
  sendKeys: (projectId: string, keys: string, enter = true) =>
    request<void>(`/api/projects/${projectId}/sessions/keys`, {
      method: 'POST',
      body: JSON.stringify({ keys, enter }),
    }),
}

export async function terminalUrl(projectId: string, cols: number, rows: number) {
  const token = await accessToken()
  const base = API_URL.replace(/^http/, 'ws')
  const params = new URLSearchParams({
    token: token ?? '',
    cols: String(cols),
    rows: String(rows),
  })
  return `${base}/ws/projects/${projectId}/terminal?${params}`
}
