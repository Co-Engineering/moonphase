import { accessToken } from './supabase'

const API_URL: string = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8471'

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
  environment: string
  repo_url: string | null
  container_name: string | null
  status: 'creating' | 'running' | 'stopped' | 'error'
  status_detail: string | null
  preview_port: number | null
  preview_url: string | null
  created_at: string
  activity: ActivityState
  activity_detail: string | null
  activity_at: string | null
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
  activity: ActivityState
  activity_detail: string | null
  /** Devices currently viewing this session, live from tmux. */
  attached_clients: number
  alive: boolean
}

export interface HarnessInfo {
  kind: string
  display_name: string
  supported_auth_modes: string[]
  available: boolean
  /** Signed in, so projects using it will actually work. */
  configured: boolean
  login_supported: boolean
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
  environment: string
  repo_url?: string | null
}

export interface Environment {
  key: string
  display_name: string
  description: string
  base_image: string
  setup_script: string | null
  /** Ships with Moonphase; cannot be deleted, only shadowed. */
  builtin: boolean
  project_count: number
}

export interface EnvironmentInput {
  key: string
  display_name: string
  description?: string | null
  base_image: string
  setup_script?: string | null
}

export interface WorkspaceProfile {
  org_id: string
  claude_settings_json: string | null
  claude_md: string | null
  mcp_json: string | null
  env_vars: Record<string, string>
  git_user_name: string | null
  git_user_email: string | null
  harness_connected: boolean
  harness_auth_mode: string | null
  github_connected: boolean
  github_account: string | null
  github_scopes: string | null
}

export interface WorkspaceProfileInput {
  claude_settings_json?: string | null
  claude_md?: string | null
  mcp_json?: string | null
  env_vars: Record<string, string>
  git_user_name?: string | null
  git_user_email?: string | null
}

export interface HarnessLogin {
  session_id: string
  state: 'starting' | 'awaiting_code' | 'verifying' | 'complete' | 'error'
  url: string | null
  detail: string | null
  /** Live terminal output, shown while verifying and on failure. */
  pane: string | null
}

export interface GitHubDevice {
  session_id: string
  state: 'awaiting_authorization' | 'complete' | 'error'
  user_code: string | null
  verification_uri: string | null
  interval: number
  detail: string | null
  account: string | null
}

export type ActivityState =
  | 'unknown'
  | 'working'
  | 'awaiting_input'
  | 'idle'
  | 'stopped'

export interface PushStatus {
  configured: boolean
  public_key: string | null
  subscribed: boolean
}

export interface PushSubscriptionInput {
  endpoint: string
  p256dh: string
  auth: string
  user_agent?: string
}

export interface DiffLine {
  /** ' ' context, '+' added, '-' removed, '@' hunk header. */
  sign: string
  text: string
}

export interface FeedEvent {
  id: string
  kind: 'user' | 'assistant' | 'thinking' | 'tool' | 'result' | 'system'
  text: string
  at: string | null
  tool: string | null
  ok: boolean | null
  /** Subagent traffic, dimmed rather than hidden. */
  sidechain: boolean
  /** Present on Edit and Write, so a change can be judged on a phone. */
  diff: DiffLine[] | null
  added: number
  removed: number
  truncated: boolean
}

export interface Prompt {
  question: string
  options: { key: string; label: string }[]
}

export interface FeedPage {
  events: FeedEvent[]
  cursor: string
  available: boolean
  activity: ActivityState
  prompt: Prompt | null
}

export interface DetectedPort {
  port: number
  bind: string
  process: string | null
  loopback_only: boolean
  shared: boolean
  url: string | null
}

// --- endpoints --------------------------------------------------------------

export const api = {
  organizations: () => request<Organization[]>('/api/organizations'),
  harnesses: () => request<HarnessInfo[]>('/api/harnesses'),
  environments: () => request<Environment[]>('/api/environments'),
  saveEnvironment: (input: EnvironmentInput) =>
    request<Environment>('/api/environments', {
      method: 'PUT',
      body: JSON.stringify(input),
    }),
  deleteEnvironment: (key: string) =>
    request<void>(`/api/environments/${key}`, { method: 'DELETE' }),

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
  startSession: (projectId: string, restart = false, session?: string) =>
    request<Session>(`/api/projects/${projectId}/sessions/start`, {
      method: 'POST',
      body: JSON.stringify({ restart, session: session ?? null }),
    }),
  createSession: (projectId: string, name: string) =>
    request<Session>(`/api/projects/${projectId}/sessions`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  deleteSession: (projectId: string, name: string) =>
    request<void>(`/api/projects/${projectId}/sessions/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  detachClients: (projectId: string, name: string) =>
    request<{ detached: number }>(
      `/api/projects/${projectId}/sessions/${encodeURIComponent(name)}/detach-clients`,
      { method: 'POST' },
    ),
  sendKeys: (projectId: string, keys: string, enter = true) =>
    request<void>(`/api/projects/${projectId}/sessions/keys`, {
      method: 'POST',
      body: JSON.stringify({ keys, enter }),
    }),

  // --- global profile -------------------------------------------------------
  profile: () => request<WorkspaceProfile>('/api/profile'),
  saveProfile: (input: WorkspaceProfileInput) =>
    request<WorkspaceProfile>('/api/profile', {
      method: 'PUT',
      body: JSON.stringify(input),
    }),

  startHarnessLogin: (harness: HarnessKind = 'claude_code') =>
    request<HarnessLogin>('/api/profile/harness/login/start', {
      method: 'POST',
      body: JSON.stringify({ harness }),
    }),
  pollHarnessLogin: (sessionId: string) =>
    request<HarnessLogin>(`/api/profile/harness/login/${sessionId}`),
  submitHarnessCode: (sessionId: string, code: string) =>
    request<HarnessLogin>('/api/profile/harness/login/code', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, code }),
    }),
  setHarnessApiKey: (apiKey: string, harness: HarnessKind = 'claude_code') =>
    request<WorkspaceProfile>('/api/profile/harness/api-key', {
      method: 'POST',
      body: JSON.stringify({ api_key: apiKey, harness }),
    }),
  disconnectHarness: () =>
    request<WorkspaceProfile>('/api/profile/harness', { method: 'DELETE' }),

  githubAvailable: () => request<{ device_flow: boolean }>('/api/profile/github/available'),
  startGitHubDevice: () =>
    request<GitHubDevice>('/api/profile/github/device/start', {
      method: 'POST',
      body: JSON.stringify({}),
    }),
  pollGitHubDevice: (sessionId: string) =>
    request<GitHubDevice>(`/api/profile/github/device/${sessionId}`),
  setGitHubToken: (token: string) =>
    request<WorkspaceProfile>('/api/profile/github/token', {
      method: 'POST',
      body: JSON.stringify({ token }),
    }),
  disconnectGitHub: () =>
    request<WorkspaceProfile>('/api/profile/github', { method: 'DELETE' }),

  // --- notifications --------------------------------------------------------
  pushStatus: () => request<PushStatus>('/api/notifications'),
  subscribePush: (input: PushSubscriptionInput) =>
    request<PushStatus>('/api/notifications/subscribe', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  unsubscribePush: (input: PushSubscriptionInput) =>
    request<void>('/api/notifications/unsubscribe', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  testPush: () =>
    request<{ delivered: number; subscriptions: number }>('/api/notifications/test', {
      method: 'POST',
    }),

  // --- phone feed -----------------------------------------------------------
  feed: (projectId: string, session?: string, cursor?: string) => {
    const params = new URLSearchParams()
    if (session) params.set('session', session)
    if (cursor) params.set('cursor', cursor)
    const query = params.toString()
    return request<FeedPage>(`/api/projects/${projectId}/feed${query ? `?${query}` : ''}`)
  },
  answerFeed: (projectId: string, key: string, session?: string) =>
    request<void>(
      `/api/projects/${projectId}/feed/answer${session ? `?session=${encodeURIComponent(session)}` : ''}`,
      { method: 'POST', body: JSON.stringify({ key }) },
    ),

  // --- previews -------------------------------------------------------------
  ports: (projectId: string) => request<DetectedPort[]>(`/api/projects/${projectId}/ports`),
  sharePort: (projectId: string, port: number) =>
    request<DetectedPort>(`/api/projects/${projectId}/ports/${port}/share`, {
      method: 'POST',
    }),
  unsharePort: (projectId: string, port: number) =>
    request<void>(`/api/projects/${projectId}/ports/${port}/share`, { method: 'DELETE' }),
}

/** Live feed socket. Falls back to `api.feed` polling if this cannot open. */
export async function feedUrl(projectId: string, session?: string) {
  const token = await accessToken()
  const base = API_URL.replace(/^http/, 'ws')
  const params = new URLSearchParams({ token: token ?? '' })
  if (session) params.set('session', session)
  return `${base}/ws/projects/${projectId}/feed?${params}`
}

export async function terminalUrl(
  projectId: string,
  cols: number,
  rows: number,
  session?: string,
) {
  const token = await accessToken()
  const base = API_URL.replace(/^http/, 'ws')
  const params = new URLSearchParams({
    token: token ?? '',
    cols: String(cols),
    rows: String(rows),
    ...(session ? { session } : {}),
  })
  return `${base}/ws/projects/${projectId}/terminal?${params}`
}
