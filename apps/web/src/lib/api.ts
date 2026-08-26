import { currentHost } from './host'
import { accessToken } from './supabase'

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

  // FormData bodies (image uploads) set their own multipart boundary; a fixed
  // 'application/json' header would silently drop the boundary and break the
  // parse on the way in.
  const isForm = init.body instanceof FormData

  const response = await fetch(`${currentHost()}${path}`, {
    ...init,
    headers: {
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
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
export type HarnessKind = 'claude_code' | 'opencode' | 'pydantic_ai'

/** What you grant someone. */
export type ShareRole = 'viewer' | 'collaborator'

/**
 * What you end up with. Decided by the database, not the client.
 *
 * `host` is the odd one: you own the machine a project runs on but not the
 * project, so you can see that it exists and reclaim it, and nothing else.
 */
export type Access = 'admin' | 'write' | 'read' | 'host'

export interface Share {
  id: string
  email: string
  role: ShareRole
  /** False until the invitee has signed up and the grant has been claimed. */
  accepted: boolean
  created_at: string
  is_you: boolean
}

export const canControl = (access: Access) => access === 'admin' || access === 'write'
export const canObserve = (access: Access) =>
  access === 'admin' || access === 'write' || access === 'read'

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
  access: Access
  /** Reached through a share rather than your own organization. */
  shared: boolean
  share_count: number
}

export interface ServerBootstrap {
  server: Server
  /**
   * 'bootstrapping' is the answer to creating one: the work happens in the
   * background and the client watches the server's own status, because a key
   * install and a Docker install take longer than a request should be held
   * open for.
   */
  status: 'bootstrapping' | 'online' | 'error' | 'awaiting_key_install' | 'pending' | 'offline'
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
  /** When the activity above was last confirmed. Null means never. */
  checked_at: string | null
  access: Access
  shared: boolean
  share_count: number
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
  activity_at: string | null
  /** When the activity above was last confirmed. Null means never. */
  checked_at: string | null
  /**
   * Who this session runs as. A session is one person's — their Claude
   * account, their git identity, their branch — so only its owner can type
   * into it. Everyone else in the project can watch.
   */
  user_id: string | null
  owner: string | null
  is_mine: boolean
  /** Present when listed across projects, so it can be named on its own. */
  project_name: string | null
  /** The git worktree this session works in, and the branch it is on. */
  workdir: string
  branch: string | null
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

export interface GitHubRepo {
  full_name: string
  clone_url: string
  private: boolean
  description: string | null
  pushed_at: string | null
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

/**
 * How long a reported activity stays believable.
 *
 * The monitor sweeps every 20 seconds, so anything it confirmed within a
 * couple of sweeps is current. Past that it has not been able to look, and the
 * last thing it saw is a guess — showing it as fact is how a finished agent
 * goes on displaying a confident "working" overnight.
 */
export const ACTIVITY_STALE_AFTER_MS = 120_000

export function liveActivity(item: {
  activity: ActivityState
  checked_at?: string | null
}): ActivityState {
  if (!item.checked_at) return 'unknown'
  if (Date.now() - Date.parse(item.checked_at) > ACTIVITY_STALE_AFTER_MS) return 'unknown'
  return item.activity
}

export function checkedAgo(item: { checked_at?: string | null }): string {
  if (!item.checked_at) return 'never checked'
  const seconds = Math.round((Date.now() - Date.parse(item.checked_at)) / 1000)
  if (seconds < 90) return `checked ${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 90) return `last checked ${minutes} min ago`
  return `last checked ${Math.round(minutes / 60)}h ago`
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

export interface FeedUpload {
  /** Where the image landed inside the session — fold it into the next message. */
  path: string
}

export interface PreviewService {
  port: number
  /** 'page' serves HTML and is what "open the app" means; 'api' answers JSON. */
  kind: 'page' | 'api' | 'unknown'
  /** The page's <title> — a better label than a port number. */
  title: string | null
  process: string | null
}

export interface Preview {
  proxy_host: string
  /** Ordered by what you most likely meant to open. */
  services: PreviewService[]
  container: string
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
  /** The display name only — see the endpoint for why nothing else. */
  renameServer: (id: string, name: string) =>
    request<Server>(`/api/servers/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    }),
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
  renameProject: (id: string, name: string) =>
    request<Project>(`/api/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    }),
  deleteProject: (id: string, deleteVolumes = false) =>
    request<void>(`/api/projects/${id}?delete_volumes=${deleteVolumes}`, {
      method: 'DELETE',
    }),

  /**
   * Sessions in one project. `live` costs an SSH round trip for the attached
   * device count, so it is off unless you are actually looking at a session.
   */
  sessions: (projectId: string, live = false) =>
    request<Session[]>(`/api/projects/${projectId}/sessions${live ? '?live=true' : ''}`),
  /** Every session the caller can see, in one query. For the sidebar. */
  allSessions: () => request<Session[]>('/api/sessions'),
  /**
   * `resume` asks the harness to reopen its previous conversation instead of
   * starting a new one — what makes a session survive its container being
   * restarted under it, rather than coming back to an empty prompt.
   */
  startSession: (projectId: string, restart = false, session?: string, resume = false) =>
    request<Session>(`/api/projects/${projectId}/sessions/start`, {
      method: 'POST',
      body: JSON.stringify({ restart, session: session ?? null, resume }),
    }),
  /**
   * Omit the name and it is derived from you, which is the useful default.
   * Omit the branch and the worktree starts from whatever `/workspace` is on.
   */
  createSession: (projectId: string, name?: string, branch?: string) =>
    request<Session>(`/api/projects/${projectId}/sessions`, {
      method: 'POST',
      body: JSON.stringify({ name: name ?? null, branch: branch ?? null }),
    }),
  /** Branches worth offering as a new session's starting point. */
  branches: (projectId: string) => request<string[]>(`/api/projects/${projectId}/branches`),
  deleteSession: (projectId: string, name: string) =>
    request<void>(`/api/projects/${projectId}/sessions/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  detachClients: (projectId: string, name: string) =>
    request<{ detached: number }>(
      `/api/projects/${projectId}/sessions/${encodeURIComponent(name)}/detach-clients`,
      { method: 'POST' },
    ),
  sendKeys: (projectId: string, keys: string, enter = true, session?: string) =>
    request<void>(
      `/api/projects/${projectId}/sessions/keys${
        session ? `?session=${encodeURIComponent(session)}` : ''
      }`,
      { method: 'POST', body: JSON.stringify({ keys, enter }) },
    ),

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
  githubRepos: () => request<GitHubRepo[]>('/api/profile/github/repos'),

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
  /** Drops an image into the session so a message can point at it by path. */
  uploadFeedImage: (projectId: string, file: File, session?: string) => {
    const form = new FormData()
    form.append('file', file)
    return request<FeedUpload>(
      `/api/projects/${projectId}/feed/upload${session ? `?session=${encodeURIComponent(session)}` : ''}`,
      { method: 'POST', body: form },
    )
  },

  // --- sharing --------------------------------------------------------------
  shares: (kind: 'servers' | 'projects', id: string) =>
    request<Share[]>(`/api/${kind}/${id}/shares`),
  addShare: (kind: 'servers' | 'projects', id: string, email: string, role: ShareRole) =>
    request<Share>(`/api/${kind}/${id}/shares`, {
      method: 'POST',
      body: JSON.stringify({ email, role }),
    }),
  setShareRole: (
    kind: 'servers' | 'projects',
    id: string,
    shareId: string,
    role: ShareRole,
  ) =>
    request<Share>(`/api/${kind}/${id}/shares/${shareId}`, {
      method: 'PATCH',
      body: JSON.stringify({ role }),
    }),
  removeShare: (kind: 'servers' | 'projects', id: string, shareId: string) =>
    request<void>(`/api/${kind}/${id}/shares/${shareId}`, { method: 'DELETE' }),

  // --- previews -------------------------------------------------------------
  openPreview: (projectId: string) =>
    request<Preview>(`/api/projects/${projectId}/preview`, { method: 'POST' }),
  closePreview: (projectId: string) =>
    request<void>(`/api/projects/${projectId}/preview`, { method: 'DELETE' }),
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
  // A ticket rather than the access token, for the same reason the terminal
  // uses one: a query parameter lands in every proxy's access log, and this
  // socket opens on every project you look at — so it was the token that
  // leaked most often, not the terminal's. See tickets.py.
  const { ticket } = await request<{ ticket: string }>(
    `/api/projects/${projectId}/sessions/ticket`,
    { method: 'POST' },
  )
  const base = currentHost().replace(/^http/, 'ws')
  const params = new URLSearchParams({ ticket })
  if (session) params.set('session', session)
  return `${base}/ws/projects/${projectId}/feed?${params}`
}

export async function terminalUrl(
  projectId: string,
  cols: number,
  rows: number,
  session?: string,
) {
  // A short-lived, single-use ticket rather than the real access token:
  // query parameters land in proxy/load-balancer access logs, and a ticket
  // minted just for this connection is worthless there. See tickets.py.
  const { ticket } = await request<{ ticket: string }>(
    `/api/projects/${projectId}/sessions/ticket`,
    { method: 'POST' },
  )
  const base = currentHost().replace(/^http/, 'ws')
  const params = new URLSearchParams({
    ticket,
    cols: String(cols),
    rows: String(rows),
    ...(session ? { session } : {}),
  })
  return `${base}/ws/projects/${projectId}/terminal?${params}`
}


export interface UsageSlice {
  model: string
  tokens: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  thinking_tokens: number
  /** Null when no rate is known for this model — not the same as free. */
  cost: number | null
  priced: boolean
}

export interface UsageProject {
  project_id: string | null
  project_name: string
  tokens: number
  cost: number | null
}

export interface UsageWindow {
  label: string
  hours: number
  /** Null when nothing has opened a window: no work, no clock running. */
  started_at: string | null
  resets_at: string | null
  tokens: number
  cost: number | null
  /** What the plan allows, if the user has said. Null means no bar is drawn. */
  limit_tokens: number | null
  percent: number | null
}

export interface Usage {
  /** 'oauth' for a subscription, 'api_key' for metered billing. */
  billing: 'oauth' | 'api_key' | 'unknown'
  hours: number
  tokens: number
  cost: number | null
  session_window: UsageWindow
  week_window: UsageWindow
  models: UsageSlice[]
  projects: UsageProject[]
  series: { at: string; tokens: number }[]
}

export interface UsageLimits {
  session_tokens: number | null
  weekly_tokens: number | null
  /** Push once per window when usage crosses this share of the allowance. */
  alert_percent?: number | null
}

export async function usageLimits() {
  return request<UsageLimits>('/api/usage/limits')
}

export async function setUsageLimits(input: UsageLimits) {
  return request<UsageLimits>('/api/usage/limits', {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}

export interface ModelPrice {
  model: string
  input_per_m: number
  output_per_m: number
  /** Ships with Moonphase rather than set here. */
  builtin: boolean
}

export async function usage(hours = 24 * 7) {
  return request<Usage>(`/api/usage?hours=${hours}`)
}

export async function modelPrices() {
  return request<ModelPrice[]>('/api/usage/prices')
}

export async function setModelPrice(input: {
  model: string
  input_per_m: number
  output_per_m: number
}) {
  return request<ModelPrice>('/api/usage/prices', {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}

export async function clearModelPrice(model: string) {
  return request<void>(`/api/usage/prices/${encodeURIComponent(model)}`, {
    method: 'DELETE',
  })
}

// --- attention, changes, search ---------------------------------------------

export interface PromptOption {
  key: string
  label: string
}

export interface Waiting {
  project_id: string
  project_name: string
  session: string
  activity_at: string | null
  question: string
  /** Null when the pane could not be parsed into buttons. */
  prompt: { question: string; options: PromptOption[] } | null
  tail: string
}

export async function attention() {
  return request<Waiting[]>('/api/attention')
}

export async function answerSession(projectId: string, session: string, key: string) {
  return request<void>(
    `/api/projects/${projectId}/sessions/${encodeURIComponent(session)}/answer`,
    { method: 'POST', body: JSON.stringify({ key }) },
  )
}

export interface ChangedFile {
  path: string
  added: number
  removed: number
  status: string
}

export interface Changes {
  branch: string
  base: string
  added: number
  removed: number
  files: ChangedFile[]
  patch: string
  truncated: boolean
  detail: string | null
}

export async function changes(projectId: string, session: string) {
  return request<Changes>(
    `/api/projects/${projectId}/sessions/${encodeURIComponent(session)}/changes`,
  )
}

export interface SearchHit {
  project_id: string
  project_name: string
  session: string
  at: string
  role: string
  text: string
}

export interface SearchResult {
  query: string
  hits: SearchHit[]
  /** True when a machine did not answer in time, so the list is incomplete. */
  partial: boolean
}

export async function searchTranscripts(q: string) {
  return request<SearchResult>(`/api/search?q=${encodeURIComponent(q)}`)
}

// --- save points and summaries ----------------------------------------------

export interface Checkpoint {
  id: string
  at: string
  label: string
  /** True when the files on disk still match this point. */
  current: boolean
  /** Made on the way to somewhere else, not chosen by a person. */
  automatic: boolean
}

export interface Checkpoints {
  points: Checkpoint[]
  unsaved: number
  detail: string | null
}

export async function checkpoints(projectId: string, session: string) {
  return request<Checkpoints>(
    `/api/projects/${projectId}/sessions/${encodeURIComponent(session)}/checkpoints`,
  )
}

export async function saveCheckpoint(projectId: string, session: string, label?: string) {
  return request<Checkpoints>(
    `/api/projects/${projectId}/sessions/${encodeURIComponent(session)}/checkpoints`,
    { method: 'POST', body: JSON.stringify({ label: label ?? null }) },
  )
}

export async function restoreCheckpoint(
  projectId: string,
  session: string,
  checkpoint: string,
) {
  return request<Checkpoints>(
    `/api/projects/${projectId}/sessions/${encodeURIComponent(session)}/checkpoints/${checkpoint}/restore`,
    { method: 'POST' },
  )
}

export interface Digest {
  created: string[]
  edited: string[]
  commands: number
  installs: number
  tests: number
  searches: number
  last_said: string
  detail: string | null
}

export async function sessionSummary(projectId: string, session: string) {
  return request<Digest>(
    `/api/projects/${projectId}/sessions/${encodeURIComponent(session)}/summary`,
  )
}

// --- first run ---------------------------------------------------------------

export interface SetupState {
  needs_setup: boolean
  signup_open: boolean
}

/** Unauthenticated: the client must know whether to show setup or sign-in. */
export async function setupState() {
  const response = await fetch(`${currentHost()}/api/setup`)
  if (!response.ok) throw new ApiError(response.status, 'Could not reach this host.')
  return (await response.json()) as SetupState
}

export async function completeSetup(input: {
  public_url: string | null
  signup_open: boolean
}) {
  return request<SetupState>('/api/setup', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export interface AuthMethods {
  /** Enabled *and* working — what the sign-in screen should offer. */
  enabled: string[]
  password_enabled: boolean
  magic_link_enabled: boolean
  google_enabled: boolean
  microsoft_enabled: boolean
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_sender: string
  google_client_id: string
  microsoft_client_id: string
  microsoft_tenant: string
  /** What to paste into Google's and Microsoft's consoles. */
  redirect_uri: string
  problems: string[]
}

/**
 * Whether this instance is taking new accounts.
 *
 * Unauthenticated, because the only page that needs it is the one nobody has
 * signed in to yet. An instance too old to answer is treated as open, which is
 * what it was.
 */
export async function signupOpen(): Promise<boolean> {
  const response = await fetch(`${currentHost()}/api/config`)
  if (!response.ok) return true
  const config = (await response.json()) as { signup_open?: boolean }
  return config.signup_open !== false
}

/** Unauthenticated: the sign-in screen must know which buttons to draw. */
export async function authMethods() {
  const response = await fetch(`${currentHost()}/api/setup/methods`)
  if (!response.ok) throw new ApiError(response.status, 'Could not reach this host.')
  return (await response.json()) as AuthMethods
}

export async function saveAuthMethods(input: Partial<AuthMethods> & {
  google_client_secret?: string | null
  microsoft_client_secret?: string | null
  smtp_password?: string | null
}) {
  return request<AuthMethods>('/api/setup/methods', {
    method: 'PUT',
    body: JSON.stringify(input),
  })
}

// --- the instance, for whoever administers it -------------------------------

export interface Person {
  id: string
  email: string
  created_at: string | null
  last_sign_in_at: string | null
  /** Administers the instance — not the same as owning an organization. */
  is_admin: boolean
  /** Why a removal may be refused: these would go with the account. */
  owned_projects: number
  is_you: boolean
}

export interface InstanceSettings {
  public_url: string | null
  signup_open: boolean
}

export interface UpdateState {
  running_version: string | null
  running_commit: string | null
  latest_version: string | null
  release_url: string | null
  release_notes: string | null
  published_at: string | null
  /** Null means the question could not be answered — not "up to date". */
  update_available: boolean | null
  detail: string | null
  /** Whether an updater is running, which decides button or command. */
  can_apply: boolean
  status: string | null
  status_detail: string | null
  command: string
}

export const instance = {
  /** Whether the caller may see any of the rest of this. */
  me: () => request<{ is_instance_admin: boolean }>('/api/instance/me'),
  people: () => request<Person[]>('/api/instance/people'),
  invite: (email: string, admin = false) =>
    request<{ id: string; email: string; password: string; is_admin: boolean }>(
      '/api/instance/people',
      { method: 'POST', body: JSON.stringify({ email, admin }) },
    ),
  remove: (id: string) =>
    request<void>(`/api/instance/people/${id}`, { method: 'DELETE' }),
  setAdmin: (id: string, makeAdmin: boolean) =>
    request<Person>(
      `/api/instance/people/${id}/admin?make_admin=${makeAdmin}`,
      { method: 'PUT' },
    ),
  update: (force = false) =>
    request<UpdateState>(`/api/instance/update${force ? '?force=true' : ''}`),
  applyUpdate: () =>
    request<UpdateState>('/api/instance/update', { method: 'POST' }),
  settings: () => request<InstanceSettings>('/api/instance/settings'),
  saveSettings: (input: InstanceSettings) =>
    request<InstanceSettings>('/api/instance/settings', {
      method: 'PUT',
      body: JSON.stringify(input),
    }),
}
